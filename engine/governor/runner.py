"""
engine/governor/runner.py
Guard pipeline runner — evaluates all guards and returns an aggregate report.

Design decisions:
  - Guards run synchronously (no async/Celery at this scale)
  - Pipeline is fail-fast: if a guard hard-fails, subsequent guards are skipped
  - Warnings do not stop the pipeline — they are flagged in the report
  - The overall_score is a weighted average of individual guard scores
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from engine.governor.guards import GUARD_PIPELINE, Guard, GuardResult

logger = logging.getLogger(__name__)


@dataclass
class GuardReport:
    """Aggregate result from running the full guard pipeline."""
    passed: bool
    overall_score: float           # 0–100 weighted average
    guards_run: list[GuardResult] = field(default_factory=list)
    failed_guards: list[str] = field(default_factory=list)
    warning_guards: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "overall_score": round(self.overall_score, 1),
            "failed_guards": self.failed_guards,
            "warning_guards": self.warning_guards,
            "guards_run": [
                {
                    "guard_id": r.guard_id,
                    "guard_name": r.guard_name,
                    "passed": r.passed,
                    "score": round(r.score, 1),
                    "reason": r.reason,
                    "is_warning": r.is_warning,
                    "errored": r.errored,
                    "reasoning_steps": r.reasoning_steps,
                }
                for r in self.guards_run
            ],
        }


def run_guards(
    enrichment: dict[str, Any],
    pipeline: list[Guard] | None = None,
    fail_fast: bool = True,
) -> GuardReport:
    """
    Run the guard pipeline against an enrichment dict.

    Args:
        enrichment:  The raw enrichment dict returned by assess_company().
        pipeline:    Override the default guard pipeline (useful for testing).
        fail_fast:   If True, stop pipeline on first hard failure.
                     Warnings do not trigger fail-fast.

    Returns:
        GuardReport with aggregate pass/fail and per-guard details.
    """
    guards = pipeline if pipeline is not None else GUARD_PIPELINE
    results: list[GuardResult] = []
    failed: list[str] = []
    warned: list[str] = []
    hard_failed = False

    for guard in guards:
        try:
            result = guard.evaluate(enrichment)
        except Exception as exc:
            # Guard itself raised an unexpected error — treat as a soft warning
            # so the pipeline doesn't crash on a guard bug
            logger.error(
                f"[Governor] Guard {guard.GUARD_ID} raised an exception: {exc}",
                exc_info=True,
            )
            # Non-blocking (a guard bug must not take down every assessment)
            # but explicitly errored, not "passed with 50/100" — a check that
            # never ran produced no evidence, and scoring it as half-decent
            # quietly dragged /api/guard-stats' average toward the middle.
            result = GuardResult(
                guard_id=guard.GUARD_ID,
                guard_name=guard.GUARD_NAME,
                passed=True,
                score=0.0,
                reason=(
                    f"Guard evaluation failed with exception: {exc}. This check "
                    f"did not run — treat its dimension as unverified, not as passed."
                ),
                reasoning_steps=[f"Exception: {exc}"],
                is_warning=True,
                errored=True,
            )

        results.append(result)

        if not result.passed:
            failed.append(result.guard_id)
            hard_failed = True
            logger.warning(
                f"[Governor] Guard FAILED: {result.guard_id} — {result.reason}"
            )
            if fail_fast:
                logger.info(
                    f"[Governor] Fail-fast triggered by {result.guard_id}. "
                    f"Skipping {len(guards) - len(results)} remaining guard(s)."
                )
                break
        elif result.is_warning:
            warned.append(result.guard_id)
            logger.info(
                f"[Governor] Guard WARNING: {result.guard_id} — {result.reason}"
            )
        else:
            logger.debug(
                f"[Governor] Guard PASSED: {result.guard_id} — {result.reason}"
            )

    # Average over guards that actually evaluated something. A crashed guard
    # contributes no evidence, so including it would invent a score from a
    # check that never happened. If every guard crashed there is no quality
    # signal at all, which is 0, not the average of nothing.
    scored = [r for r in results if not r.errored]
    overall_score = sum(r.score for r in scored) / len(scored) if scored else 0.0

    report = GuardReport(
        passed=not hard_failed,
        overall_score=overall_score,
        guards_run=results,
        failed_guards=failed,
        warning_guards=warned,
    )

    _level = logging.WARNING if hard_failed else logging.INFO
    logger.log(
        _level,
        f"[Governor] Pipeline {'FAILED' if hard_failed else 'PASSED'} — "
        f"score={overall_score:.1f}, failed={failed}, warnings={warned}",
    )

    return report
