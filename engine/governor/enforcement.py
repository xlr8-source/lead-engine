"""
engine/governor/enforcement.py

Whether a guard verdict actually gates storage.

The guard pipeline has always computed a verdict — `run_guards()` returns
`GuardReport.passed`, `assess_company()` copies it onto the enrichment dict,
the UI renders it. Nothing branched on it. A hard-failing assessment, up to
and including one whose own guard reason read "not reliable enough to store",
was persisted exactly like a passing one.

This module owns that decision so both persistence call sites (single
assessment and bulk enrichment) share one policy rather than two copies of an
`if`.

Modes (env `GUARD_ENFORCEMENT`):
    off     Guards are not run at all. No verdict is recorded — the stored
            verdict is NULL, not a fabricated pass, so `/api/guard-stats`
            excludes these rows instead of counting them as clean.
    warn    Guards run, the verdict is recorded, storage proceeds regardless.
            This is the historical behaviour and the default: enabling this
            feature must not change outcomes for anyone who hasn't opted in.
    block   Guards run, the verdict is recorded, and a hard failure prevents
            the assessment from reaching the database.

`block` fails closed: an enrichment carrying no verdict at all has not been
shown to be safe, so its absence is a rejection, not a pass.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ENFORCEMENT_MODES = ("off", "warn", "block")
DEFAULT_MODE = "warn"


def get_enforcement_mode() -> str:
    """Resolve `GUARD_ENFORCEMENT`, falling back to the default on anything
    unrecognised. Read per call rather than at import so the mode can be
    changed without restarting a long-running process (and so tests can set
    it without reloading the module)."""
    raw = (os.getenv("GUARD_ENFORCEMENT") or DEFAULT_MODE).strip().lower()
    if raw not in ENFORCEMENT_MODES:
        # Falling back to `warn`, never to `off` — a typo must not silently
        # disable the guards.
        logger.warning(
            "[Governor] GUARD_ENFORCEMENT=%r is not one of %s — using %r.",
            raw, ENFORCEMENT_MODES, DEFAULT_MODE,
        )
        return DEFAULT_MODE
    return raw


@dataclass(frozen=True)
class StorageDecision:
    """Whether this enrichment may be persisted, and why not if it may not."""
    store: bool
    reason: str | None = None


def evaluate_storage(enrichment: dict) -> StorageDecision:
    """Decide whether `enrichment` may be written to the database."""
    mode = get_enforcement_mode()
    if mode != "block":
        return StorageDecision(store=True)

    verdict = enrichment.get("guard_passed")

    if verdict is None:
        return StorageDecision(
            store=False,
            reason=(
                "Rejected: no guard verdict on this assessment, so it has not "
                "been shown to meet the quality bar (GUARD_ENFORCEMENT=block)."
            ),
        )

    if verdict:
        return StorageDecision(store=True)

    failures = enrichment.get("guard_failures") or []
    failed = ", ".join(str(f) for f in failures) if failures else "unspecified guard"
    return StorageDecision(
        store=False,
        reason=f"Rejected by guard(s): {failed} (GUARD_ENFORCEMENT=block).",
    )
