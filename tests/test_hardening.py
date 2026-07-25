"""
tests/test_hardening.py

Security hardening for deployments where the app is not guaranteed to be
localhost-only and single-user.

Covers, by finding ID:
  B4  fail closed when bound to a non-loopback host with no API_KEY
  B5  interactive API docs off by default
  B2  .env loaded from an explicit absolute path, not by scanning repo root
  R1  llm_raw_response / raw_payload never leave the API; READ_ONLY_MODE
      redacts contact PII and refuses writes
  R2  SSRF egress boundary in the researcher's fetch path
  R5  /api/log rate limited; events.log size-capped
"""
import ipaddress
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# B4 — fail closed on a non-loopback bind with no API_KEY
# ---------------------------------------------------------------------------

def test_loopback_bind_without_key_is_allowed(monkeypatch):
    """The single-developer localhost case must keep working untouched."""
    from api.security import assert_safe_bind
    monkeypatch.delenv("API_KEY", raising=False)
    assert_safe_bind("127.0.0.1")   # must not raise


def test_non_loopback_bind_without_key_refuses_to_start(monkeypatch):
    """A non-loopback bind with no key means every mutating endpoint is open to
    anyone who can reach the port. Make the configuration unreachable rather
    than merely discouraged."""
    from api.security import assert_safe_bind
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        assert_safe_bind("0.0.0.0")
    assert "API_KEY" in str(exc.value)


def test_non_loopback_bind_with_key_is_allowed(monkeypatch):
    from api.security import assert_safe_bind
    monkeypatch.setenv("API_KEY", "s3cret")
    assert_safe_bind("0.0.0.0")     # must not raise


def test_ipv6_loopback_counts_as_loopback(monkeypatch):
    from api.security import assert_safe_bind
    monkeypatch.delenv("API_KEY", raising=False)
    assert_safe_bind("::1")         # must not raise


def test_bind_host_is_read_from_api_host_env(monkeypatch):
    from api.security import resolve_bind_host
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    assert resolve_bind_host() == "0.0.0.0"


def test_bind_host_falls_back_to_uvicorn_cli_flag(monkeypatch):
    """`uvicorn --host 0.0.0.0` never sets API_HOST, and that is how a
    non-loopback deployment is typically started."""
    from api.security import resolve_bind_host
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.setattr(sys, "argv", ["uvicorn", "api.main:app", "--host", "0.0.0.0"])
    assert resolve_bind_host() == "0.0.0.0"


def test_cli_host_wins_over_a_stale_api_host_env(monkeypatch):
    """The real failure: .env says API_HOST=127.0.0.1 (the shipped default)
    while the server is actually started with `uvicorn --host 0.0.0.0`.
    The env var is a declaration; the CLI flag is what uvicorn binds."""
    from api.security import resolve_bind_host
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setattr(sys, "argv", ["uvicorn", "api.main:app", "--host", "0.0.0.0"])
    assert resolve_bind_host() == "0.0.0.0"


def test_bind_guard_refuses_when_any_configured_host_is_non_loopback(monkeypatch):
    """Fail closed across every source that could determine the bind."""
    from api.security import assert_safe_bind
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    monkeypatch.setattr(sys, "argv", ["uvicorn", "api.main:app", "--host", "127.0.0.1"])
    with pytest.raises(RuntimeError):
        assert_safe_bind()


def test_read_only_mode_is_a_valid_exposure_without_an_api_key(monkeypatch):
    """The guard's own error message offers READ_ONLY_MODE=1 as the read-only
    alternative to setting a key — and it must actually be one. In read-only
    mode every mutating route returns 403 before the key is consulted, so
    there is nothing left for a key to protect.

    Concretely: the container CMD binds --host 0.0.0.0, so without this a
    read-only container cannot start at all.
    """
    from api.security import assert_safe_bind
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("READ_ONLY_MODE", "1")
    assert_safe_bind("0.0.0.0")   # must not raise


def test_non_loopback_still_refuses_when_neither_key_nor_read_only_is_set(monkeypatch):
    from api.security import assert_safe_bind
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    with pytest.raises(RuntimeError):
        assert_safe_bind("0.0.0.0")


def test_bind_host_defaults_to_loopback(monkeypatch):
    from api.security import resolve_bind_host
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.setattr(sys, "argv", ["pytest"])
    assert resolve_bind_host() == "127.0.0.1"


