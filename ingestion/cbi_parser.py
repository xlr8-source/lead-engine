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


PDF_RECORD_RE = re.compile(
    r"^(C\d+)\s+(.+?)\s+((?:Ancillary\s+)?(?:Insurance|Reinsurance)\s+Intermediary)"
    r"\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*(.*)$"
)

LEGAL_SUFFIX_LINES = {
    "limited",
    "ltd",
    "ltd.",
    "dac",
    "designated activity company",
    "unlimited company",
    "public limited company",
}


def _parse_cbi_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(value, "%d %B %Y").date().isoformat()
    except ValueError:
        return value


def _parse_pdf_text_records(pdf) -> list[dict]:
    """Fallback parser for CBI PDFs that expose text lines but no tables."""
    records = []
    current: list[str] = []

    def flush_current() -> None:
        if not current:
            return
        record = _normalise_pdf_record(current)
        if record:
            records.append(record)

    for page in pdf.pages:
        text = page.extract_text() or ""
        for raw_line in text.splitlines():
            line = " ".join(raw_line.strip().split())
            if not line:
                continue
            if line.startswith("Run Date:"):
                continue
            if line.startswith("Ref No.") or line.startswith("Insurance Distribution Register"):
                continue
            if line.startswith("under the European Union"):
                continue

            if re.match(r"^C\d+\b", line):
                flush_current()
                current = [line]
            elif current:
                current.append(line)

    flush_current()
    return records


def _normalise_pdf_record(lines: list[str]) -> Optional[dict]:
    first_line = lines[0]
    match = PDF_RECORD_RE.match(first_line)
    if not match:
        return None

    cbi_reference, legal_name, registered_as, registered_on, first_line_tail = match.groups()
    remaining = [line for line in lines[1:] if line]

    if remaining and remaining[0].strip().lower() in LEGAL_SUFFIX_LINES:
        legal_name = f"{legal_name} {remaining.pop(0)}"

    trading_lines = []
    while remaining and remaining[0].lower().startswith("t/a "):
        trading_lines.append(remaining.pop(0)[4:].strip())

    address_lines = []
    for line in remaining:
        lowered = line.lower()
        if "(fos)" in lowered:
            continue
        address_lines.append(line)

    address = ", ".join(address_lines) if address_lines else None
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
        "raw_row": {
            "lines": lines,
            "first_line_tail": first_line_tail,
        },
    }


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
