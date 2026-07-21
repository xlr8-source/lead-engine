"""
Characterization tests for the ingestion pipeline
(cbi_fetcher.py, cbi_parser.py, cro_resolver.py, runner.py + db persistence).

Written per PRODUCT_BOARD_FINDINGS.md (2026-07-20). These pin the pipeline's
current observable behavior so the four board fixes can land safely.

NOTE: test_low_confidence_fuzzy_match_does_not_merge_cro_data asserts the
*correct* behavior demanded by Fix #1 (CRITICAL) and therefore FAILS until
that fix lands. Every other test passes against the pre-fix code.

All fuzzy-score fixtures were verified empirically against rapidfuzz WRatio
via the repo's own _norm_name:
    "CELTIC COVER INSURANCES"  vs "CELTIC INSURANCE ADVISORS"  -> 75.2 (low band)
    "ACME INSURANCE BROKERS IRELAND" vs "ACME INSURANCE BROKERS" -> 95.0 (high band)
    "ZENITH MARITIME CONSULTING" vs "CELTIC INSURANCE ADVISORS" -> 31.4 (below cutoff)

No test touches the network or the real leads_vault.db.
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from ingestion import cbi_fetcher, cro_resolver, http_retry, runner
from ingestion.cbi_fetcher import (
    _filename_from_response,
    _find_direct_register_url,
    _find_postback_register_target,
)
from ingestion.http_retry import get_with_retry
from ingestion.cbi_parser import _normalise_pdf_record, parse_cbi_register
from ingestion.cro_resolver import resolve_against_cro
from db import dal
from db.init_db import init_db


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Isolated SQLite DB; redirects dal's module-level default path away from
    the real leads_vault.db for the duration of the test."""
    db_file = tmp_path / "test_leads.db"
    init_db(str(db_file))
    monkeypatch.setattr(dal, "_DB_PATH", db_file)
    return db_file


def _cro_row(num: str, name: str, status: str = "Normal", reg_date: str = "2015-04-01") -> dict:
    """A CRO snapshot row using the real opendata.cro.ie column names."""
    return {
        "company_num": num,
        "company_name": name,
        "company_status": status,
        "company_reg_date": reg_date,
        "company_type": "Private Company Limited by Shares",
        "last_ar_date": "2025-09-30",
        "last_accounts_date": "2024-12-31",
        "princ_object_code": "66220",
    }


def _cbi_firm(legal_name: str, **extra) -> dict:
    """A parsed CBI firm dict in the shape cbi_parser produces."""
    firm = {
        "cbi_reference": "C100001",
        "legal_name": legal_name,
        "trading_name": None,
        "registered_address": "12 Main Street, Dublin, D06 K4E5",
        "county": "Dublin",
        "eircode": "D06 K4E5",
        "authorisation_type": "Insurance Intermediary",
        "authorisation_status": "registered",
        "raw_row": {},
    }
    firm.update(extra)
    return firm


def _resolve(firms: list[dict], cro_companies: list[dict]) -> list[dict]:
    """Single indirection point for the (resolved, degraded) contract:
    enrichment-semantics tests only need the resolved list."""
    return resolve_against_cro(firms, cro_companies)[0]


# ---------------------------------------------------------------------------
# cbi_parser
# ---------------------------------------------------------------------------

def test_parse_csv_normalises_and_extracts_location(tmp_path):
    reg = tmp_path / "register.csv"
    reg.write_text(
        "Firm Name,Trading Name,Registered Address,Reference Number\n"
        '"Acme  Insurance Brokers Limited","Acme Cover","12 Main Street, Dublin, D06 K4E5",C112233\n',
        encoding="utf-8",
    )

    rows = parse_cbi_register(reg)

    assert len(rows) == 1
    row = rows[0]
    assert row["legal_name"] == "Acme Insurance Brokers Limited"  # whitespace collapsed
    assert row["trading_name"] == "Acme Cover"
    assert row["cbi_reference"] == "C112233"
    assert row["county"] == "Dublin"
    # NB: EIRCODE_RE's alphabet excludes W/X, so fixtures must use in-alphabet
    # letters (a "D06 X4E5" fixture would extract nothing).
    assert row["eircode"] == "D06 K4E5"
    assert row["raw_row"]["Firm Name"] == "Acme  Insurance Brokers Limited"


