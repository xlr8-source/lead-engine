"""
ingestion/cro_resolver.py

Cross-resolves CBI-listed firms against the CRO Open Data bulk snapshot.

Resolution strategy:
1. Exact match on CRO/company number when available.
2. Fuzzy legal_name match using rapidfuzz WRatio, threshold >= 85.
3. Below-threshold candidates merge NO CRO fields: the firm is flagged
   needs_review=True and the near-miss is recorded in cro_match_method only.

Data source: https://opendata.cro.ie, free bulk company snapshot.
"""
import csv
import io
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
from rapidfuzz import fuzz, process

try:
    from ingestion.http_retry import get_with_retry
except ImportError:  # direct script execution from inside ingestion/
    from http_retry import get_with_retry


DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

CRO_OPEN_DATA_BASE = "https://opendata.cro.ie"
CRO_SNAPSHOT_CACHE = DATA_DIR / "cro_snapshot.csv"
CACHE_MAX_AGE_HOURS = 24
FUZZY_THRESHOLD = 85


def _is_cache_fresh() -> bool:
    if not CRO_SNAPSHOT_CACHE.exists():
        return False
    if CRO_SNAPSHOT_CACHE.read_bytes()[:4] == b"PK\x03\x04":
        return False
    age_hours = (time.time() - CRO_SNAPSHOT_CACHE.stat().st_mtime) / 3600
    return age_hours < CACHE_MAX_AGE_HOURS


