"""
ingestion/cbi_fetcher.py

Fetches the Central Bank of Ireland Insurance Distribution register.

Strategy:
  1. GET the CBI registers download page.
  2. Find the current Insurance Distribution Register item.
  3. Download the file, detect PDF vs CSV/XLSX, and return the local path.

The CBI page may expose either direct file links or ASP.NET __doPostBack links.
The parser remains swappable, so no downstream code depends on publication format.
"""
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse
import re

import httpx

try:
    from ingestion.http_retry import get_with_retry
except ImportError:  # direct script execution: python ingestion/cbi_fetcher.py
    from http_retry import get_with_retry


CBI_REGISTER_PAGE = "https://registers.centralbank.ie/DownloadsPage.aspx"

CBI_DIRECT_LINK_PATTERNS = [
    r"(?i)insurance.{0,40}(intermediar|distribution|retail).{0,40}\.csv",
    r"(?i)insurance.{0,40}(intermediar|distribution|retail).{0,40}\.pdf",
    r"(?i)insurance.{0,40}(intermediar|distribution|retail).{0,40}\.xlsx",
    r"(?i)retail.{0,40}intermediar.{0,40}\.(csv|pdf|xlsx)",
]

DOWNLOAD_DIR = Path(__file__).parent.parent / "data"
DOWNLOAD_DIR.mkdir(exist_ok=True)


def _safe_filename(label: str, suffix: str = ".pdf") -> str:
    label = unescape(label)
    date_match = re.search(r"as at\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", label, re.IGNORECASE)
    if date_match:
        day, month, year = date_match.groups()
        month_num = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }.get(month[:3].lower(), "01")
        return f"cbi_register_{year}-{month_num}-{int(day):02d}{suffix}"

    slug = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()
    return f"{slug[:80] or 'cbi_register'}{suffix}"


def _find_direct_register_url(html: str, base_url: str) -> tuple[str, str] | tuple[None, None]:
    """
    Scan the CBI downloads page HTML for a direct link to the register.
    Returns (absolute_url, detected_filename) or (None, None) if not found.
    """
    href_pattern = re.compile(r'href=["\']([^"\']+\.(csv|pdf|xlsx))["\']', re.IGNORECASE)
    for match in href_pattern.finditer(html):
        href = match.group(1)
        for pattern in CBI_DIRECT_LINK_PATTERNS:
            if re.search(pattern, href):
                abs_url = urljoin(base_url, href)
                filename = Path(urlparse(abs_url).path).name
                return abs_url, filename

    for match in href_pattern.finditer(html):
        href = match.group(1)
        if re.search(r"(?i)(insurance|intermediar|reinsur)", href):
            abs_url = urljoin(base_url, href)
            filename = Path(urlparse(abs_url).path).name
            return abs_url, filename

    return None, None


def _find_postback_register_target(html: str) -> tuple[str, str] | tuple[None, None]:
    """
    Return the ASP.NET __doPostBack target for the current Insurance Distribution Register.
    The temporary run-off register is intentionally excluded.

    Matches both markup shapes seen on registers.centralbank.ie:
      <a href="javascript:__doPostBack(...)"><span>Label</span></a>
      <a href="javascript:__doPostBack(...)">Label</a>              (no span)
    Any inner tags are stripped before matching the label text, so the
    presence or absence of a <span> wrapper no longer matters.
    """
    link_pattern = re.compile(
        r"<a[^>]+href=[\"']javascript:__doPostBack\('([^']+)','[^']*'\)[\"'][^>]*>"
        r"(.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    for target, label_html in link_pattern.findall(html):
        label = re.sub(r"<[^>]+>", "", unescape(label_html)).strip()
        normalized = " ".join(label.split()).lower()
        if (
            normalized.startswith("insurance distribution register")
            and "temporary" not in normalized
            and "run-off" not in normalized
        ):
            return target, _safe_filename(label, ".pdf")
    return None, None


def _hidden_form_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        match = re.search(
            rf'<input[^>]+name=["\']{re.escape(name)}["\'][^>]+value=["\']([^"\']*)["\']',
            html,
            re.IGNORECASE,
        )
        fields[name] = unescape(match.group(1)) if match else ""
    return fields


def _filename_from_response(resp: httpx.Response, fallback: str) -> str:
    content_disposition = resp.headers.get("content-disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition, re.IGNORECASE)
    if match:
        response_filename = Path(unescape(match.group(1))).name
        if not re.fullmatch(r"\d+\.(pdf|csv|xlsx?)", response_filename, re.IGNORECASE):
            return response_filename

    content_type = resp.headers.get("content-type", "").lower()
    content = resp.content[:16]
    if content.startswith(b"%PDF") and not fallback.lower().endswith(".pdf"):
        return f"{Path(fallback).stem}.pdf"
    if (
        ("spreadsheet" in content_type or "excel" in content_type)
        and not fallback.lower().endswith((".xlsx", ".xls"))
    ):
        return f"{Path(fallback).stem}.xlsx"
    if ("csv" in content_type or "text/csv" in content_type) and not fallback.lower().endswith(".csv"):
        return f"{Path(fallback).stem}.csv"
    return fallback


def fetch_cbi_register(output_dir: Path = DOWNLOAD_DIR) -> Path:
    """
    Download the current CBI insurance intermediary register.
    Returns the local path to the downloaded file.
    Raises RuntimeError if the register URL cannot be resolved.
    """
    print(f"[cbi_fetcher] Fetching CBI register page: {CBI_REGISTER_PAGE}")

    headers = {
        "User-Agent": (
            "PayBrix-LeadEngine/1.0 (research prototype; "
            "contact: engineering@paybrix.io)"
        )
    }

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = get_with_retry(client, CBI_REGISTER_PAGE, headers=headers)
        resp.raise_for_status()

        register_url, filename = _find_direct_register_url(resp.text, str(resp.url))

        if register_url:
            print(f"[cbi_fetcher] Downloading: {register_url}")
            dl_resp = get_with_retry(client, register_url, headers=headers)
        else:
            postback_target, filename = _find_postback_register_target(resp.text)
            if not postback_target:
                raise RuntimeError(
                    "Could not find the Insurance Distribution Register download link "
                    "on registers.centralbank.ie."
                )

            print(f"[cbi_fetcher] Downloading via ASP.NET postback: {postback_target}")
            form_data = {
                **_hidden_form_fields(resp.text),
                "__EVENTTARGET": postback_target,
                "__EVENTARGUMENT": "",
            }
            dl_resp = client.post(str(resp.url), data=form_data, headers=headers)

        if dl_resp.status_code == 404:
            raise RuntimeError(
                "CBI register download returned 404. "
                "The publication URL may have changed; check registers.centralbank.ie manually."
            )
        dl_resp.raise_for_status()

        filename = _filename_from_response(dl_resp, filename or "cbi_register.bin")
        out_path = output_dir / filename
        out_path.write_bytes(dl_resp.content)
        print(f"[cbi_fetcher] Saved {len(dl_resp.content):,} bytes -> {out_path}")
        return out_path


if __name__ == "__main__":
    path = fetch_cbi_register()
    print(f"Register downloaded to: {path}")
    print(f"Detected format: {path.suffix.upper()}")
