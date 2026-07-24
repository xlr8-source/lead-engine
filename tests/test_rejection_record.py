"""
tests/test_rejection_record.py

Under GUARD_ENFORCEMENT=block a rejected assessment wrote nothing anywhere.
It logged, appended to events.log, and marked the run failed in RunRegistry —
which is in-memory, so a restart erased every trace. The company simply
stayed unassessed with no recorded reason, which from the dashboard is
indistinguishable from "the Assess button doesn't work".

A rejection is a real outcome and belongs in the database next to the
assessments that succeeded.
"""
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_leads.db"
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_file)
    conn.executescript(schema)
    for cid, name in (("c1", "Acme Insurance Limited"), ("c2", "Beta Brokers Limited")):
        conn.execute(
            "INSERT INTO companies (id, legal_name, source, ingested_at) VALUES (?, ?, ?, ?)",
            (cid, name, "cbi_register", "2026-07-24T00:00:00Z"),
        )
    conn.commit()
    conn.close()

    import db.dal as dal_mod
    monkeypatch.setattr(dal_mod, "_DB_PATH", db_file)
    return db_file


def _rejected_enrichment(company_id: str = "c1") -> dict:
    return {
        "company_id": company_id,
        "qualification_score": 68,
        "guard_passed": False,
        "guard_score": 22.0,
        "guard_failures": ["EG-CONF-002"],
        "llm_model": "test-model",
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_rejection_is_persisted_with_its_reason_and_failed_guards(temp_db):
    import db.dal as dal_mod

    with dal_mod.get_db_connection() as conn:
        dal_mod.record_rejection(
            conn, "c1",
            reason="Rejected by guard(s): EG-CONF-002 (GUARD_ENFORCEMENT=block).",
            guard_failures=["EG-CONF-002"],
            guard_score=22.0,
            llm_model="test-model",
        )

    # Fresh connection — proves this outlives the process, unlike RunRegistry.
    with dal_mod.get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM assessment_rejections WHERE company_id = 'c1'"
        ).fetchone()

    assert row is not None
    assert "EG-CONF-002" in row["reason"]
    assert json.loads(row["guard_failures"]) == ["EG-CONF-002"]
    assert row["guard_score"] == 22.0
    assert row["rejected_at"]


def test_a_company_keeps_only_its_latest_rejection(temp_db):
    """Re-assessing a firm that fails again should not stack duplicate rows."""
    import db.dal as dal_mod

    with dal_mod.get_db_connection() as conn:
        dal_mod.record_rejection(conn, "c1", reason="first", guard_failures=["EG-CONF-002"])
        dal_mod.record_rejection(conn, "c1", reason="second", guard_failures=["EG-QUAL-001"])
        rows = conn.execute(
            "SELECT reason FROM assessment_rejections WHERE company_id = 'c1'"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["reason"] == "second"


def test_a_successful_assessment_clears_the_stale_rejection(temp_db):
    """Otherwise a firm that failed once, then passed on re-assessment, would
    show as both assessed and rejected forever."""
    import db.dal as dal_mod

    with dal_mod.get_db_connection() as conn:
        dal_mod.record_rejection(conn, "c1", reason="was rejected", guard_failures=["EG-CONF-002"])
        dal_mod.upsert_enrichment(conn, {
            "company_id": "c1", "qualification_score": 71, "guard_passed": True, "guard_score": 95.0,
        })
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM assessment_rejections WHERE company_id = 'c1'"
        ).fetchone()["n"]

    assert remaining == 0


# ---------------------------------------------------------------------------
# Query surface
# ---------------------------------------------------------------------------

def test_company_detail_exposes_the_rejection(temp_db):
    import db.dal as dal_mod

    with dal_mod.get_db_connection() as conn:
        dal_mod.record_rejection(conn, "c1", reason="Rejected by guard(s): EG-CONF-002.",
                                 guard_failures=["EG-CONF-002"], guard_score=22.0)

    detail = dal_mod.get_company_detail("c1")

    assert detail["rejection"] is not None
    assert "EG-CONF-002" in detail["rejection"]["reason"]
    assert detail["rejection"]["guard_failures"] == ["EG-CONF-002"]


def test_lead_list_row_shows_a_firm_was_rejected_not_merely_unassessed(temp_db):
    """The whole point: 'we tried and dropped it, here is why' must be
    distinguishable from 'nobody has looked at this yet'."""
    import db.dal as dal_mod

    with dal_mod.get_db_connection() as conn:
        dal_mod.record_rejection(conn, "c1", reason="Rejected by guard(s): EG-CONF-002.",
                                 guard_failures=["EG-CONF-002"], guard_score=22.0)

    rows = {r["id"]: r for r in dal_mod.get_companies()}

    assert rows["c1"]["rejected_at"] is not None
    assert "EG-CONF-002" in rows["c1"]["rejection_reason"]
    assert rows["c2"]["rejected_at"] is None      # never attempted
    assert rows["c2"]["rejection_reason"] is None


# ---------------------------------------------------------------------------
# API wiring
# ---------------------------------------------------------------------------

def _api(monkeypatch, recorded):
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main

    @contextmanager
    def fake_conn():
        yield None

    monkeypatch.setattr(api_main, "get_unenriched_companies",
                        lambda: [{"id": "c1", "legal_name": "Acme Insurance Limited"}])
    monkeypatch.setattr(api_main, "assess_company",
                        lambda company, on_event=None: _rejected_enrichment())
    monkeypatch.setattr(api_main, "get_db_connection", fake_conn)
    monkeypatch.setattr(api_main, "upsert_enrichment", lambda conn, e: None)
    monkeypatch.setattr(api_main, "record_rejection",
                        lambda conn, cid, **kw: recorded.append((cid, kw)))
    return api_main


def test_block_mode_records_the_rejection_through_the_api(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GUARD_ENFORCEMENT", "block")
    monkeypatch.delenv("API_KEY", raising=False)
    recorded = []
    api_main = _api(monkeypatch, recorded)

    resp = TestClient(api_main.app).post("/api/enrich-all")

    assert resp.status_code == 200
    assert len(recorded) == 1
    company_id, kwargs = recorded[0]
    assert company_id == "c1"
    assert "EG-CONF-002" in kwargs["reason"]
    assert kwargs["guard_failures"] == ["EG-CONF-002"]


def test_a_failure_to_record_the_rejection_does_not_reclassify_the_outcome(monkeypatch):
    """The rejection already happened. If the audit write fails, that's a
    logging problem — it must not turn a deliberate quality decision into a
    reported crash."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GUARD_ENFORCEMENT", "block")
    monkeypatch.delenv("API_KEY", raising=False)
    api_main = _api(monkeypatch, [])

    def boom(conn, cid, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(api_main, "record_rejection", boom)

    resp = TestClient(api_main.app).post("/api/enrich-all")

    assert resp.status_code == 200
    body = resp.json()
    assert body["rejected"] == 1
    assert body["failed"] == 0


def test_warn_mode_records_no_rejection(monkeypatch):
    """Nothing was rejected, so nothing should be recorded as rejected."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GUARD_ENFORCEMENT", "warn")
    monkeypatch.delenv("API_KEY", raising=False)
    recorded = []
    api_main = _api(monkeypatch, recorded)

    resp = TestClient(api_main.app).post("/api/enrich-all")

    assert resp.status_code == 200
    assert recorded == []