def test_parse_csv_skips_rows_without_legal_name(tmp_path):
    reg = tmp_path / "register.csv"
    reg.write_text(
        "Firm Name,Registered Address\n"
        ',"1 Nameless Way, Cork"\n'
        '"Real Firm","2 Named Street, Cork"\n',
        encoding="utf-8",
    )

    rows = parse_cbi_register(reg)

    assert [r["legal_name"] for r in rows] == ["Real Firm"]


def test_parse_csv_detects_tab_delimiter(tmp_path):
    reg = tmp_path / "register.txt"
    reg.write_text(
        "Firm Name\tTrading Name\tRegistered Address\n"
        "Acme Insurance Brokers Limited\tAcme Cover\t12 Main Street, Dublin, D06 K4E5\n",
        encoding="utf-8",
    )

    rows = parse_cbi_register(reg)

    assert len(rows) == 1
    assert rows[0]["legal_name"] == "Acme Insurance Brokers Limited"
    assert rows[0]["registered_address"] == "12 Main Street, Dublin, D06 K4E5"


def test_normalise_pdf_record_full_shape():
    lines = [
        "C54321 Emerald Cover Insurance Intermediary 5 March 2018",
        "Limited",
        "t/a Emerald Direct",
        "Unit 4, Galway, H91 AK12",
    ]

    record = _normalise_pdf_record(lines)

    assert record is not None
    assert record["cbi_reference"] == "C54321"
    assert record["legal_name"] == "Emerald Cover Limited"
    assert record["trading_name"] == "Emerald Direct"
    assert record["county"] == "Galway"
    assert record["eircode"] == "H91 AK12"
    assert record["authorisation_type"] == "Insurance Intermediary"
    assert record["authorisation_status"] == "registered"
    assert record["registered_on"] == "2018-03-05"


# ---------------------------------------------------------------------------
# cbi_fetcher (pure HTML/response helpers -- no network)
# ---------------------------------------------------------------------------

def test_find_direct_register_url_matches_insurance_csv():
    html = '<a href="/docs/insurance-intermediaries-register.csv">Download</a>'

    url, filename = _find_direct_register_url(html, "https://registers.centralbank.ie/DownloadsPage.aspx")

    assert url == "https://registers.centralbank.ie/docs/insurance-intermediaries-register.csv"
    assert filename == "insurance-intermediaries-register.csv"


def test_find_postback_target_skips_temporary_runoff():
    html = (
        "<a href=\"javascript:__doPostBack('ctl00$cph$lnkTemp','')\">"
        "Insurance Distribution Register (Temporary Run-off Regime) as at 1 July 2026</a>"
        "<a href=\"javascript:__doPostBack('ctl00$cph$lnkMain','')\">"
        "<span>Insurance Distribution Register as at 1 July 2026</span></a>"
    )

    target, filename = _find_postback_register_target(html)

    assert target == "ctl00$cph$lnkMain"
    assert filename == "cbi_register_2026-07-01.pdf"


def test_filename_from_response_prefers_content_disposition():
    resp = httpx.Response(
        200,
        headers={"content-disposition": 'attachment; filename="register_2026.csv"'},
        content=b"a,b\n",
    )

    assert _filename_from_response(resp, "fallback.bin") == "register_2026.csv"


def test_filename_from_response_sniffs_pdf_magic():
    resp = httpx.Response(
        200,
        headers={"content-type": "application/octet-stream"},
        content=b"%PDF-1.7 rest-of-document",
    )

    assert _filename_from_response(resp, "cbi_register.bin") == "cbi_register.pdf"


# ---------------------------------------------------------------------------
# Fix #4: retry with exponential backoff on external fetches
# ---------------------------------------------------------------------------

def test_get_with_retry_retries_transport_errors_with_backoff():
    calls = {"n": 0}

    class FlakyClient:
        def get(self, url, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectTimeout("transient outage")
            return "response"

    sleeps = []
    result = get_with_retry(FlakyClient(), "https://example.test/x", sleep=sleeps.append)

    assert result == "response"
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]  # exponential: base, base*2


