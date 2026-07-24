import asyncio
import json
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── Terminal logging ──
_EVENTS_LOG = ROOT / "events.log"
def _log_event(msg: str):
    """Write once to OS stdout FD (most reliable) + log file."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}\n"
    try:
        os.write(1, line.encode("utf-8", errors="replace"))
    except Exception:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        with open(_EVENTS_LOG, "a") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from engine.llm.provider import RateLimitError, LLM_MODEL

from api.logging_config import setup_logging, get_logger
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = get_logger(__name__)
logger.info("PayBrix Lead Engine starting up...")
_log_event("SERVER START")

from db.init_db import init_db
from db.dal import (
    get_db_connection,
    get_companies,
    get_company_detail,
    get_unenriched_companies,
    get_top_opportunities,
    count_companies,
    upsert_enrichment,
    upsert_email,
    clear_all_enrichments,
    update_enrichment_contacts,
    record_rejection,
)
from ingestion.runner import run_ingestion
from engine.assessor import assess_company, generate_email, refresh_contacts
from engine.governor.enforcement import evaluate_storage, get_enforcement_mode
from engine.activity import registry as run_registry
from api.security import cors_origins, get_api_key, require_api_key, require_confirmation

_LLM_MODEL = LLM_MODEL
from api.schemas import (
    CompanyListResponse,
    CompanyDetailResponse,
    EnrichmentResponse,
    BulkEnrichmentResponse,
    EmailResponse,
    IngestionResponse,
    StatsResponse,
    CountiesResponse,
    ErrorResponse,
    validate_sort_column,
    validate_sort_direction,
)

init_db()

app = FastAPI(title="PayBrix Lead Engine API", version="1.0.0")

# Was allow_origins=["*"] with allow_credentials=True, while the CORS_ORIGINS
# documented in .env.example was read by nothing. The dashboard is served
# same-origin by this app and never needed a CORS grant — the wildcard only
# handed one to every other page in the operator's browser.
_CORS_ORIGINS = cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=bool(_CORS_ORIGINS),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)
if _CORS_ORIGINS:
    logger.info(f"[Security] CORS enabled for: {_CORS_ORIGINS}")
if get_api_key() is None:
    logger.warning(
        "[Security] API_KEY is not set — mutating endpoints are unauthenticated. "
        "Fine for a localhost-only install; set API_KEY before exposing this to a network."
    )
else:
    logger.info("[Security] API_KEY is set — mutating endpoints require the X-API-Key header.")


@app.middleware("http")
async def log_all_requests(request, call_next):
    path = request.url.path
    if path.startswith("/static/"):
        return await call_next(request)
    method = request.method
    _log_event(f"REQ {method} {path}")
    try:
        response = await call_next(request)
        _log_event(f"RES {method} {path} -> {response.status_code}")
        return response
    except Exception as e:
        _log_event(f"RES {method} {path} -> 500 {str(e)[:200]}")
        raise


FRONTEND_DIR = ROOT / "frontend"

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# FileResponse sets its own Last-Modified/ETag regardless of the HTML's
# <meta> no-cache tags (browsers only honor meta cache tags inconsistently,
# and never for the conditional-GET/304 machinery those real headers
# enable) — a real explicit no-store header is what actually stops a
# browser from serving a stale cached copy of the app shell after a deploy.
_NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

# Ceiling on one client-supplied log line. events.log is the primary
# operational record; an uncapped append is a disk-fill primitive.
_MAX_CLIENT_LOG_CHARS = 500


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.htm", headers=_NO_CACHE_HEADERS)


@app.get("/lead/{company_id}")
def lead_page(company_id: str):
    return FileResponse(FRONTEND_DIR / "lead.htm", headers=_NO_CACHE_HEADERS)


SORT_COLUMNS_VALID = {"score", "name", "county", "status", "incorporated", "size"}


def _compute_stats():
    with get_db_connection() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                                                      AS total,
                COALESCE(SUM(CASE WHEN e.qualification_score IS NOT NULL THEN 1 ELSE 0 END), 0) AS assessed,
                COALESCE(SUM(CASE WHEN e.qualification_score >= 70          THEN 1 ELSE 0 END), 0) AS strong,
                COALESCE(SUM(CASE WHEN e.qualification_score >= 40 AND e.qualification_score < 70  THEN 1 ELSE 0 END), 0) AS moderate,
                COALESCE(SUM(CASE WHEN e.qualification_score >= 1  AND e.qualification_score < 40  THEN 1 ELSE 0 END), 0) AS lower,
                ROUND(AVG(CASE WHEN e.qualification_score IS NOT NULL THEN CAST(e.qualification_score AS REAL) ELSE NULL END), 1) AS avg_score
            FROM companies c
            LEFT JOIN enrichment e ON e.company_id = c.id
        """).fetchone()
        outreach = conn.execute("SELECT COUNT(*) AS n FROM outreach_emails WHERE status = 'draft'").fetchone()["n"]

    total  = row["total"]
    assessed = row["assessed"]
    strong   = row["strong"]
    moderate = row["moderate"]
    lower    = row["lower"]

    qualified   = strong + moderate + lower
    needs_review = assessed - qualified
    awaiting     = total - assessed
    avg_fit      = row["avg_score"] or 0

    return {
        "total_companies": total,
        "assessed": assessed,
        "awaiting": awaiting,
        "strong": strong,
        "moderate": moderate,
        "lower": lower,
        "needs_review": needs_review,
        "qualified": qualified,
        "average_fit": avg_fit,
        "outreach_ready": outreach,
    }


