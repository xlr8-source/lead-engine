-- PayBrix Irish Lead Engine — Database Schema
-- SQLite (prototype) with Postgres-portable types only.
-- All PKs are uuid4 TEXT. No AUTOINCREMENT. No SQLite-only types.

CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,                 -- uuid4
    cbi_reference TEXT,
    cro_number TEXT UNIQUE,
    legal_name TEXT NOT NULL,
    legal_name_normalized TEXT,          -- ingestion.cro_resolver.normalize_legal_name(legal_name); dedup key when cro_number is absent (index added in init_db migrations)
    trading_name TEXT,
    cro_status TEXT,
    incorporation_date TEXT,             -- ISO-8601
    registered_address TEXT,
    county TEXT,
    eircode TEXT,
    sector_tag TEXT DEFAULT 'insurance_intermediary',
    company_type TEXT,
    last_annual_return TEXT,
    last_accounts_date TEXT,
    principal_object TEXT,
    source TEXT NOT NULL,
    ingested_at TEXT NOT NULL,           -- ISO-8601
    raw_payload TEXT                     -- json blob, audit trail
);

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,                 -- uuid4
    company_id TEXT NOT NULL REFERENCES companies(id),
    full_name TEXT,
    role_title TEXT,
    email TEXT,
    phone TEXT,
    confidence_score REAL,
    source TEXT,
    created_at TEXT NOT NULL             -- ISO-8601
);

CREATE TABLE IF NOT EXISTS enrichment (
    id TEXT PRIMARY KEY,                 -- uuid4
    company_id TEXT NOT NULL REFERENCES companies(id),
    employee_band TEXT,
    recommended_angle TEXT,
    billing_pain_points TEXT,            -- json array (stored as TEXT in SQLite → JSONB in Postgres)
    qualification_score INTEGER,         -- 0-100 (deprecated, kept for existing data)
    assessment_breakdown TEXT,           -- json dict of evidence factors (no scores)
    narrative_assessment TEXT,           -- json: {verified_evidence, ai_reasoning, hypotheses, questions}
    signal_strength TEXT DEFAULT 'low',  -- high | medium | low
    guard_passed INTEGER DEFAULT 0,      -- 0 or 1 (boolean), NULL = guards skipped.
                                         -- Defaults to 0: a row written without
                                         -- a verdict has not been shown to pass.
    guard_score REAL,                    -- 0.0–100.0 weighted guard pipeline score
    guard_failures TEXT,                 -- json array of failed guard IDs (e.g. ["EG-QUAL-001"])
    llm_model TEXT,
    llm_raw_response TEXT,
    generated_at TEXT NOT NULL           -- ISO-8601
);

CREATE TABLE IF NOT EXISTS outreach_emails (
    id TEXT PRIMARY KEY,                 -- uuid4
    company_id TEXT NOT NULL REFERENCES companies(id),
    contact_id TEXT REFERENCES contacts(id),
    subject TEXT,
    body TEXT,
    compliance_footer INTEGER DEFAULT 1, -- boolean (0/1 in SQLite → BOOLEAN in Postgres)
    llm_model TEXT,
    generated_at TEXT NOT NULL,          -- ISO-8601
    status TEXT DEFAULT 'draft'
);

-- An assessment that ran cleanly and was then refused storage by the guard
-- pipeline (GUARD_ENFORCEMENT=block). This is a real outcome, not an error:
-- without it a rejected firm is indistinguishable from one nobody has looked
-- at yet, and the reason lived only in an in-memory run registry that a
-- restart erased. At most one live row per company — cleared when a later
-- assessment for that company succeeds.
CREATE TABLE IF NOT EXISTS assessment_rejections (
    id TEXT PRIMARY KEY,                 -- uuid4
    company_id TEXT NOT NULL REFERENCES companies(id),
    reason TEXT NOT NULL,                -- human-readable, names the failed guard(s)
    guard_failures TEXT,                 -- json array of guard IDs
    guard_score REAL,                    -- 0.0–100.0 pipeline score at rejection
    llm_model TEXT,
    rejected_at TEXT NOT NULL            -- ISO-8601
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id TEXT PRIMARY KEY,                 -- uuid4
    source TEXT,
    started_at TEXT,                     -- ISO-8601
    finished_at TEXT,                    -- ISO-8601
    records_found INTEGER,
    records_new INTEGER,
    errors TEXT                          -- json array of error strings
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_companies_county ON companies(county);
CREATE INDEX IF NOT EXISTS idx_companies_cro_status ON companies(cro_status);
-- Expression indexes matching the LOWER(...) filters actually used in
-- db/dal.py's get_companies/count_companies — the plain indexes above
-- can't be used by SQLite's planner when the query wraps the column in
-- LOWER(), which is what every county/status filter does. Both sets of
-- indexes are kept: these for the case-insensitive filter queries, the
-- plain ones above in case anything filters on exact case elsewhere.
CREATE INDEX IF NOT EXISTS idx_companies_county_lower ON companies(LOWER(county));
CREATE INDEX IF NOT EXISTS idx_companies_cro_status_lower ON companies(LOWER(cro_status));
CREATE INDEX IF NOT EXISTS idx_enrichment_company ON enrichment(company_id);
CREATE INDEX IF NOT EXISTS idx_enrichment_score ON enrichment(qualification_score);
CREATE INDEX IF NOT EXISTS idx_outreach_company ON outreach_emails(company_id);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach_emails(status);
CREATE INDEX IF NOT EXISTS idx_rejections_company ON assessment_rejections(company_id);