# ---------------------------------------------------------------------------
# B5 — interactive docs off by default
# ---------------------------------------------------------------------------

def test_api_docs_are_disabled_by_default(monkeypatch):
    from api.security import api_docs_enabled
    monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    assert api_docs_enabled() is False


def test_api_docs_can_be_explicitly_enabled(monkeypatch):
    from api.security import api_docs_enabled
    monkeypatch.setenv("ENABLE_API_DOCS", "1")
    assert api_docs_enabled() is True


def test_docs_endpoints_are_not_served(monkeypatch):
    """Swagger's 'Try it out' is a working console for disenrich-all, and the
    confirmation string is in the client JS anyway."""
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    client = TestClient(api_main.app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, f"{path} is exposed"


# ---------------------------------------------------------------------------
# B2 — .env from an explicit path
# ---------------------------------------------------------------------------

def test_env_file_is_loaded_from_an_explicit_absolute_path(tmp_path, monkeypatch):
    from engine.env_loader import load_env
    env = tmp_path / "secrets.env"
    env.write_text("LEAD_ENGINE_TEST_TOKEN=from-explicit-path\n", encoding="utf-8")
    monkeypatch.setenv("LEAD_ENGINE_ENV_FILE", str(env))
    monkeypatch.delenv("LEAD_ENGINE_TEST_TOKEN", raising=False)

    loaded = load_env()

    import os
    assert os.getenv("LEAD_ENGINE_TEST_TOKEN") == "from-explicit-path"
    assert loaded == env


def test_a_relative_env_file_path_is_rejected(monkeypatch):
    """A relative path re-introduces the repo-root scan this fix removes."""
    from engine.env_loader import load_env
    monkeypatch.setenv("LEAD_ENGINE_ENV_FILE", "./.env")
    with pytest.raises(ValueError) as exc:
        load_env()
    assert "absolute" in str(exc.value).lower()


def test_strict_mode_refuses_the_repo_root_fallback(monkeypatch):
    """With LEAD_ENGINE_STRICT_ENV=1 nothing is auto-discovered — config must
    come from the real process environment or an explicit path."""
    from engine.env_loader import load_env
    monkeypatch.delenv("LEAD_ENGINE_ENV_FILE", raising=False)
    monkeypatch.setenv("LEAD_ENGINE_STRICT_ENV", "1")
    assert load_env() is None


# ---------------------------------------------------------------------------
# R1 — response field allow-list, READ_ONLY_MODE
# ---------------------------------------------------------------------------

def test_company_detail_never_returns_the_raw_llm_response(temp_db_with_enrichment):
    """SELECT * shipped the unfiltered model output to every viewer."""
    import db.dal as dal_mod
    detail = dal_mod.get_company_detail("c1")
    assert "llm_raw_response" not in detail["enrichment"]


def test_company_detail_still_returns_the_fields_the_dashboard_uses(temp_db_with_enrichment):
    """The allow-list must not amputate the page it protects."""
    import db.dal as dal_mod
    e = dal_mod.get_company_detail("c1")["enrichment"]
    for key in ("qualification_score", "narrative_assessment", "guard_passed",
                "guard_score", "signal_strength", "generated_at", "recommended_angle"):
        assert key in e, f"dashboard field {key} was dropped"


def test_raw_payload_is_not_in_the_detail_response_schema():
    from api.schemas import CompanyDetailResponse
    assert "raw_payload" not in CompanyDetailResponse.model_fields


def test_read_only_mode_is_off_by_default(monkeypatch):
    from api.security import read_only_mode
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    assert read_only_mode() is False


def test_read_only_mode_redacts_contact_pii():
    """Named individuals at Irish regulated firms are personal data; network
    admins and a third-party AI tool have no business need for them."""
    from api.redaction import redact_company_detail
    detail = {
        "legal_name": "Acme Insurance Limited",
        "contacts": [{"id": "1", "full_name": "Jane Doe", "email": "jane@acme.ie", "phone": "+353 1 234"}],
        "enrichment": {"narrative_assessment": {
            "contacts": [{"name": "Jane Doe", "email": "jane@acme.ie", "role": "Director"}]
        }},
        "email": {"subject": "Hi Jane", "body": "Hi Jane, ..."},
    }
    out = redact_company_detail(detail)

    assert out["legal_name"] == "Acme Insurance Limited"          # company data stays
    assert "Jane Doe" not in str(out["contacts"])
    assert "jane@acme.ie" not in str(out)
    assert out["enrichment"]["narrative_assessment"]["contacts"][0]["role"] == "Director"
    assert "Jane" not in str(out["email"])


def test_redaction_scrubs_names_quoted_in_narrative_text():
    """Found live: redacting the structured contact fields left the name in
    the LLM's own prose — opportunity_signal.decision_maker_access.reason and
    personalisation.reference_points both quoted it verbatim."""
    from api.redaction import redact_company_detail
    detail = {
        "legal_name": "AIDAN MURPHY INSURANCES",
        "contacts": [],
        "enrichment": {"narrative_assessment": {
            "contacts": [{"name": "John Murphy", "role": "Director", "email": "j.murphy@example.ie"}],
            "opportunity_signal": {"decision_maker_access": {
                "level": "medium", "pct": 55.0,
                "reason": "John Murphy, Director, identified via LinkedIn corroboration.",
            }},
            "personalisation": {"reference_points": [
                "John Murphy identified as Director — a potential direct decision-maker",
                "Family-run broker established 1982",
            ]},
            "executive_summary": "Contact j.murphy@example.ie was found on the site.",
        }},
        "email": None,
    }
    out = redact_company_detail(detail)
    blob = json.dumps(out)

    assert "John Murphy" not in blob
    assert "j.murphy@example.ie" not in blob
    # The company's own name must survive — "Murphy" appears in it, and
    # scrubbing surnames blindly would mangle the thing we want on screen.
    assert out["legal_name"] == "AIDAN MURPHY INSURANCES"
    assert "Family-run broker established 1982" in blob
    assert out["enrichment"]["narrative_assessment"]["opportunity_signal"]["decision_maker_access"]["pct"] == 55.0


def test_redaction_is_a_no_op_on_a_detail_with_no_contacts():
    from api.redaction import redact_company_detail
    out = redact_company_detail({"legal_name": "X", "contacts": [], "enrichment": None, "email": None})
    assert out["legal_name"] == "X"


def test_read_only_mode_refuses_writes_even_with_a_valid_key(monkeypatch):
    """Capability removal, not credential distribution — the dashboard has
    nowhere to hold a key anyway."""
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    monkeypatch.setenv("READ_ONLY_MODE", "1")
    monkeypatch.setenv("API_KEY", "s3cret")

    @contextmanager
    def fake_conn():
        yield None
    monkeypatch.setattr(api_main, "get_db_connection", fake_conn)
    monkeypatch.setattr(api_main, "clear_all_enrichments", lambda: 7)

    resp = TestClient(api_main.app).post(
        "/api/disenrich-all",
        json={"confirm": "DELETE ALL ENRICHMENTS"},
        headers={"X-API-Key": "s3cret"},
    )
    assert resp.status_code == 403
    assert "read-only" in resp.text.lower()


def test_read_only_mode_still_allows_reads(monkeypatch):
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    monkeypatch.setenv("READ_ONLY_MODE", "1")
    monkeypatch.setattr(api_main, "get_company_detail", lambda cid: None)
    assert TestClient(api_main.app).get("/api/leads/nope").status_code == 404


# ---------------------------------------------------------------------------
# R2 — SSRF egress boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "127.0.0.1",        # loopback
    "::1",              # ipv6 loopback
    "10.1.2.3",         # RFC1918
    "192.168.0.5",      # RFC1918
    "172.16.4.4",       # RFC1918
    "169.254.169.254",  # link-local / cloud metadata
    "100.101.102.103",  # CGNAT / private overlay range
    "0.0.0.0",          # unspecified
])
def test_private_and_loopback_targets_are_rejected(ip):
    from engine.net_guard import is_public_ip
    assert is_public_ip(ipaddress.ip_address(ip)) is False, f"{ip} should be blocked"