def test_get_with_retry_exhausts_attempts_and_raises():
    calls = {"n": 0}

    class DeadClient:
        def get(self, url, **kwargs):
            calls["n"] += 1
            raise httpx.ConnectTimeout("still down")

    sleeps = []
    with pytest.raises(httpx.ConnectTimeout):
        get_with_retry(DeadClient(), "https://example.test/x", sleep=sleeps.append)

    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_fetch_cbi_register_retries_transient_timeouts(tmp_path, monkeypatch):
    """Fix #4 validation (per findings doc): the register-page GET times out
    twice then succeeds — fetch_cbi_register must still return a valid path,
    having attempted that call site 3 times."""
    monkeypatch.setattr(http_retry, "BASE_DELAY_SECONDS", 0.0)
    page_html = '<a href="/docs/insurance-intermediaries-register.csv">Download</a>'
    attempts = {"page": 0, "download": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            if url == cbi_fetcher.CBI_REGISTER_PAGE:
                attempts["page"] += 1
                if attempts["page"] < 3:
                    raise httpx.ConnectTimeout("transient outage")
                return httpx.Response(200, request=httpx.Request("GET", url), text=page_html)
            attempts["download"] += 1
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                headers={"content-type": "text/csv"},
                content=b"Firm Name,Registered Address\nAcme,Dublin\n",
            )

    monkeypatch.setattr(cbi_fetcher.httpx, "Client", FakeClient)

    path = cbi_fetcher.fetch_cbi_register(output_dir=tmp_path)

    assert attempts["page"] == 3
    assert path.exists()
    assert path.name == "insurance-intermediaries-register.csv"


def test_download_cro_snapshot_falls_back_and_retries(tmp_path, monkeypatch):
    """CKAN API is hard-down (all retries exhausted); the fallback URL is
    transiently flaky. The snapshot must still be cached, with 3 attempts on
    the fallback call site."""
    monkeypatch.setattr(http_retry, "BASE_DELAY_SECONDS", 0.0)
    cache = tmp_path / "cro_snapshot.csv"
    monkeypatch.setattr(cro_resolver, "CRO_SNAPSHOT_CACHE", cache)
    attempts = {"ckan": 0, "fallback": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            if "/api/3/action/" in url:
                attempts["ckan"] += 1
                raise httpx.ConnectTimeout("ckan hard down")
            attempts["fallback"] += 1
            if attempts["fallback"] < 3:
                raise httpx.ConnectTimeout("transient outage")
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                content=b"company_num,company_name\n654321,ACME LIMITED\n",
            )

    monkeypatch.setattr(cro_resolver.httpx, "Client", FakeClient)

    cro_resolver._download_cro_snapshot()

    assert attempts["fallback"] == 3
    assert cache.exists()
    assert b"ACME LIMITED" in cache.read_bytes()


# ---------------------------------------------------------------------------
# cro_resolver
# ---------------------------------------------------------------------------

def test_exact_cro_number_match_merges_and_clears_review():
    firms = [_cbi_firm("Acme Insurance Brokers Limited", cro_number="654321")]
    snapshot = [_cro_row("654321", "ACME INSURANCE BROKERS LIMITED")]

    enriched = _resolve(firms, snapshot)[0]

    assert enriched["cro_number"] == "654321"
    assert enriched["cro_status"] == "Normal"
    assert enriched["incorporation_date"] == "2015-04-01"
    assert enriched["company_type"] == "Private Company Limited by Shares"
    assert enriched["cro_match_confidence"] == 1.0
    assert enriched["needs_review"] is False
    assert enriched["cro_match_method"] == "exact_cro_number"


def test_exact_normalised_name_match():
    # "Acme Insurance Brokers Limited" and "ACME INSURANCE BROKERS LTD" both
    # normalise to "ACME INSURANCE BROKERS" (suffix stripped, case folded).
    firms = [_cbi_firm("Acme Insurance Brokers Limited")]
    snapshot = [_cro_row("654321", "ACME INSURANCE BROKERS LTD")]

    enriched = _resolve(firms, snapshot)[0]

    assert enriched["cro_number"] == "654321"
    assert enriched["cro_match_confidence"] == 1.0
    assert enriched["needs_review"] is False
    assert enriched["cro_match_method"] == "exact_normalized_name"


def test_high_confidence_fuzzy_match_merges():
    # Verified WRatio: "ACME INSURANCE BROKERS IRELAND" vs
    # "ACME INSURANCE BROKERS" scores 95.0 (>= FUZZY_THRESHOLD 85, not exact).
    firms = [_cbi_firm("Acme Insurance Brokers Ireland")]
    snapshot = [_cro_row("654321", "ACME INSURANCE BROKERS LIMITED")]

    enriched = _resolve(firms, snapshot)[0]

    assert enriched["cro_number"] == "654321"
    assert enriched["needs_review"] is False
    assert enriched["cro_match_method"].startswith("fuzzy_name(score=")
    assert 0.85 <= enriched["cro_match_confidence"] <= 1.0


