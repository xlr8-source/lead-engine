# Lead Engine

[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115.5-green.svg)](https://fastapi.tiangolo.com)
[![License: Proprietary](https://img.shields.io/badge/License-Proprietary-red.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-Prototype%20v1.1-orange.svg)](#v11-changes)

**PayBrix Lead Engine** — an AI-driven lead qualification and outreach system that identifies Irish insurance intermediaries most likely to benefit from PayBrix's payment-collection platform, using public regulatory data (CRO + Central Bank of Ireland registers) as its evidence base.

The distinguishing part of this codebase isn't the CRUD or the dashboard — it's the `engine/` package: a research → assess → guard pipeline that treats "the LLM said so" as insufficient evidence on its own, and validates every generated assessment — and, as of v1.1, every generated outreach email — against Constitutional-AI-style guards before it's allowed to reach the database.

---

## Table of Contents
- [v1.1 Changes](#v11-changes)
- [Overview](#overview)
- [Architecture](#architecture)
- [Deep Dive: the `engine/` package](#deep-dive-the-engine-package)
  - [`assessor.py` — orchestration](#assessorpy--orchestration)
  - [`researcher.py` — web research & domain matching](#researcherpy--web-research--domain-matching)
  - [`research/` — extraction & confidence scoring](#research--extraction--confidence-scoring)
  - [`llm/` — provider abstraction](#llm--provider-abstraction)
  - [`governor/` — the guard pipeline](#governor--the-guard-pipeline)
  - [`prompts/` — prompt management](#prompts--prompt-management)
- [Tech Stack](#tech-stack)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage / Quickstart](#usage--quickstart)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)
- [Testing](#testing)
- [Known Issues / Security Notes](#known-issues--security-notes)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)

---

## v1.1 Changes

- **Opportunity Signal is now explainable.** Every dimension (`business_fit`, `regulatory_fit`, `digital_maturity`, `evidence_coverage`, `payment_visibility`, `decision_maker_access`) now carries a mandatory, evidence-grounded `reason`, validated by schema and a new guard (`EG-DIM-004`), and rendered in the UI under each score bar. Previously `opportunity_signal` was typed as a bare string in the Pydantic schema (never actually validated, and the real data was popped out of the dict before validation ran regardless) and the score bars used a hardcoded 90/58/18 width by level instead of the model's real percentage — both fixed.
- **Outreach email quality guard added** (`engine/governor/email_guard.py`) — checks generated emails for sales clichés, competitor mentions, unfilled placeholders, a known generic-pitch pattern, and lexical grounding against the actual company, with one automatic corrective retry. Emails previously went straight to storage with only a "subject/body non-empty" check — no equivalent of the assessment guard pipeline existed for them.
- **Fixed the structural cause of generic/repetitive emails**: `outreach_user.md` instructed the model to *rephrase rather than reuse* the already-specific `opening_angle`, and hardcoded a fixed "explain what PayBrix does in one sentence" slot for paragraph 2 — both forced similar company-agnostic phrasing across every lead regardless of model quality. Rewritten so paragraph 2 must tie back to the specific fact raised in paragraph 1, and the model is told it can reuse the opening_angle's own phrasing directly.
- **Contacts**: the LLM's own contact extraction (explicitly prompted, with disambiguation instructions) was being discarded every time in favour of a cruder regex-based extractor. Now the LLM's contacts are used when well-formed; regex is the fallback.
- **Assessment latency**: research previously fetched candidate URLs sequentially (up to 4 queries × up to 10 URLs × a 10s timeout each) — a single assessment could take 2–3 minutes, almost entirely spent waiting on network I/O. Fetching within a query is now parallelised across a bounded thread pool; the research phase dropped from ~125s to ~6s in testing on the same company, with identical scoring/output. The fallback path also no longer re-fetches URLs already fetched earlier in the same run.
- **`db/dal.py`** filtered on `LOWER(county)`/`LOWER(cro_status)`, which SQLite can't serve from the plain column indexes — added matching expression indexes (`idx_companies_county_lower`, `idx_companies_cro_status_lower`) so these filters actually hit an index instead of a full scan.
- **`researcher.py`**: replaced hand-rolled regex HTML stripping (which never decoded HTML entities, e.g. `&amp;`) with BeautifulSoup, and a hand-rolled website-match heuristic with `rapidfuzz` — both were already project dependencies, just unused in this file. Removed ~50 lines of dead code (`_looks_like_company_website`, zero callers anywhere).
- **`tavily_search.py`**: removed a hardcoded API key that shipped as the `os.getenv()` fallback default, and an entirely unused async duplicate of the search function.
- **Frontend dead code removed**: `lead.htm` (the page actually served at `/lead/{id}`) never loads `app.js` — it has its own self-contained inline `<script>`. `app.js` carried a ~260-line duplicate implementation of the detail page (`loadLeadDetail` and five dedicated helpers) that was permanently unreachable, gated behind a flag that can only be true on a page with no `#leads-tbody` — i.e. never, since `app.js` only ever runs on the dashboard. `lead.htm` itself had a literal duplicate `<div id="d-fit-score-wrap">` block (same id twice) and a duplicate `renderRing` function. All removed.
- Renamed the sidebar "Business Fit Score" badge to "Overall Fit Score" — it shows `qualification_score` (the overall 0–100 fit score), which was colliding in name with the separate `business_fit` opportunity-signal dimension shown just below it.
- Cleaned up `scratch/` — removed debug scripts tied to a search backend no longer used by the project.

---

## Overview

The engine ingests companies from CBI (Central Bank of Ireland) regulatory registers, cross-resolves them against the CRO (Companies Registration Office), then for each company:

1. **Researches** it — searches the open web for its official site and digital footprint
2. **Assesses** it — an LLM produces a structured qualification score, executive summary, and outreach angle, grounded in the research + registry data
3. **Validates** the assessment — a Pydantic schema plus a four-guard pipeline check the LLM's output for evidence quality, confidence calibration, summary substance, and opportunity-signal explainability *before* it's allowed to reach storage
4. **Generates outreach** — a personalized email draft using the stored, validated assessment, itself checked by a dedicated email-quality guard before being surfaced

Guard verdicts (pass/fail, score, failure reasons) are stored directly alongside each assessment, so the evidence behind any score is inspectable via `/api/guard-stats` or the stored guard report — no separate audit system to keep in sync.

---

## Architecture

```
lead-engine/
├── api/              # FastAPI backend — REST endpoints, request/response schemas
├── engine/           # AI research, assessment, and guard logic (see deep dive below)
│   ├── governor/     # Constitutional-AI-style guard pipeline + Pydantic contracts
│   ├── llm/          # Multi-provider LLM abstraction (LiteLLM-based)
│   ├── research/     # Tavily search + signal extraction from research results
│   └── prompts/      # System/user prompt templates, loaded by prompt_loader.py
├── ingestion/        # CBI register fetch + parse + CRO cross-resolution
├── db/               # SQLite data-access layer, schema, Postgres migration script
├── frontend/         # Dashboard UI (vanilla HTML/CSS/JS, no build step)
├── data/             # Cached register downloads
├── tests/            # Pytest suite (currently: governor pipeline)
└── scratch/          # Ad-hoc dev/debug scripts — not part of the shipped system
```

---

## Deep Dive: the `engine/` package

This is the part of the repo doing the actual intellectual work. Five sub-areas, in the order data flows through them.

### `assessor.py` — orchestration

`assess_company(company: dict) -> dict` is the pipeline entry point. It doesn't do research or scoring itself — it calls out to `researcher.py`, `research/extract.py`, and `llm/summarise.py` in sequence, then hands the combined result to the governor for validation before returning it. It also exposes `generate_email(company)`, which deliberately **prefers previously stored assessment data over re-running research** — outreach generation is a fast, cheap path that reuses the expensive research/assessment work already done.

### `researcher.py` — web research & domain matching

Given a company record, this module builds a cascade of search queries (trading name → legal name → CRO/CBI identifiers → county/address → domain guesses), capped at 4 queries per company to bound Tavily API usage. Candidate URLs within each query are fetched concurrently (bounded thread pool, `FETCH_CONCURRENCY=6`) rather than one at a time, and scored against the company's name using `rapidfuzz` fuzzy matching against the domain (and page content, if fetched) — the same approach already used for CRO cross-resolution — with an early-exit once a match scores ≥ 70 so a strong match short-circuits the remaining queries. Fetched page text is extracted with BeautifulSoup (not regex tag-stripping), with HTML entities properly decoded, and cached per-URL so a weak-match fallback doesn't re-fetch pages already pulled earlier in the same run.

### `research/` — extraction & confidence scoring

`extract.py` turns raw research + registry data into the structured fields the governor will later validate:
- `extract_digital_presence` — has a website been found, and what domain
- `extract_research_coverage` — what's verified vs. still missing, in plain language
- `extract_contacts` — named individuals and emails parsed from website text via regex; used as a fallback when the LLM's own contact extraction (from the assessment call) doesn't return a well-formed result
- `compute_research_confidence` — an additive 0–100 score (baseline 20, + CRO/CBI signals, + website found, + named contacts, + public email)
- `compute_sources_reviewed` — the source list the evidence-quality guard will count

`tavily_search.py` wraps the Tavily Search API with an `"ireland"` country bias, having replaced an earlier DuckDuckGo-scraping approach that ran into bot-verification CAPTCHAs.

### `llm/` — provider abstraction

`provider.py` is a LiteLLM-based abstraction that lets `LLM_PROVIDER` be swapped between Groq, OpenAI, Anthropic, xAI, OpenRouter, or NVIDIA purely via environment variables, with an automatic fallback to a bare `openai`-client implementation if `litellm` isn't installed. It supports:
- A configurable **fallback chain** (`LLM_FALLBACK_CHAIN`) tried on rate-limit/failure
- Per-call **cost tracking** via `litellm.completion_cost`
- `complete_json()`, which retries with progressively stricter re-prompting if the model returns malformed or placeholder-looking JSON, and can enforce that specific keys are present with non-empty values before accepting a response

`summarise.py` builds the actual context string sent to the LLM — registry facts first, then a clearly-labeled research section that explicitly instructs the model not to invent details when only search snippets (no full website) are available.

### `governor/` — the guard pipeline

This is the most deliberately-engineered part of the codebase. The core idea: **an LLM's own confidence in its output is not sufficient evidence that the output is good**, so every assessment passes through independent, zero-token, pure-Python checks before being treated as valid.

- **`schemas.py`** — a Pydantic v2 `EnrichmentSchema` that is the hard contract between LLM output and the database. Beyond basic type/range validation, it encodes a genuine honesty rule: if `research_confidence < 50`, the executive summary is *required* to contain qualifying language (e.g. "limited," "unclear," "could not") — an assessment can't claim low confidence while writing as if it were certain.
- **`guards.py`** — four independent guards, ordered cheapest-first for fail-fast efficiency:
  | Guard | Checks | Cost |
  |---|---|---|
  | `EG-CONF-002` Confidence Threshold | Hard-fails below a floor, soft-warns in a middle band | 0 tokens |
  | `EG-SUMM-003` Executive Summary | Rejects empty, too-short, or generic-fallback summaries; warns if no company-specific signal is detected | 0 tokens |
  | `EG-QUAL-001` Evidence Quality | Requires 2+ registry sources, OR 1 registry + 1 external, OR 2+ external sources | 0 tokens |
  | `EG-DIM-004` Opportunity Signal Explainability | Hard-fails if the 6-dimension scorecard is missing entirely; warns on missing dimensions or duplicate/placeholder reasons | 0 tokens |

  A separate, purpose-built check (`engine/governor/email_guard.py`) covers generated outreach emails — clichés, competitor mentions, placeholder text, a known generic-pitch pattern, and lexical grounding against the actual company — with one automatic corrective retry. It isn't part of `GUARD_PIPELINE` because emails are a different object shape produced by a separate LLM call later in the flow, not part of the enrichment dict the other four guards validate.
- **`runner.py`** — runs the pipeline fail-fast (a hard failure skips remaining guards), but a guard *raising an exception* is treated as a non-blocking warning rather than crashing the whole assessment — a bug in a guard degrades gracefully instead of taking down the pipeline.

Schema validation failures are logged as warnings rather than blocking storage — they're surfaced in the guard report for human review instead of silently dropping an otherwise-usable assessment.

### `prompts/` — prompt management

`prompt_loader.py` is a small, deliberately restrictive loader: `load_prompt()` rejects any name that isn't a bare filename (no path traversal), and results are `lru_cache`d since prompt files don't change at runtime. `assessment_system.md` embeds the PayBrix product-fit context directly in the system prompt; `paybrix_product_context.md` in the same folder is reference material with more detail (ICP tiers, sales framing) that isn't currently loaded by any code path — draw from it when updating the live prompts rather than treating it as active configuration.

---

## Tech Stack

- **Backend**: FastAPI, Uvicorn
- **Database**: SQLite (`db/dal.py`), with a documented, not-yet-executed migration path to Postgres (`db/migrate_to_postgres.sql`)
- **LLM layer**: LiteLLM — provider-agnostic across Groq, OpenAI, Anthropic, xAI, OpenRouter, NVIDIA
- **Search**: Tavily Search API
- **Validation**: Pydantic v2
- **Frontend**: Vanilla JavaScript, HTML5, CSS3 — no build step
- **Data processing**: pdfplumber, rapidfuzz, openpyxl

---

## Installation

### Prerequisites
- Python 3.14 (or your installed 3.x — verify with `python --version`)
- pip
- An API key for at least one LLM provider (Groq, NVIDIA, OpenAI, etc.)
- A Tavily API key for web research

### Setup

```bash
git clone https://github.com/xlr8-source/lead-engine.git
cd lead-engine
pip install -r requirements.txt
cp .env.example .env        # then edit .env with your real keys
python -c "from db.init_db import init_db; init_db()"
```

---

## Configuration

All configuration lives in `.env` (copied from `.env.example`). Key variables:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `openai` \| `anthropic` \| `groq` \| `xai` \| `openrouter` \| `nvidia` \| `ollama` \| `custom` |
| `LLM_API_KEY` / `<PROVIDER>_API_KEY` | Provider credentials — generic key is checked first |
| `LLM_MODEL` | Model string; auto-resolves to a sensible default per provider if blank |
| `LLM_FALLBACK_CHAIN` | Ordered `provider:model` pairs tried on failure |
| `TAVILY_API_KEY` | Required for web research |
| `DB_PATH` | SQLite file path (default `leads_vault.db`) |
| `API_HOST` / `API_PORT` / `CORS_ORIGINS` | FastAPI serving config |

---

## Usage / Quickstart

**Start the server**

Windows:
```bash
run_server.bat
```
Linux/Mac:
```bash
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000
```
Dashboard: `http://localhost:8000/`

**Ingest company data**
```bash
python ingestion/runner.py                  # full fetch
python ingestion/runner.py --skip-download  # reuse cached register file
```

**Trigger ingestion via API instead**
```bash
curl -X POST http://localhost:8000/api/ingest
```

---

## API Reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard UI |
| GET | `/lead/{company_id}` | Individual lead detail page |
| GET | `/api/stats` | Aggregate counts |
| GET | `/api/counties` | County filter options |
| GET | `/api/leads` | List/filter leads |
| GET | `/api/leads/{company_id}` | Lead detail (JSON) |
| POST | `/api/ingest` | Run CBI/CRO ingestion |
| POST | `/api/enrich-all` | Bulk-assess unenriched companies |
| POST | `/api/enrich/{company_id}` | Assess a single company |
| POST | `/api/disenrich-all` | Clear stored enrichments |
| POST | `/api/email/{company_id}` | Generate outreach email |
| GET | `/api/guard-stats` | Aggregate guard pass/fail stats |
| POST | `/api/log` | Client-side log ingestion |

Full request/response shapes are defined in `api/schemas.py`.

---

## Project Structure

- **`api/main.py`** — FastAPI app (`PayBrix Lead Engine API`) and all route definitions
- **`engine/assessor.py`** — pipeline orchestration: `assess_company()`, `generate_email()`
- **`engine/researcher.py`** — web research and domain-matching heuristics
- **`engine/research/`** — Tavily search wrapper + signal/confidence extraction
- **`engine/llm/`** — provider-agnostic LLM client with fallback + cost tracking
- **`engine/governor/`** — Pydantic schema + guard pipeline (see deep dive above)
- **`engine/prompts/`** — versioned prompt templates
- **`ingestion/runner.py`** — CBI fetch → parse → CRO cross-resolution pipeline
- **`db/dal.py`** — raw-SQL SQLite data access layer
- **`frontend/`** — dashboard assets

---

## Testing

```bash
python -m pytest tests/test_governor.py
```

Current coverage is limited to the governor/guard pipeline — the research, LLM abstraction, and ingestion layers do not yet have automated tests.

---

## Known Issues / Security Notes

- **Bulk enrichment (`/api/enrich-all`) runs fully serially** — one company's research + LLM call at a time, with no concurrency. This is now the main remaining lever for reducing wall time on large batch runs (single-company assessment latency has been addressed — see v1.1 Changes).
- **Test coverage** is limited to the governor/guard pipeline; the research, LLM abstraction, and ingestion layers do not yet have automated tests.
- Rotate any provider API key that's ever been committed to source, even after the line is removed — the file history is a separate concern from the current code.

---

## Contributing

Open an issue or submit a pull request against the development branch. Ensure `pytest` passes before submitting, and run any configured linters — none are currently pinned in this repo, so match the existing style.

---

## License

This project is licensed under a **restricted, proprietary license for exclusive use by Sremium Limited and its members**. See [LICENSE](LICENSE) for the full text — this is *not* an open-source (MIT) license.

---

## Acknowledgments

Built for PayBrix, a payment-collection and receivables-automation platform for insurance intermediaries, developed by Sremium.

## 📡 Session Log

> Machine-readable project history. One entry per working session or push. **Do not hand-edit the JSON block below** — use `scripts/new_session_entry.py` (from the `github-collab-init` skill), which pulls the real commit range and file list from git.

<!-- SESSION_LOG_START
[
  {
    "id": "S-0000",
    "date": "2026-07-21T14:51:39Z",
    "author": "xlr8-source",
    "branch": "main",
    "commit_range": "none..none",
    "summary": "Repository history consolidated into a single clean commit. Cumulative state as of this reset: ingestion pipeline hardened per the 2026-07-20 Product Board audit (fuzzy CRO near-miss handling, degraded-run reporting, normalized-name dedup, retry-with-backoff on external fetches; 42/100 to 80/100). Enrichment research layer hardened against five field-observed failures: unverified websites no longer attached without identity proof, hallucinated contacts filtered before storage, team/staff pages discovered via nav links instead of a fixed path list, research parallelized (~13s/firm), and two prompt bugs fixed (personalisation ordering, generate_email reading the correct key). Validated against hand-researched ground truth on a real 15-firm run plus a deep single-firm methodology. UI shows assessment date/time. Repository hardened for collaboration: LICENSE restricted to Sremium Limited and its members, SECURITY.md and CODE_OF_CONDUCT.md added, CI precision-audit wired up (scripts/validate_repo.py, scripts/new_session_entry.py), and a dead Neo4j dependency fully removed from code, config, and docs.",
    "files_changed": [
      "README.md",
      "LICENSE",
      "SECURITY.md",
      "CODE_OF_CONDUCT.md",
      "CONTRIBUTING.md",
      ".gitignore",
      ".github/",
      "scripts/",
      "engine/",
      "ingestion/",
      "db/",
      "api/",
      "frontend/",
      "tests/"
    ],
    "tests": "63/63 passing (tests/test_ingestion_pipeline.py, tests/test_engine_research.py, tests/test_governor.py)",
    "status": "done"
  }
]
SESSION_LOG_END -->

| Session | Date (UTC) | Author | Branch | Summary | Status |
|---|---|---|---|---|---|
| S-0000 | 2026-07-21 14:51:39 | xlr8-source | main | Repository history consolidated into a single clean commit. Cumulative state: ingestion pipeline hardened per the 2026-07-20 Product Board audit (42/100 to 80/100); enrichment research layer hardened against five field-observed failures (website identity verification, contact plausibility filtering, team-page discovery, research speed, two prompt bugs); validated on a real 15-firm run plus a deep single-firm methodology; UI shows assessment date/time; LICENSE, SECURITY.md, CODE_OF_CONDUCT.md, and CI precision-audit added; dead Neo4j dependency fully removed. | 🟢 done |