@pytest.mark.parametrize("ip", ["8.8.8.8", "104.16.132.229", "2606:4700::1111"])
def test_public_targets_are_allowed(ip):
    from engine.net_guard import is_public_ip
    assert is_public_ip(ipaddress.ip_address(ip)) is True


def test_url_resolving_to_a_private_address_is_rejected(monkeypatch):
    """Validating the URL string alone is defeated by DNS — resolve first."""
    import engine.net_guard as ng
    monkeypatch.setattr(ng, "_resolve", lambda host: [ipaddress.ip_address("192.168.1.10")])
    ok, reason = ng.is_safe_url("https://totally-normal-domain.ie/about")
    assert ok is False
    assert "private" in reason.lower() or "not public" in reason.lower()


def test_url_resolving_to_a_public_address_is_allowed(monkeypatch):
    import engine.net_guard as ng
    monkeypatch.setattr(ng, "_resolve", lambda host: [ipaddress.ip_address("8.8.8.8")])
    ok, _ = ng.is_safe_url("https://example.ie/about")
    assert ok is True


def test_non_http_schemes_are_rejected():
    from engine.net_guard import is_safe_url
    for url in ("file:///etc/passwd", "gopher://x/", "ftp://x/"):
        ok, _ = is_safe_url(url)
        assert ok is False, f"{url} should be blocked"