def test_low_confidence_fuzzy_match_does_not_merge_cro_data():
    """Fix #1 (CRITICAL) validation test -- FAILS until the fix lands.

    Verified WRatio: "CELTIC COVER INSURANCES" vs "CELTIC INSURANCE ADVISORS"
    scores 75.2 -- above the extractOne score_cutoff (60), below
    FUZZY_THRESHOLD (85). A match in this band is a *different company*:
    its registration data must NOT be merged into the firm record.
    """
    firms = [_cbi_firm("Celtic Cover Insurances")]
    snapshot = [_cro_row("220011", "CELTIC INSURANCE ADVISORS LIMITED", reg_date="2018-06-12")]

    enriched = _resolve(firms, snapshot)[0]

    assert enriched["cro_number"] is None
    assert enriched["cro_status"] is None
    assert enriched["incorporation_date"] is None
    assert enriched["needs_review"] is True
    assert enriched["cro_match_method"].startswith("fuzzy_name_low_confidence")


def test_no_fuzzy_candidate_above_cutoff_is_no_match():
    # Verified WRatio: 31.4 -- below extractOne's score_cutoff of 60.
    firms = [_cbi_firm("Zenith Maritime Consulting")]
    snapshot = [_cro_row("220011", "CELTIC INSURANCE ADVISORS LIMITED")]

    enriched = _resolve(firms, snapshot)[0]

    assert enriched["cro_number"] is None
    assert enriched["cro_status"] is None
    assert enriched["cro_match_confidence"] == 0.0
    assert enriched["needs_review"] is True
    assert enriched["cro_match_method"] == "no_match"


def test_empty_cro_snapshot_flags_all_firms():
    firms = [_cbi_firm("Acme Insurance Brokers Limited")]

    enriched = _resolve(firms, [])[0]

    assert enriched["cro_number"] is None
    assert enriched["cro_match_confidence"] == 0.0
    assert enriched["needs_review"] is True
    assert enriched["cro_match_method"] == "no_cro_snapshot"


# ---------------------------------------------------------------------------
# db.dal upsert dedup
# ---------------------------------------------------------------------------

def test_upsert_company_dedups_on_normalised_legal_name(tmp_db):
    """Fix #3 validation (per findings doc): without a cro_number, dedup must
    fold whitespace/case/legal-suffix differences — "Acme  Ltd" re-extracted
    as "ACME LTD" is the same company, not a new row."""
    with dal.get_db_connection(tmp_db) as conn:
        first = dal.upsert_company(conn, {"legal_name": "Acme  Ltd"}, "2026-07-20T00:00:00+00:00")
        second = dal.upsert_company(conn, {"legal_name": "ACME LTD"}, "2026-07-20T00:00:00+00:00")
        count = conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]

    assert first is True
    assert second is False
    assert count == 1


def test_resolve_against_cro_returns_degraded_flag_false_with_data():
    """Fix #2: resolve_against_cro returns (resolved, degraded) so callers can
    distinguish a healthy run from one where CRO enrichment never happened."""
    firms = [_cbi_firm("Acme Insurance Brokers Limited", cro_number="654321")]
    snapshot = [_cro_row("654321", "ACME INSURANCE BROKERS LIMITED")]

    result = resolve_against_cro(firms, snapshot)

    assert isinstance(result, tuple) and len(result) == 2
    resolved, degraded = result
    assert degraded is False
    assert resolved[0]["cro_number"] == "654321"


def test_resolve_against_cro_returns_degraded_flag_true_when_empty():
    firms = [_cbi_firm("Acme Insurance Brokers Limited")]

    result = resolve_against_cro(firms, [])

    assert isinstance(result, tuple) and len(result) == 2
    resolved, degraded = result
    assert degraded is True
    assert resolved[0]["needs_review"] is True


# ---------------------------------------------------------------------------
# runner (full pipeline, offline: fake register file + fresh CRO cache file)
# ---------------------------------------------------------------------------

