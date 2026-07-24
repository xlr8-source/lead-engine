"""
tests/test_guard_enforcement.py

The guard pipeline computed a verdict, wrote it to a column, rendered it in
the UI — and gated nothing. A hard-failing assessment (including one whose
own guard reason read "not reliable enough to store") was persisted exactly
like a passing one.

These tests pin the enforcement contract:
  GUARD_ENFORCEMENT=off    guards are not run; no verdict is recorded
  GUARD_ENFORCEMENT=warn   guards run, verdict recorded, storage proceeds
  GUARD_ENFORCEMENT=block  guards run, verdict recorded, hard failure blocks
"""
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.governor.enforcement import (
    DEFAULT_MODE,
    ENFORCEMENT_MODES,
    evaluate_storage,
    get_enforcement_mode,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _passing_enrichment() -> dict:
    return {
        "company_id": "c1",
        "qualification_score": 72,
        "guard_passed": True,
        "guard_score": 91.0,
        "guard_failures": [],
    }


def _failing_enrichment() -> dict:
    return {
        "company_id": "c2",
        "qualification_score": 68,
        "guard_passed": False,
        "guard_score": 22.0,
        "guard_failures": ["EG-CONF-002"],
    }


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

def test_mode_defaults_to_warn_when_unset(monkeypatch):
    """Absent config must not silently change existing behaviour."""
    monkeypatch.delenv("GUARD_ENFORCEMENT", raising=False)
    assert get_enforcement_mode() == "warn"
    assert DEFAULT_MODE == "warn"


def test_unrecognised_mode_falls_back_to_warn(monkeypatch):
    """A typo'd mode must not read as 'off' and silently disable the guards."""
    monkeypatch.setenv("GUARD_ENFORCEMENT", "aggressive")
    assert get_enforcement_mode() == "warn"


def test_mode_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("GUARD_ENFORCEMENT", "  BLOCK ")
    assert get_enforcement_mode() == "block"


def test_all_three_modes_are_recognised(monkeypatch):
    for mode in ("off", "warn", "block"):
        monkeypatch.setenv("GUARD_ENFORCEMENT", mode)
        assert get_enforcement_mode() == mode
    assert set(ENFORCEMENT_MODES) == {"off", "warn", "block"}


# ---------------------------------------------------------------------------
# Storage decision
# ---------------------------------------------------------------------------

def test_warn_mode_stores_a_guard_failing_assessment(monkeypatch):
    """warn is today's behaviour — the verdict is advisory, nothing is lost."""
    monkeypatch.setenv("GUARD_ENFORCEMENT", "warn")
    decision = evaluate_storage(_failing_enrichment())
    assert decision.store is True
    assert decision.reason is None


def test_block_mode_refuses_to_store_a_guard_failing_assessment(monkeypatch):
    """The test that would have caught the disconnected governor."""
    monkeypatch.setenv("GUARD_ENFORCEMENT", "block")
    decision = evaluate_storage(_failing_enrichment())
    assert decision.store is False
    assert "EG-CONF-002" in decision.reason


def test_block_mode_stores_a_guard_passing_assessment(monkeypatch):
    monkeypatch.setenv("GUARD_ENFORCEMENT", "block")
    decision = evaluate_storage(_passing_enrichment())
    assert decision.store is True
    assert decision.reason is None


def test_block_mode_refuses_an_assessment_with_no_verdict_at_all(monkeypatch):
    """Fail closed: a dict carrying no guard verdict has not been shown to be
    safe, so block mode must not treat the absence as a pass."""
    monkeypatch.setenv("GUARD_ENFORCEMENT", "block")
    decision = evaluate_storage({"company_id": "c3", "qualification_score": 50})
    assert decision.store is False
    assert "no guard verdict" in decision.reason.lower()


def test_off_mode_stores_regardless_of_verdict(monkeypatch):
    monkeypatch.setenv("GUARD_ENFORCEMENT", "off")
    assert evaluate_storage(_failing_enrichment()).store is True


# ---------------------------------------------------------------------------
# assess_company integration
# ---------------------------------------------------------------------------

def _stub_assessment(monkeypatch):
    """Point assess_company at deterministic research + LLM output."""
    import engine.assessor as assessor_mod

    monkeypatch.setattr(
        assessor_mod, "research_company",
        lambda company, **kw: {
            "website_text": (
                "Dublin insurance broker offering commercial cover, claims "
                "handling and premium finance to Irish businesses since 1998."
            ),
            "search_results": [{"url": "https://example.ie"}],
            "linkedin_results": [],
            "social_links": {},
        },
    )
    monkeypatch.setattr(
        assessor_mod, "summarise",
        lambda company, research: {
            "qualification_score": 61,
            "signal_strength": "medium",
            "executive_summary": (
                "A CBI-authorised Dublin insurance broker with a live website "
                "and commercial lines focus identified from the register."
            ),
            "recommended_angle": "Ask about premium reconciliation.",
            "opening_angle": "Multi-provider brokers lose sight of premium status.",
            "contacts": None,
        },
    )
    return assessor_mod


def test_off_mode_skips_the_guard_pipeline_entirely(monkeypatch):
    """off is an escape hatch, not a silent pass — the verdict must be absent
    (None), never a fabricated True, so /api/guard-stats can exclude it."""
    assessor_mod = _stub_assessment(monkeypatch)
    monkeypatch.setenv("GUARD_ENFORCEMENT", "off")

    calls = []
    monkeypatch.setattr(
        assessor_mod, "run_guards",
        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("guards ran")),
    )

    result = assessor_mod.assess_company({"id": "c1", "legal_name": "Acme Insurance Limited"})

    assert calls == []
    assert result["guard_passed"] is None
    assert result["guard_score"] is None