def test_dns_failure_is_treated_as_unsafe(monkeypatch):
    """Fail closed: an unresolvable host is not a licence to connect."""
    import engine.net_guard as ng
    def boom(host):
        raise OSError("nxdomain")
    monkeypatch.setattr(ng, "_resolve", boom)
    ok, _ = ng.is_safe_url("https://nope.invalid/")
    assert ok is False


def test_fetcher_does_not_follow_redirects_blindly():
    """A crawled page redirecting to 169.254.169.254 was the whole vector."""
    import inspect
    import engine.researcher as researcher
    src = inspect.getsource(researcher)
    assert "follow_redirects=True" not in src, "redirects are still auto-followed"


def test_redirect_hop_to_a_private_address_is_rejected(monkeypatch):
    import engine.net_guard as ng
    monkeypatch.setattr(ng, "_resolve", lambda host: [ipaddress.ip_address("127.0.0.1")])
    ok, _ = ng.is_safe_url("http://redirect-target.internal/latest/meta-data/")
    assert ok is False


# ---------------------------------------------------------------------------
# R5 — /api/log rate limit + events.log cap
# ---------------------------------------------------------------------------

def test_client_log_is_rate_limited_per_ip(monkeypatch):
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    api_main.reset_log_rate_limiter()
    monkeypatch.setattr(api_main, "_log_event", lambda m: None)

    client = TestClient(api_main.app)
    codes = [client.post("/api/log", json={"msg": f"click {i}"}).status_code for i in range(70)]

    assert codes[0] == 200
    assert 429 in codes, "no rate limit — an exposed port makes this a disk-fill primitive"
    assert codes.count(200) <= 60


def test_rate_limiter_allows_a_normal_burst(monkeypatch):
    import db.init_db as init_db_mod
    monkeypatch.setattr(init_db_mod, "init_db", lambda *a, **k: None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    api_main.reset_log_rate_limiter()
    monkeypatch.setattr(api_main, "_log_event", lambda m: None)

    client = TestClient(api_main.app)
    codes = [client.post("/api/log", json={"msg": "x"}).status_code for _ in range(20)]
    assert all(c == 200 for c in codes), "normal dashboard chatter must not be throttled"


def test_events_log_is_rotated_when_it_exceeds_the_cap(tmp_path, monkeypatch):
    import api.main as api_main
    log = tmp_path / "events.log"
    log.write_text("x" * 2000, encoding="utf-8")
    monkeypatch.setattr(api_main, "_EVENTS_LOG", log)
    monkeypatch.setattr(api_main, "_MAX_EVENTS_LOG_BYTES", 1000)

    api_main._log_event("new line after rotation")

    assert log.exists()
    assert log.stat().st_size < 2000, "log was not rotated"
    assert (tmp_path / "events.log.1").exists(), "previous log was discarded instead of rotated"


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db_with_enrichment(tmp_path, monkeypatch):
    import json as _json
    import sqlite3
    db_file = tmp_path / "test_leads.db"
    conn = sqlite3.connect(db_file)
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO companies (id, legal_name, source, ingested_at) VALUES (?,?,?,?)",
        ("c1", "Acme Insurance Limited", "cbi_register", "2026-07-24T00:00:00Z"),
    )
    conn.execute(
        """INSERT INTO enrichment (id, company_id, qualification_score, llm_model,
               llm_raw_response, narrative_assessment, signal_strength,
               recommended_angle, guard_passed, guard_score, generated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("e1", "c1", 71, "test-model", "SECRET RAW MODEL OUTPUT",
         _json.dumps({"contacts": []}), "medium", "angle", 1, 95.0,
         "2026-07-24T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    import db.dal as dal_mod
    monkeypatch.setattr(dal_mod, "_DB_PATH", db_file)
    return db_file
