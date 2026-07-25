"""
api/security.py

Access control for the HTTP surface.

Three fail-open defaults lived here:

  - CORS was `allow_origins=["*"]` with `allow_credentials=True`. The
    dashboard is served same-origin by this very app, so it never needed a
    CORS grant; the wildcard existed only to hand one to everybody else.
  - `CORS_ORIGINS` was documented in `.env.example` and read by no code.
  - `POST /api/disenrich-all` deleted every assessment and every draft email
    from a single unauthenticated POST with no body.

Auth is deliberately **opt-in**. With `API_KEY` unset the app behaves exactly
as it always has, so a localhost-only install is unaffected by this change.
Setting `API_KEY` turns enforcement on for mutating endpoints only — reads
stay open, because gating them would break the dashboard the moment a key is
configured, and the dashboard has nowhere to keep one.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import sys

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

# The exact string a caller must send to run the destructive endpoint. Typed,
# not a boolean — the point is that it cannot be sent by accident, by a
# double-click, or by a drive-by request that guessed the URL.
DISENRICH_CONFIRMATION = "DELETE ALL ENRICHMENTS"


def cors_origins() -> list[str]:
    """Parse `CORS_ORIGINS` into an allow-list.

    Empty by default: same-origin requests don't consult CORS at all, so the
    dashboard keeps working with no entries here. A literal `*` is dropped
    rather than honoured — browsers reject wildcard-with-credentials anyway,
    and this app has no reason to invite arbitrary origins.
    """
    raw = os.getenv("CORS_ORIGINS") or ""
    origins = [o.strip() for o in raw.split(",") if o.strip() and o.strip() != "*"]
    return origins


def get_api_key() -> str | None:
    """The configured key, or None if auth is off. A blank/whitespace value
    counts as unset so `API_KEY=` in a .env can't enable auth with the empty
    string as the valid credential."""
    raw = (os.getenv("API_KEY") or "").strip()
    return raw or None


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """FastAPI dependency guarding mutating endpoints.

    No-op when no key is configured. Compared with `hmac.compare_digest` to
    keep the check constant-time.
    """
    configured = get_api_key()
    if configured is None:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, configured):
        logger.warning("[Security] Rejected a mutating request with a missing or invalid X-API-Key")
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


def read_only_mode() -> bool:
    """True when the app should serve reads only.

    Writes are refused and contact PII is redacted. This is capability removal
    rather than credential distribution: the dashboard is a static page with
    nowhere to keep a key, so "just set API_KEY" does not protect a read-only
    deployment — it breaks it.
    """
    return (os.getenv("READ_ONLY_MODE") or "").strip().lower() in ("1", "true", "yes")


def api_docs_enabled() -> bool:
    """Whether to serve /docs, /redoc and /openapi.json.

    Off by default. Swagger's "Try it out" is a working console for every
    mutating endpoint, including disenrich-all — and the confirmation string
    it needs is in the client JS anyway, so the docs page is the difference
    between "possible" and "two clicks".
    """
    return (os.getenv("ENABLE_API_DOCS") or "").strip().lower() in ("1", "true", "yes")


def _cli_bind_host() -> str | None:
    """The `--host` uvicorn was actually started with, if any."""
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            return argv[i + 1].strip()
        if arg.startswith("--host="):
            return arg.split("=", 1)[1].strip()
    return None


def candidate_bind_hosts() -> list[str]:
    """Every host any configuration source could bind to.

    The guard checks all of them rather than picking one, because getting the
    precedence wrong fails *open*: a server started with
    `uvicorn --host 0.0.0.0` while .env still carries the shipped
    `API_HOST=127.0.0.1` would otherwise resolve to loopback and pass.
    """
    hosts = []
    cli = _cli_bind_host()
    if cli:
        hosts.append(cli)
    env = (os.getenv("API_HOST") or "").strip()
    if env:
        hosts.append(env)
    return hosts or ["127.0.0.1"]


def resolve_bind_host() -> str:
    """The interface the server will actually bind to.

    The CLI flag wins over `API_HOST`: uvicorn binds what it was given on the
    command line, and the env var is only a declaration of intent.
    """
    return candidate_bind_hosts()[0]


def _is_loopback(host: str) -> bool:
    if host.lower() in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname we can't classify is not assumed to be loopback.
        return False


def assert_safe_bind(host: str | None = None) -> None:
    """Refuse to start when listening beyond loopback with no API key.

    On a non-loopback bind every mutating endpoint would otherwise be open to
    anyone who can reach the port: disenrich-all destroys all
    stored work, enrich-all spends the LLM budget uncancellably. Making the
    unsafe configuration fail at startup is the point — a warning scrolls past
    and the server still comes up.
    """
    hosts = [host] if host is not None else candidate_bind_hosts()
    exposed = [h for h in hosts if not _is_loopback(h)]
    if not exposed:
        return
    if read_only_mode():
        # Read-only: every mutating route returns 403 before the key is even
        # consulted, so there is nothing left for a key to protect. This is
        # the supported way to run a non-loopback deployment — and the container
        # CMD binds 0.0.0.0, so without this a read-only container cannot start.
        logger.warning(
            f"[Security] Bound to {exposed[0]} in READ_ONLY_MODE — read-only, "
            "writes refused, contact PII redacted."
        )
        return
    if get_api_key() is not None:
        logger.info(f"[Security] Bound to {exposed[0]} with API_KEY set — mutating endpoints require X-API-Key.")
        return
    raise RuntimeError(
        f"Refusing to start: bound to {exposed[0]!r}, which is reachable beyond this machine, "
        "with no API_KEY set. Every mutating endpoint (including /api/disenrich-all, which "
        "permanently deletes all assessments) would be open to anyone who can reach that "
        "port. Set API_KEY, or set READ_ONLY_MODE=1 for a read-only exposure, or bind to "
        "127.0.0.1."
    )


def require_write_access(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Dependency for every mutating endpoint.

    Read-only mode refuses outright — before the key is even considered — so a
    leaked or shared key still cannot be used to write.
    """
    if read_only_mode():
        logger.warning("[Security] Refused a write: READ_ONLY_MODE is on (read-only).")
        raise HTTPException(
            status_code=403,
            detail="The dashboard is in read-only mode; write operations are disabled.",
        )
    require_api_key(x_api_key)


def require_confirmation(payload: dict | None, expected: str = DISENRICH_CONFIRMATION) -> None:
    """Reject a destructive request that didn't explicitly opt in."""
    supplied = (payload or {}).get("confirm")
    if supplied != expected:
        raise HTTPException(
            status_code=400,
            detail=(
                f'This permanently deletes every assessment and draft email. '
                f'Send {{"confirm": "{expected}"}} to proceed.'
            ),
        )
