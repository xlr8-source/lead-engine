-- migrate_to_postgres.sql
-- Companion file for the PayBrix Irish Lead Engine.
-- This is the type-mapping reference for migrating leads_vault.db → PostgreSQL.
-- Not executed in the prototype — present to demonstrate the migration story.

-- ============================================================
-- TYPE MAPPING REFERENCE
-- ============================================================
-- SQLite (prototype)          PostgreSQL (production)
-- -----------------------------------------------------------
-- TEXT (uuid4)             →  UUID  (use gen_random_uuid() as default)
-- TEXT (ISO-8601 ts)       →  TIMESTAMPTZ
-- INTEGER (0/1 flag)       →  BOOLEAN
-- TEXT (json blob)         →  JSONB
-- REAL                     →  NUMERIC or DOUBLE PRECISION
-- TEXT (general)           →  TEXT  (no change needed)
-- INTEGER (score 0-100)    →  SMALLINT  (range is small)
-- ============================================================

-- Enable UUID extension if not already active
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- COMPANIES
-- ============================================================
CREATE TABLE IF NOT EXISTS companies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cbi_reference       TEXT,
    cro_number          TEXT UNIQUE,
    legal_name          TEXT NOT NULL,
    trading_name        TEXT,
    cro_status          TEXT,
    incorporation_date  TIMESTAMPTZ,
    registered_address  TEXT,
    county              TEXT,
    eircode             TEXT,
    sector_tag          TEXT DEFAULT 'insurance_intermediary',
    source              TEXT NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_payload         JSONB
);

-- ============================================================
-- CONTACTS
-- ============================================================
CREATE TABLE IF NOT EXISTS contacts (
    id                TEXT PRIMARY KEY,   -- cast from uuid4 TEXT on migration
    company_id        UUID NOT NULL REFERENCES companies(id),
    full_name         TEXT,
    role_title        TEXT,
    email             TEXT,
    phone             TEXT,
    confidence_score  DOUBLE PRECISION,
    source            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- ENRICHMENT
-- ============================================================
CREATE TABLE IF NOT EXISTS enrichment (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id            UUID NOT NULL REFERENCES companies(id),
    employee_band         TEXT,
    recommended_angle     TEXT,
    billing_pain_points   JSONB,           -- was TEXT json blob in SQLite
    qualification_score   SMALLINT,        -- 0-100
    llm_model             TEXT,
    llm_raw_response      TEXT,
    generated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- OUTREACH EMAILS
-- ============================================================
CREATE TABLE IF NOT EXISTS outreach_emails (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id          UUID NOT NULL REFERENCES companies(id),
    contact_id          UUID REFERENCES contacts(id),
    subject             TEXT,
    body                TEXT,
    compliance_footer   BOOLEAN DEFAULT TRUE,
    llm_model           TEXT,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status              TEXT DEFAULT 'draft'
);

-- ============================================================
-- INGESTION RUNS
-- ============================================================
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    records_found   INTEGER,
    records_new     INTEGER,
    errors          JSONB        -- was TEXT json array in SQLite
);

-- ============================================================
-- INDEXES (mirrors SQLite indexes)
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_companies_county     ON companies(county);
CREATE INDEX IF NOT EXISTS idx_companies_cro_status ON companies(cro_status);
CREATE INDEX IF NOT EXISTS idx_enrichment_company   ON enrichment(company_id);
CREATE INDEX IF NOT EXISTS idx_enrichment_score     ON enrichment(qualification_score);
CREATE INDEX IF NOT EXISTS idx_outreach_company     ON outreach_emails(company_id);
CREATE INDEX IF NOT EXISTS idx_outreach_status      ON outreach_emails(status);