@app.get("/api/stats", response_model=StatsResponse)
def api_stats():
    stats = _compute_stats()
    stats["model"] = _LLM_MODEL
    return stats


@app.get("/api/counties", response_model=CountiesResponse)
def api_counties():
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT county FROM companies WHERE county IS NOT NULL AND county != '' ORDER BY county"
        ).fetchall()
    return {"counties": [r["county"] for r in rows]}


@app.get("/api/leads", response_model=CompanyListResponse)
def api_leads(
    county: Optional[str] = Query(None),
    min_score: Optional[int] = Query(None, ge=0, le=100),
    max_score: Optional[int] = Query(None, ge=0, le=100),
    status: Optional[str] = Query(None),
    sort_by: str = Query("score"),
    sort_dir: str = Query("desc"),
    limit: int = Query(100, ge=1, le=99999),
    offset: int = Query(0, ge=0),
):
    try:
        sort_by = validate_sort_column(sort_by)
        sort_dir = validate_sort_direction(sort_dir)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    companies = get_companies(
        county=county,
        min_score=min_score,
        max_score=max_score,
        status=status,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )
    total_count = count_companies(
        county=county,
        min_score=min_score,
        max_score=max_score,
        status=status,
    )
    for c in companies:
        c.pop("billing_pain_points", None)
    return {"leads": companies, "total": total_count}


