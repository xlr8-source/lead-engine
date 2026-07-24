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
import logging
import os

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
