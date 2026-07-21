"""
engine/governor/schemas.py
Pydantic v2 schema for LLM enrichment output validation.

This is the contract between the LLM and the database. Any output that fails
validation here is rejected before it can corrupt the audit trail.
"""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class OpportunityDimension(BaseModel):
    """
    One scored dimension of the opportunity_signal scorecard (business_fit,
    regulatory_fit, digital_maturity, evidence_coverage, payment_visibility,
    decision_maker_access).

    `reason` is mandatory and is what the UI shows next to the score bar —
    previously this whole field was typed as a bare string and silently
    never validated, so a dimension could come back with no explanation
    (or the wrong shape) and nothing would catch it before it hit the DB.
    """
    level: Literal["high", "medium", "low"]
    pct: float = Field(ge=0.0, le=100.0)
    reason: str = Field(min_length=10, description="Evidence-grounded explanation for this score")

    @field_validator("reason")
    @classmethod
    def reason_must_be_specific(cls, v: str) -> str:
        v = v.strip()
        generic = {
            "no information available", "n/a", "unknown", "unclear",
            "based on available evidence", "standard assessment",
        }
        if v.lower() in generic:
            raise ValueError(f"reason '{v}' is too generic to explain the score")
        return v


class OpportunitySignal(BaseModel):
    business_fit: OpportunityDimension
    regulatory_fit: OpportunityDimension
    digital_maturity: OpportunityDimension
    evidence_coverage: OpportunityDimension
    payment_visibility: OpportunityDimension
    decision_maker_access: OpportunityDimension


class ConfidenceField(BaseModel):
    """One labeled confidence judgement with a stated reason — the building
    block of ContactConfidence. Never a bare number: "trust me" isn't
    verifiable, "here's why" is."""
    level: Literal["high", "medium", "low"]
    reason: str = Field(min_length=8, description="Why this confidence level, specifically")


class ContactConfidence(BaseModel):
    """
    Per-field contact confidence, replacing a single blended 0-100 number.

    Directly implements the sales-enablement mandate: "Can the salesperson
    trust this contact enough to act on it?" needs a defensible, explainable
    answer per channel, not one number that hides which parts are solid and
    which are guesses. A contact with a rock-solid name/role but an inferred
    email should not look as trustworthy overall as one where every field is
    independently confirmed.
    """
    identity: ConfidenceField
    role: ConfidenceField
    email: ConfidenceField
    phone: ConfidenceField
    linkedin: ConfidenceField
    freshness: ConfidenceField
    overall: ConfidenceField


class Contact(BaseModel):
    """A single contact candidate. email/phone/linkedin_url are nullable —
    a contact identified by name+role alone, with no channel found, is
    still useful (it tells the salesperson who to look for), so absence of
    a channel isn't itself a validation failure. What IS required is that
    every channel — present or absent — has a stated confidence and reason,
    so "we don't know" and "we're not sure" are distinguishable from "we
    verified this."""
    name: str = Field(min_length=1)
    role: str | None = None
    detail: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    confidence: ContactConfidence


class EnrichmentSchema(BaseModel):
    """
    Validates the structured output returned by the LLM assessment step.
    Enforces the Constitutional AI principles:
      - Honesty: sources must be real, diverse, and recent
      - Helpfulness: summary must be actionable and substantive
      - Harmlessness: confidence must be justified, not inflated
    """

    # Core scoring
    qualification_score: int = Field(ge=0, le=100, description="0–100 fit score")
    signal_strength: Literal["high", "medium", "low"] = Field(
        description="Categorical signal strength"
    )

    # Evidence quality (guard EG-QUAL-001 will re-validate, but schema catches obvious failures)
    sources_reviewed: list[str] = Field(
        min_length=0, description="List of source URLs/identifiers reviewed"
    )

    # Confidence
    research_confidence: float = Field(
        ge=0.0, le=100.0, description="Researcher confidence 0–100"
    )

    # Narrative
    executive_summary: str = Field(
        min_length=1, description="Human-readable assessment summary"
    )
    recommended_angle: str = Field(
        min_length=1, description="Recommended sales angle"
    )
    opportunity_signal: OpportunitySignal | None = Field(default=None)
    personalisation: dict[str, Any] | None = Field(default=None)
    opening_angle: str | None = Field(default=None)
    contacts: list[Contact] | None = Field(default=None)

    # Structured data
    billing_pain_points: list[str] = Field(default_factory=list)
    discovery_questions: list[str] = Field(default_factory=list)
    assessment_breakdown: dict[str, Any] = Field(default_factory=dict)
    narrative_assessment: dict[str, Any] = Field(default_factory=dict)

    # LLM metadata (injected after call, not validated strictly)
    llm_model: str | None = Field(default=None)

    @field_validator("qualification_score")
    @classmethod
    def score_must_be_valid(cls, v: int) -> int:
        if not (0 <= v <= 100):
            raise ValueError(f"qualification_score {v} is outside 0–100 range")
        return v

    @field_validator("executive_summary")
    @classmethod
    def summary_must_be_substantive(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 30:
            raise ValueError(
                f"executive_summary too short ({len(v)} chars). Must be ≥ 30 chars."
            )
        # Reject obvious fallback strings
        FALLBACK_PHRASES = [
            "no information available",
            "unable to assess",
            "insufficient data",
            "no data found",
            "n/a",
        ]
        lower = v.lower()
        for phrase in FALLBACK_PHRASES:
            if lower.startswith(phrase):
                raise ValueError(
                    f"executive_summary appears to be a generic fallback: '{v[:80]}'"
                )
        return v

    @model_validator(mode="after")
    def low_confidence_must_have_explanation(self) -> "EnrichmentSchema":
        """
        Honesty principle: if confidence is below 70, the summary must
        acknowledge the limitation (contain qualifying language).
        """
        if self.research_confidence < 50:
            qualifying_words = [
                "limited", "insufficient", "unclear", "uncertain",
                "no website", "no online", "could not", "unable",
                "only cro", "only cbi", "register only",
            ]
            summary_lower = self.executive_summary.lower()
            if not any(w in summary_lower for w in qualifying_words):
                raise ValueError(
                    f"research_confidence is {self.research_confidence:.0f}% but "
                    "executive_summary does not acknowledge the data limitation. "
                    "Add qualifying language when confidence < 50%."
                )
        return self


class GuardResultSchema(BaseModel):
    """Schema for a single guard's evaluation result."""
    guard_id: str
    guard_name: str
    passed: bool
    score: float = Field(ge=0.0, le=100.0)
    reason: str
    reasoning_steps: list[str] = Field(default_factory=list)


class GuardReportSchema(BaseModel):
    """Aggregate report from running all guards."""
    passed: bool
    overall_score: float = Field(ge=0.0, le=100.0)
    guards_run: list[GuardResultSchema] = Field(default_factory=list)
    failed_guards: list[str] = Field(default_factory=list)
    warning_guards: list[str] = Field(default_factory=list)
