"""
tests/test_bulk_run.py

POST /api/enrich-all was a synchronous request that iterated every unenriched
company inline. At ~2,400 register records and ~20s per assessment with a
concurrency of 3, that is a multi-hour HTTP request — no browser, proxy or
load balancer holds one open, so the run always died by timeout with no
record of how far it got. There was also no way to stop it once started, and
the `limit` query parameter was applied by slicing the list in Python after
loading every row.

This pins the background-run contract:
  - the endpoint returns immediately with a run_id
  - progress is observable while it runs
  - it can be cancelled, and cancelling stops handing out new work
  - `limit` reaches SQL
"""
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

BULK_KIND = "enrich_all"


# ---------------------------------------------------------------------------
# Registry: cancellation + progress
# ---------------------------------------------------------------------------

def test_registry_reports_a_run_as_not_cancelled_by_default():
    from engine.activity import RunRegistry
    reg = RunRegistry()
    run_id = reg.start_run("__all__", BULK_KIND)
    assert reg.is_cancelled(run_id) is False


def test_registry_marks_a_run_cancelled_on_request():
    from engine.activity import RunRegistry
    reg = RunRegistry()
    run_id = reg.start_run("__all__", BULK_KIND)
    assert reg.request_cancel(run_id) is True
    assert reg.is_cancelled(run_id) is True


def test_cancelling_an_unknown_run_is_reported_not_raised():
    from engine.activity import RunRegistry
    reg = RunRegistry()
    assert reg.request_cancel("no-such-run") is False


def test_registry_tracks_progress_counters():
    from engine.activity import RunRegistry
    reg = RunRegistry()
    run_id = reg.start_run("__all__", BULK_KIND)

    reg.set_progress(run_id, total=10)
    reg.bump_progress(run_id, done=1)
    reg.bump_progress(run_id, done=1, failed=1)

    status = reg.get_run_status(run_id)
    assert status["progress"]["total"] == 10
    assert status["progress"]["done"] == 2
    assert status["progress"]["failed"] == 1


def test_a_finished_run_is_not_reported_as_cancelled():
    """finish_run must not leave a stale cancel flag that a later lookup of
    the same id would read."""
    from engine.activity import RunRegistry
    reg = RunRegistry()
    run_id = reg.start_run("__all__", BULK_KIND)
    reg.finish_run(run_id, "complete")
    assert reg.get_run_status(run_id)["status"] == "complete"


# ---------------------------------------------------------------------------
# DAL: limit reaches SQL
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    import sqlite3
    db_file = tmp_path / "bulk.db"
    conn = sqlite3.connect(db_file)
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    for i in range(25):
        conn.execute(
            "INSERT INTO companies (id, legal_name, cro_status, source, ingested_at) "
            "VALUES (?,?,?,?,?)",
            (f"c{i:02d}", f"Firm {i} Limited", "Normal", "cbi_register",
             f"2026-07-24T00:00:{i:02d}Z"),
        )
    conn.commit()
    conn.close()
    import db.dal as dal_mod
    monkeypatch.setattr(dal_mod, "_DB_PATH", db_file)
    return db_file


def test_unenriched_query_applies_the_limit_in_sql(temp_db):
    """Was: load every row, then slice in Python."""
    import db.dal as dal_mod
    rows = dal_mod.get_unenriched_companies(limit=5)
    assert len(rows) == 5


def test_unenriched_query_without_a_limit_returns_everything(temp_db):
    import db.dal as dal_mod
    assert len(dal_mod.get_unenriched_companies()) == 25


def test_a_zero_or_negative_limit_is_ignored_rather_than_returning_nothing(temp_db):
    """`?limit=0` must not silently mean "assess nothing" — the endpoint
    already treats 0 as unset."""
    import db.dal as dal_mod
    assert len(dal_mod.get_unenriched_companies(limit=0)) == 25


# ---------------------------------------------------------------------------
# Endpoint: returns immediately
# ---------------------------------------------------------------------------

@pytest.fixture()
def api(monkeypatch):
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main

    @contextmanager
    def fake_conn():
        yield None

    monkeypatch.setattr(api_main, "get_db_connection", fake_conn)
    monkeypatch.setattr(api_main, "upsert_enrichment", lambda conn, e: None)
    monkeypatch.setattr(api_main, "record_rejection", lambda conn, cid, **kw: None)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    monkeypatch.setenv("GUARD_ENFORCEMENT", "warn")
    api_main.run_registry._runs.clear()
    api_main.run_registry._active.clear()
    return api_main


