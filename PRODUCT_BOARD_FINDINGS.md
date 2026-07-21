# Product Board Audit — Ingestion Pipeline (2026-07-20)

> Handoff brief for a fresh session. This audit was run by the `product-board`
> skill (an 8-persona senior engineering review board, built in the sibling
> `engineers-board` project) against this repo's ingestion pipeline. Every
> finding below is grounded in an actual file read — no speculation. Nothing
> has been fixed yet; this is the starting brief for that work.

**Scope audited:** `ingestion/cbi_fetcher.py`, `ingestion/cbi_parser.py`,
`ingestion/cro_resolver.py`, `ingestion/runner.py` (+ `db/dal.py`,
`db/schema.sql` for dedup verification).

**Verdict:** `STRUCTURAL PROBLEMS` — **42/100** ship readiness. 1 CRITICAL,
4 HIGH, 4 MEDIUM, 3 LOW. Not safe as the source of truth for outbound sales
until the CRITICAL is fixed and covered by a test.

**Repo state at audit time:** `main` clean, in sync with `origin/main`,
nothing pending.

---

## Suggested execution order (per the board's CHAIR)

1. **Write characterization tests first** (WARDEN finding — zero test
   coverage exists anywhere in this scope today). Include one test that
   currently **fails** and pins down Fix #1's bug.
2. **Fix #1 (CRITICAL)** with that new test now proving it.
3. Fix #2, #3, #4 (HIGH) — each has its own validation test below.
4. Only after tests exist, consider the ARCHITECT module-split (MEDIUM,
   optional, lower priority) — refactoring `cro_resolver.py` before it has
   tests risks losing behavior WARDEN needs to characterize first.

---

## FIX #1 — CRITICAL — data integrity

**File:** `ingestion/cro_resolver.py`
**Location:** lines 248–257

**Problem:** Both branches of the fuzzy-match confidence check perform the
identical assignment — differing only in a label string:

```python
if score >= FUZZY_THRESHOLD:
    matched_row = cro_name_to_row[matched_name]
    confidence = score / 100.0
    match_method = f"fuzzy_name(score={score})"
else:
    matched_row = cro_name_to_row[matched_name]   # <-- same as above
    confidence = score / 100.0
    match_method = f"fuzzy_name_low_confidence(score={score})"
```

A match scoring as low as 60 (the `score_cutoff`) gets its `cro_number`,
`cro_status`, `incorporation_date`, and `company_type` merged into the firm
record exactly like a 95-score match. The only thing distinguishing it is the
`needs_review` boolean — any downstream code that reads these fields without
checking that flag silently attaches one company's registration data to a
different company. This directly violates the product's own compliance golden
rule (never present unverified data as fact).

**Fix:**
```python
if score >= FUZZY_THRESHOLD:
    matched_row = cro_name_to_row[matched_name]
    confidence = score / 100.0
    match_method = f"fuzzy_name(score={score})"
else:
    matched_row = None
    confidence = score / 100.0
    match_method = f"fuzzy_name_low_confidence(score={score})"
```

**Validation test:** CBI firm whose best fuzzy match scores 70 (between
`score_cutoff=60` and `FUZZY_THRESHOLD=85`) → assert `enriched["cro_number"]
is None` and `enriched["needs_review"] is True`.

---

## FIX #2 — HIGH — silent degraded runs report as clean

**Files:** `ingestion/cro_resolver.py` (lines 108–112), `ingestion/runner.py`
(lines 114–126)

**Problem:** When the CRO snapshot can't be downloaded, `_download_cro_snapshot`
prints a warning and returns; `resolve_against_cro` proceeds normally
(flags every firm `needs_review=True`) and returns without raising.
`runner.py` only appends to `errors` on an actual exception — so a run where
CRO enrichment completely failed reports **zero errors** to `log_run()`. The
audit trail lies about run health.

**Fix:** Change `_load_cro_snapshot()` to return `(companies, degraded: bool)`;
thread that through `resolve_against_cro`; in `runner.py`:
```python
enriched_firms, cro_degraded = resolve_against_cro(firms)
if cro_degraded:
    errors.append("CRO snapshot unavailable — all firms flagged needs_review")
```

**Validation test:** Delete/corrupt the cache file, run the pipeline, assert
`result["errors"]` is non-empty and the `ingestion_runs` row for that run has
a non-empty `errors` JSON array.

---

## FIX #3 — HIGH — duplicate-insert risk

