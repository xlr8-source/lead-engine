import sys
from pathlib import Path
import pytest
from pydantic import ValidationError

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.governor.runner import run_guards
from engine.governor.schemas import EnrichmentSchema
from engine.governor.guards import EvidenceQualityGuard, ConfidenceThresholdGuard, ExecutiveSummaryGuard


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


def test_executive_summary_guard_fallback_phrase():
    enrichment = {"executive_summary": "No information available at this time. We could not find any website or other information about this broker."}
    guard = ExecutiveSummaryGuard()
    result = guard.evaluate(enrichment)
    assert not result.passed
    assert "generic fallback" in result.reason