def test_warn_mode_still_runs_guards_and_records_a_verdict(monkeypatch):
    assessor_mod = _stub_assessment(monkeypatch)
    monkeypatch.setenv("GUARD_ENFORCEMENT", "warn")

    result = assessor_mod.assess_company({"id": "c1", "legal_name": "Acme Insurance Limited"})

    assert result["guard_passed"] in (True, False)
    assert isinstance(result["guard_score"], float)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_leads.db"
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_file)
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO companies (id, legal_name, source, ingested_at) VALUES (?, ?, ?, ?)",
        ("c1", "Acme Insurance Limited", "cbi_register", "2026-07-24T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    import db.dal as dal_mod
    monkeypatch.setattr(dal_mod, "_DB_PATH", db_file)
    return db_file


def test_upsert_enrichment_defaults_guard_passed_to_false_when_absent(temp_db):
    """Fail closed. An enrichment dict carrying no guard verdict has not been
    shown to have passed anything, so it must not be recorded as a pass —
    that biased /api/guard-stats toward reporting a clean system."""
    import db.dal as dal_mod

    with dal_mod.get_db_connection() as conn:
        dal_mod.upsert_enrichment(conn, {
            "company_id": "c1",
            "qualification_score": 61,
        })
        row = conn.execute(
            "SELECT guard_passed FROM enrichment WHERE company_id = 'c1'"
        ).fetchone()

    assert row["guard_passed"] == 0


def test_schema_default_for_guard_passed_is_not_a_pass(temp_db):
    """The DDL default was 1 — a row inserted by any path that didn't supply
    the column read as 'guards passed'. Belt-and-braces behind the DAL."""
    import db.dal as dal_mod

    with dal_mod.get_db_connection() as conn:
        conn.execute(
            "INSERT INTO enrichment (id, company_id, generated_at) VALUES (?, ?, ?)",
            ("e-raw", "c1", "2026-07-24T00:00:00Z"),
        )
        row = conn.execute(
            "SELECT guard_passed FROM enrichment WHERE id = 'e-raw'"
        ).fetchone()

    assert row["guard_passed"] == 0


def test_upsert_enrichment_preserves_an_absent_guard_verdict_as_null(temp_db):
    """off-mode records must be NULL, not 1 — /api/guard-stats filters on
    `guard_passed IS NOT NULL`, so a coerced 0 or 1 would pollute the pass
    rate with assessments that were never checked."""
    import db.dal as dal_mod

    with dal_mod.get_db_connection() as conn:
        dal_mod.upsert_enrichment(conn, {
            "company_id": "c1",
            "qualification_score": 61,
            "guard_passed": None,
            "guard_score": None,
        })
        row = conn.execute(
            "SELECT guard_passed, guard_score FROM enrichment WHERE company_id = 'c1'"
        ).fetchone()

    assert row["guard_passed"] is None
    assert row["guard_score"] is None


# ---------------------------------------------------------------------------
# API integration — the end-to-end proof
# ---------------------------------------------------------------------------

def test_block_mode_does_not_write_a_guard_failing_assessment(monkeypatch):
    """End to end: a hard-failing assessment must never reach upsert_enrichment."""
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GUARD_ENFORCEMENT", "block")
    monkeypatch.setattr(api_main, "get_unenriched_companies",
                        lambda: [{"id": "c1", "legal_name": "Acme Insurance Limited"}])
    monkeypatch.setattr(api_main, "assess_company",
                        lambda company, on_event=None: _failing_enrichment())

    stored = []

    @contextmanager
    def fake_conn():
        yield None

    monkeypatch.setattr(api_main, "get_db_connection", fake_conn)
    monkeypatch.setattr(api_main, "upsert_enrichment", lambda conn, e: stored.append(e))

    resp = TestClient(api_main.app).post("/api/enrich-all")

    assert resp.status_code == 200
    assert stored == [], "a guard-failing assessment was persisted in block mode"
    body = resp.json()
    assert body["rejected"] == 1
    assert body["enriched"] == 0


def test_warn_mode_still_writes_a_guard_failing_assessment(monkeypatch):
    """Regression guard on the default: enabling the feature must not change
    behaviour for anyone who hasn't opted in."""
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GUARD_ENFORCEMENT", "warn")
    monkeypatch.setattr(api_main, "get_unenriched_companies",
                        lambda: [{"id": "c1", "legal_name": "Acme Insurance Limited"}])
    monkeypatch.setattr(api_main, "assess_company",
                        lambda company, on_event=None: _failing_enrichment())

    stored = []

    @contextmanager
    def fake_conn():
        yield None

    monkeypatch.setattr(api_main, "get_db_connection", fake_conn)
    monkeypatch.setattr(api_main, "upsert_enrichment", lambda conn, e: stored.append(e))

    resp = TestClient(api_main.app).post("/api/enrich-all")

    assert resp.status_code == 200
    assert len(stored) == 1
    assert resp.json()["enriched"] == 1
    assert resp.json()["rejected"] == 0