@app.get("/api/leads/{company_id}", response_model=CompanyDetailResponse)
def api_lead_detail(company_id: str):
    detail = get_company_detail(company_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Company not found")
    return detail


@app.post("/api/ingest", response_model=IngestionResponse, dependencies=[Depends(require_api_key)])
def api_ingest():
    logger.info("Starting ingestion process")
    try:
        result = run_ingestion()
        logger.info(f"Ingestion completed: {result.get('records_new', 0)} new records")
        return {
            "status": "ok",
            "run_id": result.get("run_id"),
            "records_found": result.get("records_found", 0),
            "records_new": result.get("records_new", 0),
            "errors": result.get("errors", []),
        }
    except Exception as e:
        logger.error(f"Ingestion failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(e)}")


@app.post("/api/enrich-all", response_model=BulkEnrichmentResponse, dependencies=[Depends(require_api_key)])
def api_enrich_all(limit: Optional[int] = Query(None, ge=0)):
    logger.info(f"[API] === BULK ENRICHMENT START === limit: {limit}")
    companies = get_unenriched_companies()
    if limit and limit > 0:
        companies = companies[:limit]
    total = len(companies)

    # Assessments are independent (own research, own LLM call, own DB write)
    # — running them serially made a 30-firm batch take 30 x full assessment
    # time. Conservative default of 3 workers respects LLM rate limits;
    # tune via ASSESS_CONCURRENCY.
    concurrency = max(1, int(os.getenv("ASSESS_CONCURRENCY", "3")))
    logger.info(f"[API] Processing {total} companies (concurrency={concurrency})")

    abort = threading.Event()

    def _assess_one(numbered):
        idx, c = numbered
        company_name = c.get("legal_name", "unknown")
        if abort.is_set():
            return ("skipped", c, None)
        logger.info(f"[API] [{idx}/{total}] Processing: {company_name}")
        try:
            enrichment = assess_company(c)
            decision = evaluate_storage(enrichment)
            if not decision.store:
                logger.warning(f"[API] [{idx}/{total}] REJECTED: {company_name} - {decision.reason}")
                _record_rejection_safely(c["id"], enrichment, decision.reason)
                return ("rejected", c, RuntimeError(decision.reason))
            with get_db_connection() as conn:
                upsert_enrichment(conn, enrichment)
            logger.info(f"[API] [{idx}/{total}] SUCCESS: {company_name}")
            return ("ok", c, None)
        except RateLimitError as e:
            # Stop handing out new work; in-flight assessments finish.
            abort.set()
            logger.error(f"[API] [{idx}/{total}] RATE LIMIT: {company_name} - {str(e)}")
            return ("rate_limited", c, e)
        except Exception as e:
            logger.error(f"[API] [{idx}/{total}] FAILED: {company_name} - {str(e)}")
            return ("failed", c, e)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        outcomes = list(pool.map(_assess_one, enumerate(companies, 1)))

    count = sum(1 for status_, _, _ in outcomes if status_ == "ok")
    rejected = sum(1 for status_, _, _ in outcomes if status_ == "rejected")
    rate_limited = any(status_ == "rate_limited" for status_, _, _ in outcomes)
    # Guard rejections are reported separately from errors: nothing broke, the
    # assessment was produced and deliberately not kept.
    errors = [
        {"company_id": c.get("id"), "company_name": c.get("legal_name"), "error": str(err)}
        for status_, c, err in outcomes if status_ in ("failed", "rejected")
    ][:10]

    if rate_limited:
        logger.error(f"[API] === BULK ENRICHMENT RATE-LIMITED === {count}/{total} completed before abort")
        raise HTTPException(status_code=429, detail="API rate limit reached. Please try again later.")

    logger.info(
        f"[API] === BULK ENRICHMENT COMPLETE === {count}/{total} successful, "
        f"{rejected} rejected by guards (mode={get_enforcement_mode()})"
    )
    return {
        "status": "ok",
        "attempted": total,
        "enriched": count,
        "rejected": rejected,
        "failed": total - count - rejected,
        "errors": errors,
    }


def _record_rejection_safely(company_id: str, enrichment: dict, reason: str) -> None:
    """Persist a guard rejection without letting a storage failure change the
    outcome. The rejection already happened; failing to write the audit row is
    a logging problem, not a reclassification of the assessment — letting it
    raise turned a `rejected` outcome into a `failed` one, which reads as a
    crash rather than a deliberate quality decision.
    """
    try:
        with get_db_connection() as conn:
            record_rejection(
                conn, company_id, reason=reason,
                guard_failures=enrichment.get("guard_failures"),
                guard_score=enrichment.get("guard_score"),
                llm_model=enrichment.get("llm_model"),
            )
    except Exception as exc:
        logger.error(f"[API] Could not record rejection for {company_id}: {exc}", exc_info=True)


def _run_assess_job(run_id: str, company: dict):
    company_id = company.get("id")
    company_name = company.get("legal_name", "Unknown")

    def on_event(step, label, status, metadata=None):
        run_registry.emit(run_id, step, label, status, metadata)

    try:
        _log_event(f"ASSESS START company={company_id} run_id={run_id}")
        enrichment = assess_company(company, on_event=on_event)
        decision = evaluate_storage(enrichment)
        if not decision.store:
            # Not an error — the assessment ran cleanly and was deliberately
            # discarded. Surfaced as a failed run so the operator sees the
            # reason rather than a silent no-op with no new data on screen.
            logger.warning(f"[Run {run_id}] Assessment rejected for {company_name}: {decision.reason}")
            _record_rejection_safely(company_id, enrichment, decision.reason)
            run_registry.finish_run(run_id, "failed", error=decision.reason)
            _log_event(f"ASSESS REJECTED company={company_id} run_id={run_id} reason={decision.reason}")
            return
        with get_db_connection() as conn:
            upsert_enrichment(conn, enrichment)
        run_registry.finish_run(run_id, "complete")
        _log_event(f"ASSESS SUCCESS company={company_id} run_id={run_id}")
    except RateLimitError as e:
        logger.error(f"[Run {run_id}] Rate limit assessing {company_name}: {e}")
        run_registry.finish_run(run_id, "failed", error="API rate limit reached")
        _log_event(f"ASSESS RATE_LIMIT company={company_id} run_id={run_id}")
    except Exception as e:
        logger.error(f"[Run {run_id}] Assessment job failed for {company_name}: {e}", exc_info=True)
        run_registry.finish_run(run_id, "failed", error=str(e)[:500])
        _log_event(f"ASSESS ERROR company={company_id} run_id={run_id} error={str(e)[:200]}")


def _run_outreach_job(run_id: str, company_id: str):
    def on_event(step, label, status, metadata=None):
        run_registry.emit(run_id, step, label, status, metadata)

    try:
        company_detail = get_company_detail(company_id)
        email_result = generate_email(company_detail, on_event=on_event)
        contact_id = None
        if company_detail.get("contacts"):
            contact_id = company_detail["contacts"][0].get("id")
        email = {
            "company_id": company_id,
            "subject": email_result.get("subject", ""),
            "body": email_result.get("body", ""),
            "llm_model": _LLM_MODEL,
            "contact_id": contact_id,
        }
        with get_db_connection() as conn:
            upsert_email(conn, email)
        run_registry.finish_run(run_id, "complete")
        logger.info(f"[Run {run_id}] Outreach job complete for company {company_id}")
    except RateLimitError as e:
        logger.error(f"[Run {run_id}] Rate limit generating email for {company_id}: {e}")
        run_registry.finish_run(run_id, "failed", error="API rate limit reached")
    except Exception as e:
        logger.error(f"[Run {run_id}] Outreach job failed for {company_id}: {e}", exc_info=True)
        run_registry.finish_run(run_id, "failed", error=str(e)[:500])


def _run_contacts_refresh_job(run_id: str, company_id: str, company: dict):
    def on_event(step, label, status, metadata=None):
        run_registry.emit(run_id, step, label, status, metadata)

    try:
        contacts = refresh_contacts(company, on_event=on_event)
        with get_db_connection() as conn:
            ok = update_enrichment_contacts(conn, company_id, contacts)
        run_registry.finish_run(run_id, "complete" if ok else "failed",
                                 error=None if ok else "No existing enrichment to update")
        logger.info(f"[Run {run_id}] Contacts refresh {'complete' if ok else 'failed'} for company {company_id}")
    except Exception as e:
        logger.error(f"[Run {run_id}] Contacts refresh job failed for {company_id}: {e}", exc_info=True)
        run_registry.finish_run(run_id, "failed", error=str(e)[:500])


@app.post("/api/enrich/{company_id}", response_model=EnrichmentResponse, dependencies=[Depends(require_api_key)])
def api_enrich_single(company_id: str):
    _log_event(f"ASSESS CLICKED company={company_id}")
    with get_db_connection() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not company:
        _log_event(f"ASSESS FAIL company={company_id} reason=not_found")
        raise HTTPException(status_code=404, detail="Company not found")
    run_id, is_new = run_registry.get_or_start_run(company_id, "assess")
    if not is_new:
        return {"status": "running", "run_id": run_id}
    _log_event(f"ASSESS QUEUED company={company_id} run_id={run_id}")
    threading.Thread(target=_run_assess_job, args=(run_id, dict(company)), daemon=True).start()
    return {"status": "running", "run_id": run_id}


@app.post("/api/companies/{company_id}/contacts/refresh", dependencies=[Depends(require_api_key)])
def api_contacts_refresh(company_id: str):
    with get_db_connection() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    detail = get_company_detail(company_id)
    if not detail or not detail.get("enrichment"):
        raise HTTPException(status_code=400, detail="Company must be assessed before contacts can be refreshed")
    # An in-flight assess/reassess for the same company will DELETE+INSERT
    # a brand-new enrichment row when it finishes — racing a contacts-only
    # UPDATE against that would either lose the refreshed contacts (assess
    # wins) or silently update a row that's about to be deleted (refresh
    # wins), so refuse to start until the assess run clears.
    if run_registry.get_active_run(company_id, "assess"):
        raise HTTPException(status_code=409, detail="An assessment is already running for this company — wait for it to finish before refreshing contacts")
    run_id, is_new = run_registry.get_or_start_run(company_id, "contacts_refresh")
    if not is_new:
        return {"status": "running", "run_id": run_id}
    threading.Thread(target=_run_contacts_refresh_job, args=(run_id, company_id, dict(company)), daemon=True).start()
    return {"status": "running", "run_id": run_id}


@app.get("/api/runs/active")
def api_runs_active():
    return {"runs": run_registry.list_active()}


@app.get("/api/runs/{run_id}")
def api_run_status(run_id: str):
    status = run_registry.get_run_status(run_id)
    if not status:
        raise HTTPException(status_code=404, detail="Run not found")
    return status


@app.get("/api/runs/{run_id}/events")
async def api_run_events(run_id: str):
    if not run_registry.get_run_status(run_id):
        raise HTTPException(status_code=404, detail="Run not found")

    async def gen():
        since = 0
        while True:
            events, status, since = run_registry.get_new_events(run_id, since)
            for e in events:
                yield f"data: {json.dumps(e)}\n\n"
            if status in ("complete", "failed"):
                yield f"data: {json.dumps({'run_id': run_id, 'step': '_run', 'status': status})}\n\n"
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Deliberately NOT key-gated: the dashboard calls this on every interaction
# and has nowhere to keep a credential, so gating it would break diagnostics
# in precisely the networked deployment where they matter. The finding here
# was an unbounded, unsanitised append — addressed below. Rate limiting is
# the remaining follow-up.
@app.post("/api/log")
def api_log_event(data: dict):
    # Was an unbounded, unsanitised append to events.log: no length cap, and
    # newlines in `msg` forged entries that read like genuine server events in
    # the file that is this app's primary operational record.
    msg = (data.get("msg") or data.get("event") or "?").strip()
    msg = re.sub(r"[\r\n]+", " ", msg)[:_MAX_CLIENT_LOG_CHARS]
    if msg:
        _log_event(f"FRONTEND {msg}")
    return {"ok": True}


@app.post("/api/disenrich-all", dependencies=[Depends(require_api_key)])
def api_disenrich_all(payload: dict = Body(default=None)):
    """Permanently deletes every enrichment and every draft email.

    Requires an explicit typed confirmation in the body. This was reachable
    as a bare unauthenticated POST with no payload, which meant any page in
    the operator's browser could destroy the whole assessment set.
    """
    require_confirmation(payload)
    count = clear_all_enrichments()
    logger.warning(f"[API] DISENRICH-ALL confirmed — deleted {count} enrichment(s) and all draft emails")
    _log_event(f"DISENRICH ALL count={count}")
    return {"status": "ok", "disenriched": count}


@app.post("/api/email/{company_id}", response_model=EmailResponse, dependencies=[Depends(require_api_key)])
def api_email(company_id: str):
    logger.info(f"[API] Email generation request for company {company_id}")
    company_detail = get_company_detail(company_id)
    if not company_detail:
        logger.warning(f"[API] Company not found for email generation: {company_id}")
        raise HTTPException(status_code=404, detail="Company not found")

    # Pre-flight check: skip email if insufficient research confidence for personalization
    enrichment = company_detail.get("enrichment")
    if not enrichment:
        return EmailResponse(status="skipped", subject="", detail="Company must be assessed before email generation")
    
    # Extract from narrative_assessment where the fields actually live
    narrative = enrichment.get("narrative_assessment") or {}
    confidence = narrative.get("research_confidence", 0)
    has_website = narrative.get("digital_presence", {}).get("has_website", False)
    contacts = narrative.get("contacts", [])
    
    if confidence < 50 and not has_website and not contacts:
        return EmailResponse(
            status="skipped",
            subject="",
            detail=f"Insufficient research confidence ({confidence}%) for personalization. No website or contacts found."
        )

    run_id, is_new = run_registry.get_or_start_run(company_id, "outreach")
    if not is_new:
        return {"status": "running", "run_id": run_id, "subject": ""}
    threading.Thread(target=_run_outreach_job, args=(run_id, company_id), daemon=True).start()
    logger.info(f"[API] Outreach run queued for company {company_id} run_id={run_id}")
    return {"status": "running", "run_id": run_id, "subject": ""}


@app.get("/api/guard-stats")
def api_guard_stats():
    """
    Return aggregate guard statistics across all assessments.
    Sourced directly from SQLite (enrichment.guard_passed/guard_score) —
    guard verdicts are written there at assessment time, so this used to
    detour through a write-only Neo4j audit trail that nothing ever read.
    """
    try:
        with get_db_connection() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN guard_passed = 1 THEN 1 ELSE 0 END) AS passed,
                    SUM(CASE WHEN guard_passed = 0 THEN 1 ELSE 0 END) AS failed,
                    ROUND(AVG(guard_score), 1) AS avg_score
                FROM enrichment
                WHERE guard_passed IS NOT NULL
            """).fetchone()
        total = row["total"] or 0
        passed = row["passed"] or 0
        return {
            "total_assessments": total,
            "passed": passed,
            "failed": row["failed"] or 0,
            "pass_rate": round(passed / total * 100, 1) if total > 0 else 0.0,
            "avg_guard_score": row["avg_score"],
        }
    except Exception as e:
        logger.error(f"[API] Guard stats failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Guard stats unavailable: {str(e)}")

