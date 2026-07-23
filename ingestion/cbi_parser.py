"""
ingestion/cbi_parser.py

Isolated, swappable parser for the CBI insurance intermediary register.
Supports: CSV, XLSX, PDF (pdfplumber).

Public API:
    parse_cbi_register(path: Path) -> list[dict]

Each returned dict has a normalised shape:
{
    "cbi_reference": str | None,
    "legal_name": str,
    "trading_name": str | None,
    "registered_address": str | None,
    "county": str | None,
    "eircode": str | None,
    "authorisation_type": str | None,
    "authorisation_status": str | None,
    "raw_row": dict   # original unparsed row for audit
}

If the CBI changes publication format, only this file needs updating.
Nothing downstream touches format-specific logic.
"""
import csv
import io
import json
import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

COUNTY_NAMES = {
    "antrim", "armagh", "carlow", "cavan", "clare", "cork", "donegal",
    "down", "dublin", "fermanagh", "galway", "kerry", "kildare", "kilkenny",
    "laois", "leitrim", "limerick", "longford", "louth", "mayo", "meath",
    "monaghan", "offaly", "roscommon", "sligo", "tipperary", "tyrone",
    "waterford", "westmeath", "wexford", "wicklow",
    # Common abbreviations / alternate spellings
    "co. dublin", "co. cork", "co. galway",
}

EIRCODE_RE = re.compile(r'\b([AC-FHKNPRTVY]\d{2}\s?[AC-FHKNPRTVY0-9]{4})\b', re.IGNORECASE)


