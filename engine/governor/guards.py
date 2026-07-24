"""
engine/governor/guards.py
Constitutional AI guard implementations for Lead Engine enrichment quality.

Guards are evaluated in fail-fast order (cheapest first). All guards are pure
Python — zero LLM tokens consumed, < 5ms per evaluation.

Guard IDs:
  EG-QUAL-001  Evidence Quality Guard
  EG-CONF-002  Confidence Threshold Guard
  EG-SUMM-003  Executive Summary Guard
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base types
# ---------------------------------------------------------------------------

@dataclass
class GuardResult:
    guard_id: str
    guard_name: str
    passed: bool
    score: float          # 0–100
    reason: str
    reasoning_steps: list[str] = field(default_factory=list)
    is_warning: bool = False  # True = soft fail (passes pipeline but flagged)
    # True = the guard raised and never actually evaluated anything. Distinct
    # from a soft fail: a soft fail is a judgement, this is the absence of one.
    # `score` is meaningless on an errored result and is excluded from the
    # pipeline average rather than averaged in as a middling number.
    errored: bool = False


class Guard:
    """Abstract base class for all guards."""

    GUARD_ID: str = ""
    GUARD_NAME: str = ""

    def evaluate(self, enrichment: dict[str, Any]) -> GuardResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# EG-QUAL-001: Evidence Quality Guard
# ---------------------------------------------------------------------------

class EvidenceQualityGuard(Guard):
    """
    Constitutional AI Guard — Evidence Quality

    Harmlessness:  Sources must be from identifiable domains (not blank/generic)
    Honesty:       Sources must span ≥ 2 distinct domains (no single-source bias)
    Helpfulness:   At least 1 source should appear recent (detectable date signal)

    CRO/CBI are DEFAULT sources. Any other source is EXTERNAL.
    PASS if: 2+ default OR 1 default + 1 external OR 2+ external

    Cost: 0 tokens (pure Python)
    """

    GUARD_ID = "EG-QUAL-001"
    GUARD_NAME = "Evidence Quality Guard"

    # Domains that are too generic to count as distinct research sources
    GENERIC_DOMAINS = {"google.com", "bing.com", "yahoo.com", "duckduckgo.com"}

    # Default (registry) sources per user rule
    DEFAULT_SOURCES = {
        "cro_register", "cbi_register", "cro_cbi_records",
        "cbi_reference", "cro_number", "cbi_ref", "cbi_ref_number",
        "cbi_authorisation", "cbi_authorisations", "cbi_authorization",
    }

    def evaluate(self, enrichment: dict[str, Any]) -> GuardResult:
        steps: list[str] = []

        # Pull sources from enrichment or its narrative sub-dict
        narrative = enrichment.get("narrative_assessment") or {}
        if isinstance(narrative, str):
            import json
            try:
                narrative = json.loads(narrative)
            except Exception:
                narrative = {}

        # sources_reviewed can be a list (from LLM) or int count (from compute_sources_reviewed)
        sources_list = (
            enrichment.get("sources_reviewed")
            or narrative.get("sources_reviewed")
            or []
        )

        # If it's an integer count (legacy), reconstruct default sources from company data
        if isinstance(sources_list, int):
            count = sources_list
            steps.append(f"sources_reviewed is integer count: {count}")
            # Reconstruct default sources from enrichment data
            default_sources = []
            if enrichment.get("cbi_reference") or enrichment.get("cbi_authorisations"):
                default_sources.append("cbi_register")
            if enrichment.get("cro_number") or enrichment.get("cro_status"):
                default_sources.append("cro_register")
            # If we have website/digital presence, add as external
            digital = enrichment.get("digital_presence") or {}
            if digital.get("has_website"):
                default_sources.append("website")
            # Use count to determine external sources
            external_count = max(0, count - len(default_sources))
            sources_list = default_sources + [f"external_{i}" for i in range(external_count)]
            steps.append(f"Reconstructed {len(default_sources)} default + {external_count} external sources")

        if not isinstance(sources_list, list):
            sources_list = []

        # Normalize sources for comparison
        norm_sources = [str(s).strip().lower() for s in sources_list if s]

        # Count default (registry) vs external sources
        default_count = sum(1 for s in norm_sources if s in self.DEFAULT_SOURCES)
        external_count = len(norm_sources) - default_count

        steps.append(f"Sources: {len(norm_sources)} total ({default_count} default, {external_count} external)")

        # PASS conditions:
        # - 2+ default sources (CRO + CBI)
        # - 1 default + 1+ external
        # - 2+ external
        if default_count >= 2:
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=True,
                score=80.0,
                reason="CRO + CBI registry sources sufficient",
                reasoning_steps=steps + ["Two default registry sources found"],
            )

        if default_count >= 1 and external_count >= 1:
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=True,
                score=90.0,
                reason="Registry + external source",
                reasoning_steps=steps + [f"1 default + {external_count} external"],
            )

        if external_count >= 2:
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=True,
                score=100.0,
                reason="Multiple external sources",
                reasoning_steps=steps + [f"{external_count} external sources"],
            )

        # FAIL with specific guidance
        if default_count == 0 and external_count == 0:
            reason = "No research sources found. Assessment has zero evidence basis."
        elif default_count == 0 and external_count == 1:
            reason = "Only 1 external source found. Need ≥ 2 external OR 1 default + 1 external."
        elif default_count == 1 and external_count == 0:
            reason = "Only 1 default registry source (CRO or CBI). Need 2 default OR 1 default + 1 external."
        else:
            reason = f"Insufficient: {default_count} default, {external_count} external. Need 2 default OR 1+1 OR 2 external."

        return GuardResult(
            guard_id=self.GUARD_ID,
            guard_name=self.GUARD_NAME,
            passed=False,
            score=0.0,
            reason=reason,
            reasoning_steps=steps,
        )

    def _extract_domains(self, sources: list[str]) -> list[str]:
        domains = []
        for s in sources:
            s = str(s).strip()
            try:
                parsed = urlparse(s if "://" in s else f"https://{s}")
                domain = parsed.netloc or parsed.path.split("/")[0]
                # Strip www.
                domain = re.sub(r"^www\.", "", domain).lower()
                if domain:
                    domains.append(domain)
            except Exception:
                # Non-URL source (e.g. "CRO register") — still counts as 1 domain
                clean = s[:30].lower().replace(" ", "_")
                if clean:
                    domains.append(clean)
        return domains


# ---------------------------------------------------------------------------
# EG-CONF-002: Confidence Threshold Guard
# ---------------------------------------------------------------------------

class ConfidenceThresholdGuard(Guard):
    """
    Constitutional AI Guard — Research Confidence

    Honesty: Low confidence must be explicitly acknowledged. High confidence
             claims must be proportional to available evidence.

    Thresholds:
      < 30   → hard fail (assessment is not reliable)
      30–49  → soft fail (passes with is_warning=True, flagged in audit)
      50–69  → warn (passes, but flagged for human review)
      ≥ 70   → pass

    Cost: 0 tokens (pure Python)
    """

    GUARD_ID = "EG-CONF-002"
    GUARD_NAME = "Confidence Threshold Guard"

    HARD_FAIL_THRESHOLD = 30.0
    SOFT_FAIL_THRESHOLD = 50.0
    WARN_THRESHOLD = 70.0

    def evaluate(self, enrichment: dict[str, Any]) -> GuardResult:
        steps: list[str] = []

        narrative = enrichment.get("narrative_assessment") or {}
        if isinstance(narrative, str):
            import json
            try:
                narrative = json.loads(narrative)
            except Exception:
                narrative = {}

        # Confidence can live at top level or inside narrative_assessment
        raw = (
            enrichment.get("research_confidence")
            or narrative.get("research_confidence")
        )

        if raw is None:
            steps.append("research_confidence field is missing — defaulting to 0")
            confidence = 0.0
        else:
            try:
                confidence = float(raw)
            except (TypeError, ValueError):
                steps.append(f"research_confidence '{raw}' is not numeric — defaulting to 0")
                confidence = 0.0

        steps.append(f"research_confidence = {confidence:.1f}%")

        if confidence < self.HARD_FAIL_THRESHOLD:
            steps.append(f"Below hard-fail threshold ({self.HARD_FAIL_THRESHOLD}%) — assessment unreliable")
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=False,
                score=confidence,
                reason=f"research_confidence {confidence:.0f}% is below minimum threshold {self.HARD_FAIL_THRESHOLD:.0f}%. Assessment not reliable enough to store.",
                reasoning_steps=steps,
            )

        if confidence < self.SOFT_FAIL_THRESHOLD:
            steps.append(f"Below soft-fail threshold ({self.SOFT_FAIL_THRESHOLD}%) — flagging for review")
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=True,
                score=confidence,
                reason=f"research_confidence {confidence:.0f}% is low. Human review recommended.",
                reasoning_steps=steps,
                is_warning=True,
            )

        if confidence < self.WARN_THRESHOLD:
            steps.append(f"Below recommended threshold ({self.WARN_THRESHOLD}%) — soft warning")
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=True,
                score=confidence,
                reason=f"research_confidence {confidence:.0f}% is moderate. Consider additional research.",
                reasoning_steps=steps,
                is_warning=True,
            )

        steps.append(f"Confidence {confidence:.1f}% meets threshold — PASS")
        return GuardResult(
            guard_id=self.GUARD_ID,
            guard_name=self.GUARD_NAME,
            passed=True,
            score=confidence,
            reason=f"research_confidence {confidence:.0f}% meets quality standard.",
            reasoning_steps=steps,
        )


# ---------------------------------------------------------------------------
# EG-SUMM-003: Executive Summary Guard
# ---------------------------------------------------------------------------

class ExecutiveSummaryGuard(Guard):
    """
    Constitutional AI Guard — Executive Summary Quality

    Helpfulness: Summary must be actionable and substantive (not boilerplate)
    Honesty:     Summary must not be a generic fallback or truncated stub

    Cost: 0 tokens (pure Python)
    """

    GUARD_ID = "EG-SUMM-003"
    GUARD_NAME = "Executive Summary Guard"

    MIN_LENGTH = 80  # characters

    # Phrases that indicate the LLM fell back to a generic response
    FALLBACK_PATTERNS = [
        r"^no information available",
        r"^unable to (assess|determine|find)",
        r"^insufficient data",
        r"^no data (found|available)",
        r"^n/?a\.?$",
        r"^not applicable",
        r"^(the company|this company) could not be (found|assessed|identified)",
        r"^assessment (not|cannot be) (completed|performed)",
    ]

    def evaluate(self, enrichment: dict[str, Any]) -> GuardResult:
        steps: list[str] = []

        narrative = enrichment.get("narrative_assessment") or {}
        if isinstance(narrative, str):
            import json
            try:
                narrative = json.loads(narrative)
            except Exception:
                narrative = {}

        summary: str = (
            enrichment.get("executive_summary")
            or narrative.get("executive_summary")
            or ""
        )
        summary = summary.strip()

        # --- Check 1: not empty ---
        if not summary:
            steps.append("executive_summary is empty")
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=False,
                score=0.0,
                reason="executive_summary is empty.",
                reasoning_steps=steps,
            )

        # --- Check 2: minimum length ---
        if len(summary) < self.MIN_LENGTH:
            steps.append(f"executive_summary is {len(summary)} chars — below minimum {self.MIN_LENGTH}")
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=False,
                score=20.0,
                reason=f"executive_summary too short ({len(summary)} chars). Must be ≥ {self.MIN_LENGTH} chars.",
                reasoning_steps=steps,
            )

        steps.append(f"Length OK: {len(summary)} chars")

        # --- Check 3: not a fallback phrase ---
        lower = summary.lower()
        for pattern in self.FALLBACK_PATTERNS:
            if re.match(pattern, lower):
                steps.append(f"executive_summary matches fallback pattern: '{pattern}'")
                return GuardResult(
                    guard_id=self.GUARD_ID,
                    guard_name=self.GUARD_NAME,
                    passed=False,
                    score=10.0,
                    reason=f"executive_summary appears to be a generic fallback. Pattern matched: {pattern}",
                    reasoning_steps=steps,
                )

        steps.append("No fallback patterns detected")

        # --- Check 4: contains at least one company-specific signal (soft) ---
        # Look for indicators that the summary is specific, not generic boilerplate
        specificity_signals = [
            r"\b(crm|direct debit|premium collect|payment|broker|intermediar|insur|finance|regulated|cbi|cro)\b",
            r"\b(founded|established|years|since \d{4}|\d+ years)\b",
            r"\b(county|dublin|cork|galway|limerick|waterford|ireland|irish)\b",
            r"\b(website|online|digital|linkedin|facebook)\b",
        ]
        signals_found = sum(
            1 for p in specificity_signals
            if re.search(p, lower)
        )

        if signals_found == 0:
            steps.append("Summary appears generic — no company-specific signals detected (warning)")
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=True,
                score=65.0,
                reason="Summary is present but lacks company-specific signals. May be generic.",
                reasoning_steps=steps,
                is_warning=True,
            )

        steps.append(f"Specificity OK: {signals_found} contextual signal(s) found")

        return GuardResult(
            guard_id=self.GUARD_ID,
            guard_name=self.GUARD_NAME,
            passed=True,
            score=100.0,
            reason="Executive summary meets all quality standards.",
            reasoning_steps=steps,
        )


# ---------------------------------------------------------------------------
# EG-DIM-004: Opportunity Signal Explainability Guard
# ---------------------------------------------------------------------------

class OpportunitySignalGuard(Guard):
    """
    Constitutional AI Guard — Opportunity Signal Explainability

    Helpfulness: Every scored dimension (business_fit, regulatory_fit,
                 digital_maturity, evidence_coverage, payment_visibility,
                 decision_maker_access) must carry a specific reason — this
                 is exactly what the UI renders next to each score bar, so a
                 dimension with no (or a placeholder) reason is a score the
                 rep can't defend on a call.
    Honesty:     A reason copy-pasted across every dimension is a sign the
                 model filled in a shape rather than reasoning per-dimension.

    Only total absence of the scorecard is a hard fail — thin or duplicate
    reasons are flagged as warnings rather than blocking storage, since this
    is a new requirement and assessments already saved should stay visible.

    Cost: 0 tokens (pure Python)
    """

    GUARD_ID = "EG-DIM-004"
    GUARD_NAME = "Opportunity Signal Explainability Guard"

    REQUIRED_DIMENSIONS = [
        "business_fit", "regulatory_fit", "digital_maturity",
        "evidence_coverage", "payment_visibility", "decision_maker_access",
    ]
    MIN_REASON_LENGTH = 10

    def evaluate(self, enrichment: dict[str, Any]) -> GuardResult:
        steps: list[str] = []

        narrative = enrichment.get("narrative_assessment") or {}
        if isinstance(narrative, str):
            import json
            try:
                narrative = json.loads(narrative)
            except Exception:
                narrative = {}

        signal = enrichment.get("opportunity_signal") or narrative.get("opportunity_signal") or {}
        if isinstance(signal, str):
            import json
            try:
                signal = json.loads(signal)
            except Exception:
                signal = {}

        if not isinstance(signal, dict) or not signal:
            steps.append("opportunity_signal missing or not a dict")
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=False,
                score=0.0,
                reason="opportunity_signal is missing entirely — the scorecard cannot be explained or shown.",
                reasoning_steps=steps,
            )

        missing_dims = [d for d in self.REQUIRED_DIMENSIONS if d not in signal]
        weak_dims = []
        reasons_seen = []
        for dim in self.REQUIRED_DIMENSIONS:
            entry = signal.get(dim)
            if not isinstance(entry, dict):
                continue
            reason = str(entry.get("reason") or "").strip()
            if len(reason) < self.MIN_REASON_LENGTH:
                weak_dims.append(dim)
            else:
                reasons_seen.append(reason.lower())

        duplicate_reason = len(reasons_seen) >= 2 and len(set(reasons_seen)) == 1

        steps.append(f"Missing: {missing_dims or 'none'}; thin reason (<{self.MIN_REASON_LENGTH} chars): {weak_dims or 'none'}")
        if duplicate_reason:
            steps.append("All non-missing dimension reasons are identical — looks like a copy-pasted placeholder")

        if not missing_dims and not weak_dims and not duplicate_reason:
            return GuardResult(
                guard_id=self.GUARD_ID,
                guard_name=self.GUARD_NAME,
                passed=True,
                score=100.0,
                reason="All 6 opportunity_signal dimensions have specific, distinct reasons.",
                reasoning_steps=steps,
            )

        issue_count = len(missing_dims) + len(weak_dims) + (1 if duplicate_reason else 0)
        return GuardResult(
            guard_id=self.GUARD_ID,
            guard_name=self.GUARD_NAME,
            passed=True,
            score=max(30.0, 100.0 - issue_count * 15),
            reason=(
                f"{len(missing_dims)} dimension(s) missing, {len(weak_dims)} under-explained"
                + (", duplicate reasons across dimensions" if duplicate_reason else "")
                + " — scorecard is only partially explainable."
            ),
            reasoning_steps=steps,
            is_warning=True,
        )


# ---------------------------------------------------------------------------
# EG-DPRES-005: Digital Presence Consistency Guard
# ---------------------------------------------------------------------------

class DigitalPresenceConsistencyGuard(Guard):
    """
    Constitutional AI Guard — Digital Presence Consistency

    Honesty: digital_presence is computed deterministically from the crawled
             page BEFORE the LLM runs, with no company-name verification. If
             the LLM's own executive_summary / personalisation.avoid /
             digital_maturity.reason concludes the crawled page belongs to a
             different company, but digital_presence.has_website is still
             True, the record is self-contradictory — a rep or an email
             draft could act on a domain that isn't the target firm's.

    assess_company() reconciles this before storage (see
    _reconcile_digital_presence there), so this guard should almost always
    PASS. It exists as a backstop: if it ever fires, the reconciliation
    regex missed a phrasing and needs widening — treat a WARNING from this
    guard as a signal to update that pattern list, not just noise.

    Cost: 0 tokens (pure Python)
    """

    GUARD_ID = "EG-DPRES-005"
    GUARD_NAME = "Digital Presence Consistency Guard"

    _MISMATCH_RE = re.compile(
        r"belongs to (a |an )?(different|separate|unrelated) (company|firm|entity)"
        r"|is a (different|separate) (company|firm|entity)"
        r"|crawled content belongs to"
        r"|not (their|this firm'?s|the company'?s) (official )?website"
        r"|no official website .* was identified"
        r"|unrelated to this (company|firm)",
        re.IGNORECASE,
    )

    def evaluate(self, enrichment: dict[str, Any]) -> GuardResult:
        steps: list[str] = []

        narrative = enrichment.get("narrative_assessment") or {}
        if isinstance(narrative, str):
            import json
            try:
                narrative = json.loads(narrative)
            except Exception:
                narrative = {}

        digital = enrichment.get("digital_presence") or narrative.get("digital_presence") or {}
        has_website = bool(digital.get("has_website"))
        domain = digital.get("domain")

        steps.append(f"digital_presence.has_website={has_website}, domain={domain!r}")

        if not has_website:
            return GuardResult(
                guard_id=self.GUARD_ID, guard_name=self.GUARD_NAME,
                passed=True, score=100.0,
                reason="No website claimed — nothing to reconcile.",
                reasoning_steps=steps,
            )

        text_blob = " ".join([
            str(enrichment.get("executive_summary") or narrative.get("executive_summary") or ""),
            " ".join(str(x) for x in ((enrichment.get("personalisation") or narrative.get("personalisation") or {}).get("avoid", []))),
            str(((enrichment.get("opportunity_signal") or narrative.get("opportunity_signal") or {}).get("digital_maturity") or {}).get("reason") or ""),
        ])

        if self._MISMATCH_RE.search(text_blob):
            steps.append("Assessment text indicates crawled domain belongs to a different company, but has_website is still True")
            return GuardResult(
                guard_id=self.GUARD_ID, guard_name=self.GUARD_NAME,
                passed=True, score=20.0,
                reason=(
                    f"digital_presence claims domain '{domain}' but the assessment's own text "
                    f"says it belongs to a different company. Reconciliation in assess_company() "
                    f"should have caught this — widen the mismatch pattern list."
                ),
                reasoning_steps=steps,
                is_warning=True,
            )

        steps.append("No contradiction detected")
        return GuardResult(
            guard_id=self.GUARD_ID, guard_name=self.GUARD_NAME,
            passed=True, score=100.0,
            reason="digital_presence is consistent with the assessment's own reasoning.",
            reasoning_steps=steps,
        )


# ---------------------------------------------------------------------------
# Guard registry (ordered: fail-fast, cheapest first)
# ---------------------------------------------------------------------------

GUARD_PIPELINE: list[Guard] = [
    ConfidenceThresholdGuard(),          # Fastest check — single numeric value
    ExecutiveSummaryGuard(),             # String checks only
    EvidenceQualityGuard(),              # URL parsing — slightly more work
    OpportunitySignalGuard(),            # Dict/shape checks on the scorecard
    DigitalPresenceConsistencyGuard(),   # Regex over summary/avoid/reason text
]