def _write_register_csv(path: Path) -> None:
    path.write_text(
        "Firm Name,Registered Address\n"
        '"Acme Insurance Brokers Limited","12 Main Street, Dublin, D06 K4E5"\n'
        '"Celtic Cover Insurances","5 Quay Road, Galway, H91 AK12"\n',
        encoding="utf-8",
    )


def _write_cro_snapshot(path: Path) -> None:
    path.write_text(
        "company_num,company_name,company_status,company_reg_date\n"
        "654321,ACME INSURANCE BROKERS LIMITED,Normal,2015-04-01\n"
        "220011,CELTIC COVER INSURANCES LIMITED,Normal,2018-06-12\n",
        encoding="utf-8",
    )


def test_run_ingestion_happy_path_persists_and_logs(tmp_db, tmp_path, monkeypatch):
    reg = tmp_path / "cbi_register_2026-07-01.csv"
    _write_register_csv(reg)
    monkeypatch.setattr(runner, "fetch_cbi_register", lambda: reg)

    snap = tmp_path / "cro_snapshot.csv"
    _write_cro_snapshot(snap)  # just written => mtime fresh => no download attempt
    monkeypatch.setattr(cro_resolver, "CRO_SNAPSHOT_CACHE", snap)

    result = runner.run_ingestion()

    assert result["errors"] == []
    assert result["records_found"] == 2
    assert result["records_new"] == 2
    with dal.get_db_connection(tmp_db) as conn:
        companies = conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
        run_rows = conn.execute("SELECT errors FROM ingestion_runs").fetchall()
    assert companies == 2
    assert len(run_rows) == 1
    assert json.loads(run_rows[0]["errors"]) == []


def test_run_ingestion_second_run_deduplicates(tmp_db, tmp_path, monkeypatch):
    reg = tmp_path / "cbi_register_2026-07-01.csv"
    _write_register_csv(reg)
    monkeypatch.setattr(runner, "fetch_cbi_register", lambda: reg)

    snap = tmp_path / "cro_snapshot.csv"
    _write_cro_snapshot(snap)
    monkeypatch.setattr(cro_resolver, "CRO_SNAPSHOT_CACHE", snap)

    first = runner.run_ingestion()
    second = runner.run_ingestion()

    assert first["records_new"] == 2
    assert second["records_new"] == 0
    with dal.get_db_connection(tmp_db) as conn:
        companies = conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
    assert companies == 2


def test_run_ingestion_reports_degraded_cro_as_error(tmp_db, tmp_path, monkeypatch):
    """Fix #2 validation (per findings doc): CRO snapshot unavailable => the run
    still completes degraded, but the failure must reach result["errors"] AND
    the ingestion_runs audit row — never report a degraded run as clean."""
    reg = tmp_path / "cbi_register_2026-07-01.csv"
    _write_register_csv(reg)
    monkeypatch.setattr(runner, "fetch_cbi_register", lambda: reg)

    missing_cache = tmp_path / "cro_snapshot.csv"  # deliberately never created
    monkeypatch.setattr(cro_resolver, "CRO_SNAPSHOT_CACHE", missing_cache)
    # Simulate the download failing the way it really does: prints a warning,
    # caches nothing, raises nothing.
    monkeypatch.setattr(cro_resolver, "_download_cro_snapshot", lambda: None)

    result = runner.run_ingestion()

    assert any("CRO snapshot unavailable" in e for e in result["errors"])
    assert result["records_found"] == 2  # degraded, not aborted
    with dal.get_db_connection(tmp_db) as conn:
        run_row = conn.execute("SELECT errors FROM ingestion_runs").fetchone()
        payloads = [
            r["raw_payload"]
            for r in conn.execute("SELECT raw_payload FROM companies").fetchall()
        ]
    assert json.loads(run_row["errors"]) != []
    assert len(payloads) == 2
    assert all(json.loads(p)["needs_review"] is True for p in payloads)


def test_run_ingestion_isolates_parse_failure_and_logs_it(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "fetch_cbi_register", lambda: tmp_path / "register.csv")

    def boom(path):
        raise RuntimeError("simulated parse failure")

    monkeypatch.setattr(runner, "parse_cbi_register", boom)

    result = runner.run_ingestion()

    assert result["records_found"] == 0
    assert result["records_new"] == 0
    assert any("CBI parse failed" in e for e in result["errors"])
    with dal.get_db_connection(tmp_db) as conn:
        row = conn.execute("SELECT errors FROM ingestion_runs").fetchone()
    assert "CBI parse failed" in row["errors"]
