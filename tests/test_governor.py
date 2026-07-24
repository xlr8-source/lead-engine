import sys
from pathlib import Path
import pytest
from pydantic import ValidationError

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.governor.runner import run_guards
from engine.governor.schemas import EnrichmentSchema
from engine.governor.guards import Guard, GuardResult, EvidenceQualityGuard, ConfidenceThresholdGuard, ExecutiveSummaryGuard


class _CrashingGuard(Guard):
    GUARD_ID = "EG-TEST-BOOM"
    GUARD_NAME = "Deliberately Broken Guard"

    def evaluate(self, enrichment):
        raise RuntimeError("guard implementation bug")


class _CleanGuard(Guard):
    GUARD_ID = "EG-TEST-OK"
    GUARD_NAME = "Always Passes"

    def evaluate(self, enrichment):
        return GuardResult(
            guard_id=self.GUARD_ID, guard_name=self.GUARD_NAME,
            passed=True, score=100.0, reason="fine",
        )


def test_schema_valid_enrichment():
    data = {
        "qualification_score": 75,
        "signal_strength": "medium",
        "research_confidence": 85.0,
        "executive_summary": (
            "This company is a regulated insurance intermediary based in Dublin, "
            "crawled successfully from their main web page. They focus on commercial "
            "lines and require automated premium billing journeys."
        ),
        "sources_reviewed": ["https://commercialinsurance.ie", "https://cbi.ie/1234"],
        "recommended_angle": "Receivables automation for commercial brokers",
        "billing_pain_points": ["Manual bank reconciliations"],
        "assessment_breakdown": {},
        "narrative_assessment": {},
    }
    validated = EnrichmentSchema.model_validate(data)
    assert validated.qualification_score == 75
    assert validated.signal_strength == "medium"


def test_schema_invalid_fit_score():
    data = {
        "qualification_score": 120,  # Out of range 0-100
        "signal_strength": "high",
        "research_confidence": 90.0,
        "executive_summary": "Substantive summary that meets the length check guidelines.",
        "sources_reviewed": ["https://source1.ie", "https://source2.ie"],
        "recommended_angle": "Test",
    }
    with pytest.raises(ValidationError):
        EnrichmentSchema.model_validate(data)


def test_schema_low_confidence_missing_explanation():
    data = {
        "qualification_score": 40,
        "signal_strength": "low",
        "research_confidence": 35.0,  # Low confidence
        "executive_summary": "Standard summary that has no qualifying words at all.",
        "sources_reviewed": ["https://source1.ie"],
        "recommended_angle": "Test",
    }
    with pytest.raises(ValidationError) as excinfo:
        EnrichmentSchema.model_validate(data)
    assert "research_confidence is 35% but executive_summary does not acknowledge" in str(excinfo.value)


def test_evidence_quality_guard_insufficient_sources():
    """Single external source (no default) = FAIL. Need 2 external OR 1 default + 1 external."""
    enrichment = {
        "sources_reviewed": ["https://onlyone.ie"],
        "narrative_assessment": {},
    }
    guard = EvidenceQualityGuard()
    result = guard.evaluate(enrichment)
    assert not result.passed
    assert "external" in result.reason.lower()
    assert "1 external" in result.reason


def test_evidence_quality_guard_single_domain_bias():
    """Two external sources from same domain = PASS (2 external sources count)."""
    enrichment = {
        "sources_reviewed": ["https://onlyone.ie/page1", "https://onlyone.ie/page2"],
        "narrative_assessment": {},
    }
    guard = EvidenceQualityGuard()
    result = guard.evaluate(enrichment)
    assert result.passed
    assert "external" in result.reason.lower()


def test_confidence_threshold_guard_hard_fail():
    enrichment = {"research_confidence": 25.0}
    guard = ConfidenceThresholdGuard()
    result = guard.evaluate(enrichment)
    assert not result.passed
    assert "below minimum threshold" in result.reason


def test_executive_summary_guard_too_short():
    enrichment = {"executive_summary": "Too short"}
    guard = ExecutiveSummaryGuard()
    result = guard.evaluate(enrichment)
    assert not result.passed
    assert "too short" in result.reason


def test_crashed_guard_is_reported_as_a_warning_not_a_pass():
    """A guard that raises must degrade gracefully — but it did so by
    synthesising `passed=True, score=50.0`, i.e. recording a check that never
    ran as a half-decent pass. It must be flagged instead."""
    report = run_guards({}, pipeline=[_CrashingGuard()])

    assert "EG-TEST-BOOM" in report.warning_guards
    result = report.guards_run[0]
    assert result.is_warning is True
    assert result.errored is True


def test_crashed_guard_is_excluded_from_the_overall_score():
    """One clean guard at 100 plus one crashed guard used to average to 75,
    inventing a 25-point penalty out of a check that produced no evidence at
    all. /api/guard-stats reports that average."""
    report = run_guards({}, pipeline=[_CleanGuard(), _CrashingGuard()])

    assert report.overall_score == 100.0


def test_pipeline_where_every_guard_crashed_scores_zero_not_fifty():
    """No guard produced evidence, so there is no quality signal to report."""
    report = run_guards({}, pipeline=[_CrashingGuard()])

    assert report.overall_score == 0.0


def test_crashed_guard_does_not_block_the_pipeline():
    """The graceful-degradation intent is preserved: a guard bug must not
    take down every assessment."""
    report = run_guards({}, pipeline=[_CrashingGuard(), _CleanGuard()])

    assert report.passed is True
    assert len(report.guards_run) == 2


def test_executive_summary_guard_fallback_phrase():
    enrichment = {"executive_summary": "No information available at this time. We could not find any website or other information about this broker."}
    guard = ExecutiveSummaryGuard()
    result = guard.evaluate(enrichment)
    assert not result.passed
    assert "generic fallback" in result.reason