def _cache_cro_response(content: bytes, source_url: str) -> None:
    """Persist a CRO CSV response, unzipping .csv.zip resources when needed."""
    if source_url.lower().endswith(".zip") or content[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
            if not csv_names:
                raise RuntimeError("CRO ZIP did not contain a CSV file")
            with zf.open(csv_names[0]) as csv_file:
                content = csv_file.read()

    CRO_SNAPSHOT_CACHE.write_bytes(content)
    print(f"[cro_resolver] Cached {len(content):,} bytes -> {CRO_SNAPSHOT_CACHE}")


def _download_cro_snapshot() -> None:
    print("[cro_resolver] Downloading CRO Open Data snapshot...")
    headers = {"User-Agent": "PayBrix-LeadEngine/1.0"}

    with httpx.Client(timeout=120, follow_redirects=True) as client:
        try:
            pkg_resp = get_with_retry(
                client,
                f"{CRO_OPEN_DATA_BASE}/api/3/action/package_list",
                headers=headers,
                timeout=30,
            )
            if pkg_resp.status_code == 200:
                packages = pkg_resp.json().get("result", [])
                for pkg_name in packages:
                    if not any(k in pkg_name.lower() for k in ("compan", "registr")):
                        continue

                    pkg_detail = get_with_retry(
                        client,
                        f"{CRO_OPEN_DATA_BASE}/api/3/action/package_show?id={pkg_name}",
                        headers=headers,
                    ).json()
                    resources = pkg_detail.get("result", {}).get("resources", [])
                    for res in resources:
                        url = res.get("url", "")
                        fmt = res.get("format", "")
                        if (
                            url.lower().endswith((".csv", ".csv.zip", ".zip"))
                            or "csv" in fmt.lower()
                        ):
                            print(f"[cro_resolver] Found CRO snapshot: {url}")
                            dl_resp = get_with_retry(client, url, headers=headers)
                            if dl_resp.status_code == 200:
                                _cache_cro_response(dl_resp.content, url)
                                return
        except Exception as e:
            print(f"[cro_resolver] CKAN API attempt failed: {e}")

        fallback_url = "https://opendata.cro.ie/dataset/companies/resource/companies.csv"
        print(f"[cro_resolver] Trying fallback URL: {fallback_url}")
        try:
            resp = get_with_retry(client, fallback_url, headers=headers)
            if resp.status_code == 200:
                _cache_cro_response(resp.content, fallback_url)
                return
        except Exception as e:
            print(f"[cro_resolver] Fallback URL failed: {e}")

    print(
        "[cro_resolver] WARNING: Could not download CRO snapshot. "
        "CRO resolution will be skipped; leads will be flagged needs_review=True. "
        "Download manually from opendata.cro.ie and save to data/cro_snapshot.csv"
    )


def _load_cro_snapshot() -> tuple[list[dict], bool]:
    """Load the cached CRO snapshot, downloading first if stale.

    Returns (companies, degraded). degraded=True means no usable CRO rows are
    available — callers must surface that instead of letting a blanket
    needs_review run masquerade as a clean one (Fix #2).
    """
    if not _is_cache_fresh():
        _download_cro_snapshot()

    if not CRO_SNAPSHOT_CACHE.exists():
        return [], True

    companies = []
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = CRO_SNAPSHOT_CACHE.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = CRO_SNAPSHOT_CACHE.read_text(encoding="latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        companies.append(dict(row))

    print(f"[cro_resolver] Loaded {len(companies):,} CRO companies from snapshot")
    return companies, not companies


def _norm_cro_number(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    return re.sub(r"[^0-9A-Za-z]", "", str(val)).upper()


def _norm_name(val: Optional[str]) -> str:
    if not val:
        return ""
    val = re.sub(r"\b(LIMITED|LTD|DAC|UC|CLG|PLC|UNLIMITED)\b", "", val, flags=re.IGNORECASE)
    return " ".join(val.upper().split())


# Public alias: db/dal.py and db/init_db.py reuse this exact folding for the
# legal_name_normalized dedup key (Fix #3), so the resolver and the
# persistence layer share ONE definition of company-name identity.
normalize_legal_name = _norm_name


def _get_cro_field_exact(row: dict, *exact_names: str) -> Optional[str]:
    """Try exact (case-insensitive) column names before any substring fallback.
    The real CRO export has BOTH company_status_code (numeric) and company_status
    (text) -- substring matching on "status" alone silently grabs the numeric one.
    Confirmed against opendata.cro.ie/dataset/companies schema."""
    lower_map = {k.lower(): v for k, v in row.items() if k}
    for name in exact_names:
        v = lower_map.get(name.lower())
        if v and str(v).strip():
            return str(v).strip()
    return None

def _get_cro_field(row: dict, *keys) -> Optional[str]:
    for key in keys:
        for row_key in row:
            if row_key and key.lower() in row_key.lower():
                value = row[row_key]
                if value and str(value).strip():
                    return str(value).strip()
    return None


def resolve_against_cro(
    cbi_firms: list[dict],
    cro_companies: Optional[list[dict]] = None,
) -> tuple[list[dict], bool]:
    """
    Enrich CBI firm dicts with CRO fields and review flags.

    Returns (resolved_firms, degraded). degraded=True means CRO enrichment
    never ran (no snapshot data) and every firm was blanket-flagged
    needs_review — the caller must record that as a run error (Fix #2).
    """
    if cro_companies is None:
        cro_companies, degraded = _load_cro_snapshot()
    else:
        degraded = not cro_companies

    if not cro_companies:
        print("[cro_resolver] No CRO data available; all firms flagged needs_review")
        for firm in cbi_firms:
            firm.update(
                {
                    "cro_number": None,
                    "cro_status": None,
                    "incorporation_date": None,
                    "cro_match_confidence": 0.0,
                    "needs_review": True,
                    "cro_match_method": "no_cro_snapshot",
                }
            )
        return cbi_firms, degraded

    cro_by_number: dict[str, dict] = {}
    cro_names: list[str] = []
    cro_name_to_row: dict[str, dict] = {}
    cro_names_by_first_token: dict[str, list[str]] = {}

    for row in cro_companies:
        num = _norm_cro_number(
            _get_cro_field(row, "company number", "company_number", "company_num", "regno", "number", "num")
        )
        name = _get_cro_field(row, "company name", "company_name", "name")
        if num:
            cro_by_number[num] = row
        if name:
            norm = _norm_name(name)
            cro_names.append(norm)
            cro_name_to_row[norm] = row
            first_token = norm.split(" ", 1)[0] if norm else ""
            if first_token:
                cro_names_by_first_token.setdefault(first_token, []).append(norm)

    resolved = []
    for firm in cbi_firms:
        enriched = dict(firm)
        matched_row = None
        confidence = 0.0
        match_method = "no_match"

        cbi_cro_num = _norm_cro_number(firm.get("cro_number"))
        if cbi_cro_num and cbi_cro_num in cro_by_number:
            matched_row = cro_by_number[cbi_cro_num]
            confidence = 1.0
            match_method = "exact_cro_number"

        if matched_row is None and firm.get("legal_name"):
            query = _norm_name(firm["legal_name"])
            if query in cro_name_to_row:
                matched_row = cro_name_to_row[query]
                confidence = 1.0
                match_method = "exact_normalized_name"
            elif cro_names:
                first_token = query.split(" ", 1)[0] if query else ""
                choices = cro_names_by_first_token.get(first_token) or cro_names
                result = process.extractOne(
                    query,
                    choices,
                    scorer=fuzz.WRatio,
                    score_cutoff=60,
                )
                if result:
                    matched_name, score, _ = result
                    if score >= FUZZY_THRESHOLD:
                        matched_row = cro_name_to_row[matched_name]
                        confidence = score / 100.0
                        match_method = f"fuzzy_name(score={score})"
                    else:
                        # Below-threshold candidate: record the near-miss for
                        # human review, but merge NOTHING from it — a 60-84
                        # score is a different company, not a weak match.
                        matched_row = None
                        confidence = score / 100.0
                        match_method = f"fuzzy_name_low_confidence(score={score})"

        if matched_row:
            enriched["cro_number"] = (
                _get_cro_field_exact(matched_row, "company_num")
                or _get_cro_field(matched_row, "company number", "company_number", "company_num", "regno", "number", "num")
            )
            enriched["cro_status"] = (
                _get_cro_field_exact(matched_row, "company_status")
                or _get_cro_field(matched_row, "status", "company status", "company_status")
            )
            enriched["incorporation_date"] = (
                _get_cro_field_exact(matched_row, "company_reg_date")
                or _get_cro_field(matched_row, "incorporation date", "date incorporated", "date_incorporated", "reg_date")
            )
            # New fields -- real per-company differentiators instead of re-deriving
            # everything from address + incorporation date every time.
            enriched["company_type"] = _get_cro_field_exact(matched_row, "company_type")
            enriched["last_annual_return"] = _get_cro_field_exact(matched_row, "last_ar_date")
            enriched["last_accounts_date"] = _get_cro_field_exact(matched_row, "last_accounts_date")
            enriched["principal_object"] = _get_cro_field_exact(matched_row, "princ_object_code")
            enriched["cro_match_confidence"] = round(confidence, 4)
            enriched["needs_review"] = confidence < (FUZZY_THRESHOLD / 100.0)
            enriched["cro_match_method"] = match_method
        else:
            enriched["cro_number"] = None
            enriched["cro_status"] = None
            enriched["incorporation_date"] = None
            enriched["company_type"] = None
            enriched["last_annual_return"] = None
            enriched["last_accounts_date"] = None
            enriched["principal_object"] = None
            # confidence/match_method carry the low-confidence near-miss label
            # (e.g. "fuzzy_name_low_confidence(score=75.2)") so reviewers can
            # see WHY the row needs review; plain "no_match"/0.0 otherwise.
            enriched["cro_match_confidence"] = round(confidence, 4)
            enriched["needs_review"] = True
            enriched["cro_match_method"] = match_method

        resolved.append(enriched)

    matched_count = sum(1 for firm in resolved if not firm["needs_review"])
    print(
        f"[cro_resolver] Resolved {matched_count}/{len(resolved)} firms "
        f"(threshold >= {FUZZY_THRESHOLD}%); "
        f"{len(resolved) - matched_count} flagged needs_review"
    )
    return resolved, degraded