def _extract_county(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    addr_lower = address.lower()
    for county in COUNTY_NAMES:
        if county in addr_lower:
            return county.replace("co. ", "").title()
    return None


def _extract_eircode(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    m = EIRCODE_RE.search(address)
    return m.group(1).upper() if m else None


def _normalise_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return " ".join(name.strip().split())


def _normalise_row(row: dict) -> dict:
    """
    Map raw column names (which vary between CBI publications) to our canonical schema.
    Handles known column name variants observed in CBI register downloads.
    """
    def get(*keys) -> Optional[str]:
        for k in keys:
            for rk in row:
                if rk and k.lower() in rk.lower():
                    v = row[rk]
                    if v and str(v).strip():
                        return str(v).strip()
        return None

    legal_name = (
        get("firm name", "legal name", "name of firm", "registered name", "company name", "name")
        or ""
    )
    address = get("address", "registered address", "registered office", "principal place")
    county = get("county") or _extract_county(address)
    eircode = get("eircode", "postcode", "eir code") or _extract_eircode(address)

    return {
        "cbi_reference": get("reference", "authorisation number", "ref", "cbi ref", "registration number"),
        "legal_name": _normalise_name(legal_name),
        "trading_name": _normalise_name(get("trading name", "trading as", "also known as")),
        "registered_address": address,
        "county": county,
        "eircode": eircode,
        "authorisation_type": get("type", "authorisation type", "category", "licence type"),
        "authorisation_status": get("status", "authorisation status", "current status"),
        "raw_row": row,
    }


# ---------------------------------------------------------------------------
# Format-specific parsers
# ---------------------------------------------------------------------------

def _parse_csv(path: Path) -> list[dict]:
    """Parse a CSV or TSV register file."""
    print(f"[cbi_parser] Parsing CSV: {path}")
    rows = []

    # Try UTF-8 first, fall back to latin-1 (common in Irish gov exports)
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = path.read_text(encoding="latin-1", errors="replace")

    # Detect delimiter
    sample = text[:2048]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    for raw_row in reader:
        normalised = _normalise_row(dict(raw_row))
        if normalised["legal_name"]:
            rows.append(normalised)

    print(f"[cbi_parser] CSV: parsed {len(rows)} records")
    return rows


def _parse_xlsx(path: Path) -> list[dict]:
    """Parse an Excel (xlsx) register file."""
    print(f"[cbi_parser] Parsing XLSX: {path}")
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError(
            "openpyxl is required to parse XLSX files. "
            "Run: pip install openpyxl"
        )

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = []

    for sheet in wb.worksheets:
        data = list(sheet.values)
        if len(data) < 2:
            continue

        # First non-empty row is the header
        header = None
        data_rows = []
        for r in data:
            if header is None:
                if any(c for c in r if c):
                    header = [str(c).strip() if c else "" for c in r]
            else:
                data_rows.append(r)

        if not header:
            continue

        for raw in data_rows:
            raw_dict = {header[i]: (str(v).strip() if v is not None else "") for i, v in enumerate(raw) if i < len(header)}
            normalised = _normalise_row(raw_dict)
            if normalised["legal_name"]:
                rows.append(normalised)

    print(f"[cbi_parser] XLSX: parsed {len(rows)} records across {len(wb.worksheets)} sheet(s)")
    return rows


LEGAL_SUFFIX_LINES = {
    "limited",
    "ltd",
    "ltd.",
    "dac",
    "designated activity company",
    "unlimited company",
    "public limited company",
}

# Column x0 (left-edge) start positions for the CBI Insurance Distribution
# Register PDF template — verified 2026-07 against a live download from
# registers.centralbank.ie. This register has no cell borders, so
# pdfplumber's extract_tables() finds nothing and a naive extract_text()
# flattens every column into single lines purely by y-position: on any row
# where only the Intermediary column (name/trading-name/address, which
# wraps across many lines) and the Passporting Into column (an unrelated
# EU-country checklist, which also spans many lines) both have content,
# their text gets glued into one bogus line. That's the exact mechanism
# behind the Clements Insurance case: legal trading name "Gallagher" fused
# with "Belgium (FOS)" from the Passporting Into column into "t/a Gallagher
# Belgium (FOS)", and three more Passporting Into entries (Cyprus, Czech
# Republic, Germany) leaked into what became registered_address — which
# then read to the LLM as evidence of "a multinational group entity," not
# an independent Irish intermediary, and also poisoned website-search
# queries built from that trading name.
_PDF_COLUMNS = [
    ("ref_no", 44.8),
    ("intermediary", 106.7),
    ("registered_as", 243.2),
    ("registered_on", 363.0),
    ("tied_to", 450.9),
    ("persons_responsible", 604.4),
    ("passporting_into", 715.8),
]
_REF_NO_RE = re.compile(r"^C\d+$")


def _parse_cbi_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(value, "%d %B %Y").date().isoformat()
    except ValueError:
        return value


def _column_for_x(x0: float) -> str:
    """Snap a word's x0 to the nearest column start at or to its left."""
    col = _PDF_COLUMNS[0][0]
    for name, start in _PDF_COLUMNS:
        if x0 + 0.5 >= start:
            col = name
        else:
            break
    return col


def _extract_columnar_rows(page) -> list[dict]:
    """One dict per visual text row on the page, keyed by column name —
    words are bucketed by x-position instead of flattened into one string,
    so unrelated columns sharing a row's y-position never mix."""
    words = page.extract_words()
    rows_by_top: dict[int, list] = {}
    for w in words:
        rows_by_top.setdefault(round(w["top"]), []).append(w)
    rows = []
    for top in sorted(rows_by_top):
        row_words = sorted(rows_by_top[top], key=lambda w: w["x0"])
        cols: dict[str, list[str]] = {}
        for w in row_words:
            cols.setdefault(_column_for_x(w["x0"]), []).append(w["text"])
        rows.append({name: " ".join(tokens) for name, tokens in cols.items()})
    return rows


def _build_record_from_intermediary_lines(
    cbi_reference: str,
    registered_as: Optional[str],
    registered_on: Optional[str],
    lines: list[str],
) -> Optional[dict]:
    if not lines:
        return None
    legal_name = lines[0]
    remaining = [line for line in lines[1:] if line]

    if remaining and remaining[0].strip().lower() in LEGAL_SUFFIX_LINES:
        legal_name = f"{legal_name} {remaining.pop(0)}"

    trading_lines = []
    while remaining and remaining[0].lower().startswith("t/a "):
        trading_lines.append(remaining.pop(0)[4:].strip())

    address = ", ".join(remaining) if remaining else None
    legal_name = _normalise_name(legal_name) or ""
    trading_name = _normalise_name(", ".join(trading_lines)) if trading_lines else None

    return {
        "cbi_reference": cbi_reference,
        "legal_name": legal_name,
        "trading_name": trading_name,
        "registered_address": address,
        "county": _extract_county(address),
        "eircode": _extract_eircode(address),
        "authorisation_type": registered_as,
        "authorisation_status": "registered",
        "registered_on": _parse_cbi_date(registered_on),
        "raw_row": {"lines": lines},
    }


def _parse_pdf_text_records(pdf) -> list[dict]:
    """Column-aware fallback for CBI PDFs that expose no extractable table
    borders. See _PDF_COLUMNS for why this reconstructs records from
    x-bucketed words rather than pdfplumber's flowed page text."""
    records: list[dict] = []
    current_ref: Optional[str] = None
    current_registered_as: Optional[str] = None
    current_registered_on: Optional[str] = None
    current_lines: list[str] = []

    def flush() -> None:
        if current_ref is None:
            return
        record = _build_record_from_intermediary_lines(
            current_ref, current_registered_as, current_registered_on, current_lines,
        )
        if record and record["legal_name"]:
            records.append(record)

    for page in pdf.pages:
        for row in _extract_columnar_rows(page):
            ref_no = (row.get("ref_no") or "").strip()
            intermediary = (row.get("intermediary") or "").strip()
            # The column header repeats on every page — its own cells must
            # never be read as a continuation line of the record that
            # happens to end at a page boundary.
            if intermediary == "Intermediary*" or ref_no.lower().startswith("ref"):
                continue
            if ref_no and _REF_NO_RE.match(ref_no):
                flush()
                current_ref = ref_no
                current_registered_as = (row.get("registered_as") or "").strip() or None
                current_registered_on = (row.get("registered_on") or "").strip() or None
                current_lines = [intermediary] if intermediary else []
            elif current_ref is not None and intermediary:
                current_lines.append(intermediary)

    flush()
    return records


def _parse_pdf(path: Path) -> list[dict]:
    """
    Parse a PDF register file using pdfplumber.
    CBI PDFs are typically tabular -- pdfplumber's table extraction handles these well.
    """
    print(f"[cbi_parser] Parsing PDF: {path}")
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError(
            "pdfplumber is required to parse PDF files. "
            "Run: pip install pdfplumber"
        )

    rows = []
    header = None

    with pdfplumber.open(path) as pdf:
        print(f"[cbi_parser] PDF has {len(pdf.pages)} pages")
        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                for row in table:
                    if row is None:
                        continue
                    clean_row = [str(c).strip() if c else "" for c in row]
                    if not any(clean_row):
                        continue

                    # Detect header row (contains known column label patterns)
                    if header is None:
                        row_joined = " ".join(clean_row).lower()
                        if any(k in row_joined for k in ("name", "reference", "address", "status", "type")):
                            header = clean_row
                            continue

                    if header and any(clean_row):
                        raw_dict = {header[i]: clean_row[i] for i in range(min(len(header), len(clean_row)))}
                        normalised = _normalise_row(raw_dict)
                        if normalised["legal_name"]:
                            rows.append(normalised)

        if not rows:
            print("[cbi_parser] No tables found; falling back to text-line PDF parser")
            rows = _parse_pdf_text_records(pdf)

    print(f"[cbi_parser] PDF: parsed {len(rows)} records")
    return rows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_cbi_register(path: Path) -> list[dict]:
    """
    Parse the CBI register file at `path`.
    Auto-detects format from file extension.
    Returns a list of normalised company dicts ready for CRO cross-resolution.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in (".csv", ".txt", ".tsv"):
        return _parse_csv(path)
    elif suffix in (".xlsx", ".xls"):
        return _parse_xlsx(path)
    elif suffix == ".pdf":
        return _parse_pdf(path)
    else:
        # Last resort: try CSV (some downloads have no/wrong extension)
        print(f"[cbi_parser] Unknown extension '{suffix}', attempting CSV parse...")
        return _parse_csv(path)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python cbi_parser.py <path_to_register_file>")
        sys.exit(1)
    records = parse_cbi_register(Path(sys.argv[1]))
    print(f"\nParsed {len(records)} records. First 3:")
    for r in records[:3]:
        print(json.dumps({k: v for k, v in r.items() if k != "raw_row"}, indent=2))
