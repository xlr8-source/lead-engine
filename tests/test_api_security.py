"""
tests/test_api_security.py

Three fail-open defaults on the HTTP surface:

  - `POST /api/disenrich-all` ran `DELETE FROM enrichment` + `DELETE FROM
    outreach_emails` — every assessment and every draft, unrecoverable — from
    a single unauthenticated POST with no body and no confirmation.
  - CORS was `allow_origins=["*"]` with `allow_credentials=True`, so any page
    in the operator's browser could issue that POST.
  - `CORS_ORIGINS` was documented in .env.example and read by nothing.

Auth is opt-in: with no API_KEY set the app behaves exactly as before, so a
localhost-only install is unaffected. Setting API_KEY turns it on.
"""
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from api.security import cors_origins, get_api_key


@pytest.fixture()
def client(monkeypatch):
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    @contextmanager
    def fake_conn():
        yield None

    monkeypatch.setattr(api_main, "get_db_connection", fake_conn)
    monkeypatch.setattr(api_main, "clear_all_enrichments", lambda: 7)
    return TestClient(api_main.app)


# ---------------------------------------------------------------------------
# CORS configuration
# ---------------------------------------------------------------------------

def test_cors_defaults_to_no_cross_origin_access(monkeypatch):
    """The dashboard is served same-origin by this very app, so it needs no
    CORS grant at all. The default must not hand one to everybody."""
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert cors_origins() == []


def test_cors_origins_env_is_actually_read(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000, https://app.example.ie")
    assert cors_origins() == ["http://localhost:3000", "https://app.example.ie"]


def test_cors_never_returns_a_wildcard(monkeypatch):
    """`*` with allow_credentials=True is rejected by browsers anyway, and
    signals an intent this app should not have."""
    monkeypatch.setenv("CORS_ORIGINS", "*")
    assert "*" not in cors_origins()


# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------

def test_api_key_is_absent_by_default(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    assert get_api_key() is None


def test_blank_api_key_counts_as_unset(monkeypatch):
    """API_KEY= in a .env must not enable auth with the empty string as the
    valid credential."""
    monkeypatch.setenv("API_KEY", "   ")
    assert get_api_key() is None


# ---------------------------------------------------------------------------
# Destructive endpoint
# ---------------------------------------------------------------------------

def test_disenrich_all_refuses_a_bare_post(client, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    resp = client.post("/api/disenrich-all", json={})
    assert resp.status_code == 400
    assert "confirm" in resp.text.lower()


def test_disenrich_all_refuses_a_wrong_confirmation(client, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    resp = client.post("/api/disenrich-all", json={"confirm": "yes"})
    assert resp.status_code == 400


def test_disenrich_all_proceeds_with_the_exact_confirmation(client, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    resp = client.post("/api/disenrich-all", json={"confirm": "DELETE ALL ENRICHMENTS"})
    assert resp.status_code == 200
    assert resp.json()["disenriched"] == 7


# ---------------------------------------------------------------------------
# Opt-in auth
# ---------------------------------------------------------------------------

def test_mutating_endpoints_stay_open_when_no_key_is_configured(client, monkeypatch):
    """Non-breaking default: an existing localhost install keeps working."""
    monkeypatch.delenv("API_KEY", raising=False)
    resp = client.post("/api/disenrich-all", json={"confirm": "DELETE ALL ENRICHMENTS"})
    assert resp.status_code == 200


def test_mutating_endpoint_rejects_a_missing_key_once_configured(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    resp = client.post("/api/disenrich-all", json={"confirm": "DELETE ALL ENRICHMENTS"})
    assert resp.status_code == 401


def test_mutating_endpoint_rejects_a_wrong_key(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    resp = client.post(
        "/api/disenrich-all",
        json={"confirm": "DELETE ALL ENRICHMENTS"},
        headers={"X-API-Key": "guess"},
    )
    assert resp.status_code == 401


def test_mutating_endpoint_accepts_the_configured_key(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    resp = client.post(
        "/api/disenrich-all",
        json={"confirm": "DELETE ALL ENRICHMENTS"},
        headers={"X-API-Key": "s3cret"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Client log ingestion
# ---------------------------------------------------------------------------

def test_client_log_line_cannot_forge_extra_log_entries(client, monkeypatch):
    """events.log is the primary operational record. Newlines in `msg` wrote
    entries that read exactly like genuine server events."""
    monkeypatch.delenv("API_KEY", raising=False)
    import api.main as api_main

    written = []
    monkeypatch.setattr(api_main, "_log_event", lambda m: written.append(m))

    client.post("/api/log", json={"msg": "click\n[12:00:00.000] ASSESS SUCCESS company=fake"})

    # The request-logging middleware writes REQ/RES lines around this, so
    # isolate the client-supplied one rather than indexing blindly.
    frontend_lines = [m for m in written if m.startswith("FRONTEND")]
    assert len(frontend_lines) == 1
    assert "\n" not in frontend_lines[0]
    assert "\r" not in frontend_lines[0]


def test_client_log_line_is_length_capped(client, monkeypatch):
    """An uncapped append is a disk-fill primitive on an unauthenticated
    endpoint."""
    monkeypatch.delenv("API_KEY", raising=False)
    import api.main as api_main

    written = []
    monkeypatch.setattr(api_main, "_log_event", lambda m: written.append(m))

    client.post("/api/log", json={"msg": "A" * 50_000})

    frontend_lines = [m for m in written if m.startswith("FRONTEND")]
    assert len(frontend_lines[0]) < 1_000


def test_read_endpoints_are_never_gated_by_the_api_key(client, monkeypatch):
    """The key protects writes. Gating reads too would break the dashboard
    the moment a key is set, which is not what this change is for."""
    monkeypatch.setenv("API_KEY", "s3cret")
    import api.main as api_main
    monkeypatch.setattr(api_main, "get_company_detail", lambda cid: None)
    resp = client.get("/api/leads/does-not-exist")
    assert resp.status_code == 404  # reached the handler, not blocked by auth