def test_enrich_all_returns_a_run_id_without_waiting_for_the_work(api, monkeypatch):
    """The whole point: the response must not wait on the assessments."""
    from fastapi.testclient import TestClient

    companies = [{"id": f"c{i}", "legal_name": f"Firm {i}"} for i in range(4)]
    monkeypatch.setattr(api, "get_unenriched_companies", lambda limit=None: companies)
    monkeypatch.setattr(api, "assess_company",
                        lambda c, on_event=None: (time.sleep(0.5) or {"company_id": c["id"], "guard_passed": True}))

    started = time.perf_counter()
    resp = TestClient(api.app).post("/api/enrich-all")
    elapsed = time.perf_counter() - started

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"]
    assert body["status"] == "running"
    assert elapsed < 1.0, f"endpoint blocked for {elapsed:.1f}s — it is still synchronous"


def test_enrich_all_reports_progress_while_running(api, monkeypatch):
    from fastapi.testclient import TestClient

    companies = [{"id": f"c{i}", "legal_name": f"Firm {i}"} for i in range(6)]
    monkeypatch.setattr(api, "get_unenriched_companies", lambda limit=None: companies)
    monkeypatch.setattr(api, "assess_company",
                        lambda c, on_event=None: (time.sleep(0.1) or {"company_id": c["id"], "guard_passed": True}))

    client = TestClient(api.app)
    run_id = client.post("/api/enrich-all").json()["run_id"]

    deadline = time.time() + 20
    while time.time() < deadline:
        status = client.get(f"/api/runs/{run_id}").json()
        if status["status"] in ("complete", "failed"):
            break
        time.sleep(0.2)

    final = client.get(f"/api/runs/{run_id}").json()
    assert final["status"] == "complete"
    assert final["progress"]["total"] == 6
    assert final["progress"]["done"] == 6


def test_only_one_bulk_run_at_a_time(api, monkeypatch):
    """A second click must join the existing run, not start a parallel one
    that doubles the API spend."""
    from fastapi.testclient import TestClient

    companies = [{"id": f"c{i}", "legal_name": f"Firm {i}"} for i in range(4)]
    monkeypatch.setattr(api, "get_unenriched_companies", lambda limit=None: companies)
    monkeypatch.setattr(api, "assess_company",
                        lambda c, on_event=None: (time.sleep(0.4) or {"company_id": c["id"], "guard_passed": True}))

    client = TestClient(api.app)
    first = client.post("/api/enrich-all").json()["run_id"]
    second = client.post("/api/enrich-all").json()["run_id"]
    assert first == second


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancelling_a_bulk_run_stops_new_assessments(api, monkeypatch):
    """Cooperative cancel: in-flight work finishes, nothing new starts. The
    endpoint spends real money per company, so 'stop' has to actually stop."""
    from fastapi.testclient import TestClient

    companies = [{"id": f"c{i}", "legal_name": f"Firm {i}"} for i in range(40)]
    monkeypatch.setattr(api, "get_unenriched_companies", lambda limit=None: companies)

    seen = []
    lock = threading.Lock()

    def slow_assess(c, on_event=None):
        with lock:
            seen.append(c["id"])
        time.sleep(0.15)
        return {"company_id": c["id"], "guard_passed": True}

    monkeypatch.setattr(api, "assess_company", slow_assess)

    client = TestClient(api.app)
    run_id = client.post("/api/enrich-all").json()["run_id"]

    time.sleep(0.5)
    resp = client.post(f"/api/runs/{run_id}/cancel")
    assert resp.status_code == 200

    with lock:
        at_cancel = len(seen)

    deadline = time.time() + 20
    while time.time() < deadline:
        if client.get(f"/api/runs/{run_id}").json()["status"] in ("complete", "failed", "cancelled"):
            break
        time.sleep(0.2)

    with lock:
        total = len(seen)

    assert total < 40, "cancel did not stop the run"
    assert total - at_cancel <= 6, f"{total - at_cancel} assessments started after cancel"
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "cancelled"


def test_cancelling_an_unknown_run_returns_404(api):
    from fastapi.testclient import TestClient
    assert TestClient(api.app).post("/api/runs/nope/cancel").status_code == 404


def test_cancel_is_a_write_and_is_refused_in_read_only_mode(api, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("READ_ONLY_MODE", "1")
    assert TestClient(api.app).post("/api/runs/anything/cancel").status_code == 403
