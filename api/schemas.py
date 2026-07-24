"""Pydantic models for API request/response validation."""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Company Models
# ---------------------------------------------------------------------------

class CompanyBase(BaseModel):
    """Base company model with common fields."""
    cbi_reference: Optional[str] = None
    cro_number: Optional[str] = None
    legal_name: str
    trading_name: Optional[str] = None
    cro_status: Optional[str] = None
    incorporation_date: Optional[str] = None
    registered_address: Optional[str] = None
    county: Optional[str] = None
    eircode: Optional[str] = None
    company_type: Optional[str] = None


class Company(CompanyBase):
    """Full company model with database fields."""
    id: str
    sector_tag: str = "insurance_intermediary"
    source: str
    ingested_at: str
    qualification_score: Optional[int] = None
    employee_band: Optional[str] = None
    recommended_angle: Optional[str] = None
    email_status: Optional[str] = None
    assessed_at: Optional[str] = None

    class Config:
        from_attributes = True


class CompanyListResponse(BaseModel):
    """Response model for company list endpoint."""
    leads: List[Company]
    total: int


class CompanyDetailResponse(BaseModel):
    """Response model for company detail endpoint."""
    id: str
    cbi_reference: Optional[str] = None
    cro_number: Optional[str] = None
    legal_name: str
    trading_name: Optional[str] = None
    cro_status: Optional[str] = None
    incorporation_date: Optional[str] = None
    registered_address: Optional[str] = None
    county: Optional[str] = None
    eircode: Optional[str] = None
    sector_tag: str
    company_type: Optional[str] = None
    last_annual_return: Optional[str] = None
    last_accounts_date: Optional[str] = None
    principal_object: Optional[str] = None
    source: str
    ingested_at: str
    raw_payload: Optional[str] = None
    enrichment: Optional[dict] = None
    email: Optional[dict] = None
    contacts: List[dict] = []
    employee_band: Optional[str] = None
    qualification_score: Optional[int] = None
    recommended_angle: Optional[str] = None
    email_status: Optional[str] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Enrichment Models
# ---------------------------------------------------------------------------

class EnrichmentRequest(BaseModel):
    """Request model for enrichment operations."""
    company_id: str = Field(..., description="Company UUID to enrich")


class EnrichmentResponse(BaseModel):
    """Response model for enrichment operations."""
    status: str
    message: Optional[str] = None
    run_id: Optional[str] = None


class BulkEnrichmentRequest(BaseModel):
    """Request model for bulk enrichment operations."""
    limit: Optional[int] = Field(None, ge=0, description="Maximum number of companies to enrich")


class BulkEnrichmentResponse(BaseModel):
    """Response model for bulk enrichment operations."""
    status: str
    attempted: int
    enriched: int
    rejected: int = 0   # assessed cleanly, then blocked by the guard pipeline
    failed: int
    errors: List[dict] = []


# ---------------------------------------------------------------------------
# Email Models
# ---------------------------------------------------------------------------

class EmailRequest(BaseModel):
    """Request model for email generation."""
    company_id: str = Field(..., description="Company UUID to generate email for")


class EmailResponse(BaseModel):
    """Response model for email generation."""
    status: str
    subject: str
    detail: Optional[str] = None
    run_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Ingestion Models
# ---------------------------------------------------------------------------

class IngestionResponse(BaseModel):
    """Response model for ingestion operations."""
    status: str
    run_id: Optional[str] = None
    records_found: int = 0
    records_new: int = 0
    errors: List[str] = []


# ---------------------------------------------------------------------------
# Stats Models
# ---------------------------------------------------------------------------

class StatsResponse(BaseModel):
    """Response model for statistics endpoint."""
    total_companies: int
    assessed: int
    awaiting: int
    strong: int
    moderate: int
    lower: int
    needs_review: int
    qualified: int
    average_fit: float
    outreach_ready: int
    model: str


class CountiesResponse(BaseModel):
    """Response model for counties endpoint."""
    counties: List[str]


# ---------------------------------------------------------------------------
# Error Models
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    """Standard error response model."""
    error: str
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------------------------

def validate_sort_column(value: str) -> str:
    """Validate sort column is in allowed set."""
    allowed = {"score", "name", "county", "status", "incorporated", "size"}
    if value not in allowed:
        raise ValueError(f"Sort column must be one of: {', '.join(allowed)}")
    return value


def validate_sort_direction(value: str) -> str:
    """Validate sort direction."""
    if value.lower() not in {"asc", "desc"}:
        raise ValueError("Sort direction must be 'asc' or 'desc'")
    return value.lower()