**Files:** `db/dal.py` (lines 93–99), `db/schema.sql` (line 8–9, confirmed:
`cro_number TEXT UNIQUE` but `legal_name TEXT NOT NULL` has **no** unique
constraint)

**Problem:** When a firm has no `cro_number` match, dedup falls back to an
exact `legal_name` string match with no normalization and no DB backstop. A
CBI PDF re-extraction with different whitespace, or a future CSV publication
with different capitalization, will insert a duplicate row instead of
updating — silently inflating `records_new` and duplicating the same company
in the sales pipeline.

**Fix:** Reuse the same name-folding `cro_resolver.py` already has
(`_norm_name`) in the fallback lookup — either normalize before the `SELECT`,
or (cleaner) add a `legal_name_normalized` column, populated at insert time
and indexed, and match on that.

**Validation test:** Insert a firm with `legal_name="Acme  Ltd"`, then call
`upsert_company` again with `legal_name="ACME LTD"` and no `cro_number` —
assert the second call returns `is_new=False` and the `companies` table still
has exactly 1 row for it.

---

## FIX #4 — HIGH — no retry on external fetches

**Files:** `ingestion/cbi_fetcher.py` (lines 152–188), `ingestion/cro_resolver.py`
(lines 61–107)

**Problem:** Both external fetch paths (CBI register download, CRO snapshot
download) make a single attempt each. A transient network failure aborts or
degrades the entire scheduled run with no automatic recovery.

**Fix:** Add a small shared retry helper (3 attempts, exponential backoff)
and wrap both fetch entry points' `client.get(...)` calls.

**Validation test:** Mock the client to raise `httpx.ConnectTimeout` twice
then succeed — assert `fetch_cbi_register()` still returns a valid path and
the mock was called 3 times.

---

## Remaining findings (MEDIUM / LOW — fix opportunistically, not blocking)

| # | Severity | Owner | File:Line | Finding |
|---|---|---|---|---|
| M1 | MEDIUM | QUANT | `cro_resolver.py:132-134,200-218` | Full CRO snapshot (hundreds of thousands of rows) loaded entirely into memory with duplicate indexes, no batching |
| M2 | MEDIUM | QUANT | `cro_resolver.py:38-44,115-120` | `_is_cache_fresh()` doesn't validate the cached CSV has actual rows — an empty-but-fresh cache silently causes 24h of zero matches |
| M3 | MEDIUM | ARCHITECT | `cro_resolver.py` (whole file) | Mixes download/cache/match responsibilities in one 302-line module — split after tests exist |
| M4 | MEDIUM | OPS | all 4 files | `print()`-only logging, no structured/leveled output; `ingestion_runs` DB row is the only durable signal |
| L1 | LOW | ARCHITECT | `runner.py:133-144` | Persistence loop inlined in `run_ingestion()` instead of a testable `persist_firms()` function |
| L2 | LOW | SENTINEL | `cbi_fetcher.py`, `cro_resolver.py` | No response-size cap on external HTTP fetches (low risk — known .ie gov/open-data sources) |
| L3 | LOW | SENTINEL | `cbi_parser.py:184-187,210-243` | Regex-based PDF parsing has no line-length bound before matching (precautionary, no exploit found) |

**HIGH — also present, separate from correctness:** QUANT flagged
`cro_resolver.py:239-247` — fuzzy match scans the full CRO snapshot per CBI
firm when first-token bucketing doesn't reduce the candidate pool (common
first tokens like "THE"). This is a present-day performance risk at current
data volume, not a future one, but is not a data-correctness bug like Fix #1
— track separately once #1–#4 land.

---

## What's genuinely good (don't regress this)

- Stage-by-stage error isolation in `runner.py` — each step (fetch/parse/
  resolve/persist) catches its own failures and keeps going rather than
  crashing the whole run.
- `log_run()` (`db/dal.py:430-455`) persisting `run_id`/`records_found`/
  `records_new`/`errors` per run is the right foundation for an audit trail.
- `cbi_parser.py`'s format isolation is real — CBI format changes only
  require editing that one file, nothing downstream touches format-specific
  fields.
- Secrets loaded via `.env`/`python-dotenv`, nothing hardcoded.

---

## Re-running the audit

This audit was produced by the `product-board` skill in
`C:\Users\x\Desktop\engineers-board\.claude\skills\product-board\`. Once
fixes land, re-run it (same scope or `[FULL]`) — the board is designed for
iterative use and the score should rise each pass.
