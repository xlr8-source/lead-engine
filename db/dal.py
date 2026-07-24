"""
db/dal.py -- Data Access Layer (raw SQL, sqlite3)

Intentionally uses raw SQL so the Postgres migration story is demonstrable:
every query here translates directly to Postgres with only type-cast changes
(documented in db/migrate_to_postgres.sql).

No ORM. No magic. The schema is explicit and auditable.

Public API:
    get_db_connection()          → context-managed sqlite3.Connection
    upsert_company(conn, firm, ingested_at) → bool (True if new)
    upsert_contact(conn, contact)           → None
    upsert_enrichment(conn, enrichment)     → None
    upsert_email(conn, email)               → None
    log_run(run_id, ...)                    → None
    get_companies(filters)                  → list[dict]
    get_company_detail(company_id)          → dict | None
    get_unenriched_companies()              → list[dict]
"""
import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

ROOT = Path(__file__).parent.parent

# Resolve DB path from environment (set by init_db or tests)
_DB_PATH_ENV = os.getenv("DB_PATH", "leads_vault.db")
_DB_PATH = Path(_DB_PATH_ENV) if Path(_DB_PATH_ENV).is_absolute() else ROOT / _DB_PATH_ENV


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    """Return rows as dicts (column_name → value)."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


@contextmanager
def get_db_connection(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Context-managed SQLite connection.
    Enables WAL mode for better concurrent read performance.
    Rows returned as dicts via row_factory.
    """
    path = db_path or _DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = _row_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

def upsert_company(conn: sqlite3.Connection, firm: dict, ingested_at: str) -> bool:
    """
    Insert or update a company record.
    Returns True if this is a new record, False if updated.

    Deduplication key, in order: cro_number (if present), then normalized
    legal_name (same folding as ingestion.cro_resolver — case, whitespace and
    legal suffixes collapsed), then raw legal_name as a last resort for rows
    whose name normalizes to empty (e.g. bare "Limited").
    """
    # Local import: keeps db importable without pulling ingestion deps at module load
    from ingestion.cro_resolver import normalize_legal_name

    # Check for existing record
    existing_id = None

    if firm.get("cro_number"):
        row = conn.execute(
            "SELECT id FROM companies WHERE cro_number = ?",
            (firm["cro_number"],),
        ).fetchone()
        if row:
            existing_id = row["id"]

    legal_name_normalized = normalize_legal_name(firm.get("legal_name")) or None

    if existing_id is None and legal_name_normalized:
        row = conn.execute(
            "SELECT id FROM companies WHERE legal_name_normalized = ?",
            (legal_name_normalized,),
        ).fetchone()
        if row:
            existing_id = row["id"]

    if existing_id is None:
        row = conn.execute(
            "SELECT id FROM companies WHERE legal_name = ?",
            (firm.get("legal_name", ""),),
        ).fetchone()
        if row:
            existing_id = row["id"]

    raw_payload = json.dumps({
        k: v for k, v in firm.items()
        if k not in ("raw_row",) and isinstance(v, (str, int, float, bool, type(None)))
    })

    if existing_id:
        conn.execute(
            """
            UPDATE companies SET
                cbi_reference       = COALESCE(?, cbi_reference),
                cro_number          = COALESCE(?, cro_number),
                trading_name        = COALESCE(?, trading_name),
                cro_status          = COALESCE(?, cro_status),
                incorporation_date  = COALESCE(?, incorporation_date),
                registered_address  = COALESCE(?, registered_address),
                county              = COALESCE(?, county),
                eircode             = COALESCE(?, eircode),
                company_type        = COALESCE(?, company_type),
                last_annual_return  = COALESCE(?, last_annual_return),
                last_accounts_date  = COALESCE(?, last_accounts_date),
                principal_object    = COALESCE(?, principal_object),
                raw_payload         = ?
            WHERE id = ?
            """,
            (
                firm.get("cbi_reference"),
                firm.get("cro_number"),
                firm.get("trading_name"),
                firm.get("cro_status"),
                firm.get("incorporation_date"),
                firm.get("registered_address"),
                firm.get("county"),
                firm.get("eircode"),
                firm.get("company_type"),
                firm.get("last_annual_return"),
                firm.get("last_accounts_date"),
                firm.get("principal_object"),
                raw_payload,
                existing_id,
            ),
        )
        return False
    else:
        company_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO companies (
                id, cbi_reference, cro_number, legal_name, legal_name_normalized,
                trading_name,
                cro_status, incorporation_date, registered_address, county,
                eircode, sector_tag, company_type, last_annual_return,
                last_accounts_date, principal_object, source, ingested_at, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_id,
                firm.get("cbi_reference"),
                firm.get("cro_number"),
                firm.get("legal_name", ""),
                legal_name_normalized,
                firm.get("trading_name"),
                firm.get("cro_status"),
                firm.get("incorporation_date"),
                firm.get("registered_address"),
                firm.get("county"),
                firm.get("eircode"),
                "insurance_intermediary",
                firm.get("company_type"),
                firm.get("last_annual_return"),
                firm.get("last_accounts_date"),
                firm.get("principal_object"),
                "cbi_register",
                ingested_at,
                raw_payload,
            ),
        )
        return True


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def upsert_contact(conn: sqlite3.Connection, contact: dict) -> str:
    """
    Insert or update a contact record.
    Returns the contact id.
    """
    existing = conn.execute(
        "SELECT id FROM contacts WHERE company_id = ? AND full_name = ?",
        (contact["company_id"], contact.get("full_name")),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE contacts SET
                role_title       = COALESCE(?, role_title),
                email            = COALESCE(?, email),
                phone            = COALESCE(?, phone),
                confidence_score = COALESCE(?, confidence_score)
            WHERE id = ?
            """,
            (
                contact.get("role_title"),
                contact.get("email"),
                contact.get("phone"),
                contact.get("confidence_score"),
                existing["id"],
            ),
        )
        return existing["id"]

    contact_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO contacts (id, company_id, full_name, role_title, email, phone,
                              confidence_score, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact_id,
            contact["company_id"],
            contact.get("full_name"),
            contact.get("role_title"),
            contact.get("email"),
            contact.get("phone"),
            contact.get("confidence_score"),
            contact.get("source", "cbi_register"),
            _now_iso(),
        ),
    )
    return contact_id


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

_assessment_columns_cache = set()
_narrative_columns_cache = set()
_guard_columns_cache = set()
_columns_lock = None


def _get_columns_lock():
    """Lazy initialization of lock for thread safety."""
    global _columns_lock
    if _columns_lock is None:
        import threading
        _columns_lock = threading.Lock()
    return _columns_lock


def _ensure_assessment_column(conn: sqlite3.Connection) -> None:
    """Thread-safe column existence check and addition."""
    with _get_columns_lock():
        if "assessment_breakdown" in _assessment_columns_cache:
            return
        
        # Check if column actually exists in schema
        cursor = conn.execute("PRAGMA table_info(enrichment)")
        existing_columns = {row["name"] for row in cursor.fetchall()}
        
        if "assessment_breakdown" in existing_columns:
            _assessment_columns_cache.add("assessment_breakdown")
            return
        
        try:
            conn.execute("ALTER TABLE enrichment ADD COLUMN assessment_breakdown TEXT")
            _assessment_columns_cache.add("assessment_breakdown")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def _ensure_narrative_columns(conn: sqlite3.Connection) -> None:
    """Thread-safe narrative columns existence check and addition."""
    with _get_columns_lock():
        columns_to_ensure = ["narrative_assessment", "signal_strength"]
        if all(col in _narrative_columns_cache for col in columns_to_ensure):
            return
        
        # Check if columns actually exist in schema
        cursor = conn.execute("PRAGMA table_info(enrichment)")
        existing_columns = {row["name"] for row in cursor.fetchall()}
        
        for col in columns_to_ensure:
            if col in existing_columns:
                _narrative_columns_cache.add(col)
                continue
            
            try:
                conn.execute(f"ALTER TABLE enrichment ADD COLUMN {col} TEXT")
                _narrative_columns_cache.add(col)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def _ensure_guard_columns(conn: sqlite3.Connection) -> None:
    """Thread-safe guard column existence check and addition."""
    with _get_columns_lock():
        guard_cols = ["guard_passed", "guard_score", "guard_failures"]
        if all(col in _guard_columns_cache for col in guard_cols):
            return

        cursor = conn.execute("PRAGMA table_info(enrichment)")
        existing_columns = {row["name"] for row in cursor.fetchall()}

        for col in guard_cols:
            if col in existing_columns:
                _guard_columns_cache.add(col)
                continue
            col_type = "INTEGER" if col == "guard_passed" else ("REAL" if col == "guard_score" else "TEXT")
            try:
                conn.execute(f"ALTER TABLE enrichment ADD COLUMN {col} {col_type}")
                _guard_columns_cache.add(col)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def upsert_enrichment(conn: sqlite3.Connection, enrichment: dict) -> str:
    """
    Insert or replace enrichment for a company.
    Returns the enrichment id.
    """
    _ensure_assessment_column(conn)
    _ensure_narrative_columns(conn)
    _ensure_guard_columns(conn)

    conn.execute(
        "DELETE FROM enrichment WHERE company_id = ?",
        (enrichment["company_id"],),
    )

    # Three distinct states, and the difference matters to /api/guard-stats:
    #   explicit None  → guards deliberately skipped (GUARD_ENFORCEMENT=off).
    #                    Persist as NULL so the stats query, which filters on
    #                    `guard_passed IS NOT NULL`, excludes it entirely.
    #   key absent     → nobody ran guards on this record. Fail closed (0).
    #                    This defaulted to True, so any write path that didn't
    #                    run the pipeline was recorded as having passed it —
    #                    biasing the pass rate toward "the system is clean".
    #   True / False   → a real verdict.
    _MISSING = object()
    _guard_verdict = enrichment.get("guard_passed", _MISSING)
    if _guard_verdict is _MISSING:
        guard_passed_col = 0
    elif _guard_verdict is None:
        guard_passed_col = None
    else:
        guard_passed_col = 1 if _guard_verdict else 0

    # A stored assessment supersedes any earlier rejection for this company.
    clear_rejection(conn, enrichment["company_id"])

    enrichment_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO enrichment (
            id, company_id, employee_band, recommended_angle, billing_pain_points,
            qualification_score, llm_model, llm_raw_response, assessment_breakdown,
            narrative_assessment, signal_strength,
            guard_passed, guard_score, guard_failures,
            generated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            enrichment_id,
            enrichment["company_id"],
            enrichment.get("employee_band"),
            enrichment.get("recommended_angle"),
            json.dumps(enrichment.get("billing_pain_points", [])),
            enrichment.get("qualification_score"),
            enrichment.get("llm_model"),
            enrichment.get("llm_raw_response"),
            json.dumps(enrichment.get("assessment_breakdown", {})),
            json.dumps(enrichment.get("narrative_assessment", {})),
            enrichment.get("signal_strength", "low"),
            guard_passed_col,
            enrichment.get("guard_score"),
            json.dumps(enrichment.get("guard_failures", [])),
            _now_iso(),
        ),
    )
    return enrichment_id


def record_rejection(
    conn: sqlite3.Connection,
    company_id: str,
    reason: str,
    guard_failures: Optional[list] = None,
    guard_score: Optional[float] = None,
    llm_model: Optional[str] = None,
) -> str:
    """Persist an assessment that ran cleanly and was refused storage by the
    guard pipeline.

    Replaces any prior rejection for the company rather than stacking rows —
    re-assessing a firm that fails again is the same fact, not a new one.
    """
    conn.execute("DELETE FROM assessment_rejections WHERE company_id = ?", (company_id,))
    rejection_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO assessment_rejections
            (id, company_id, reason, guard_failures, guard_score, llm_model, rejected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rejection_id,
            company_id,
            reason,
            json.dumps(guard_failures or []),
            guard_score,
            llm_model,
            _now_iso(),
        ),
    )
    return rejection_id


def clear_rejection(conn: sqlite3.Connection, company_id: str) -> None:
    """Drop any recorded rejection for this company. Called when an
    assessment is successfully stored, so a firm that failed once and then
    passed on re-assessment doesn't show as assessed AND rejected."""
    conn.execute("DELETE FROM assessment_rejections WHERE company_id = ?", (company_id,))


def update_enrichment_contacts(conn: sqlite3.Connection, company_id: str, contacts: list[dict]) -> bool:
    """Patch ONLY the `contacts` key inside the latest enrichment row's
    narrative_assessment JSON blob. Never touches qualification_score,
    opportunity_signal, guard_*, or any other column. Returns False if
    no enrichment row exists yet for this company (caller should 400), or
    if a concurrent assess/reassess replaced the row (new id) between the
    SELECT and this UPDATE — checking rowcount instead of assuming success
    means that race reports as a failed refresh rather than silently
    updating (or appearing to update) a row that's already gone."""
    row = conn.execute(
        "SELECT id, narrative_assessment FROM enrichment WHERE company_id = ? ORDER BY generated_at DESC LIMIT 1",
        (company_id,),
    ).fetchone()
    if not row:
        return False
    na = json.loads(row["narrative_assessment"] or "{}")
    na["contacts"] = contacts
    cur = conn.execute("UPDATE enrichment SET narrative_assessment = ? WHERE id = ?", (json.dumps(na), row["id"]))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Outreach emails
# ---------------------------------------------------------------------------

def upsert_email(conn: sqlite3.Connection, email: dict) -> str:
    """
    Insert or replace draft email for a company.
    Returns the email id.
    """
    conn.execute(
        "DELETE FROM outreach_emails WHERE company_id = ? AND status = 'draft'",
        (email["company_id"],),
    )

    email_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO outreach_emails (
            id, company_id, contact_id, subject, body,
            compliance_footer, llm_model, generated_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email_id,
            email["company_id"],
            email.get("contact_id"),
            email.get("subject"),
            email.get("body"),
            1,  # compliance_footer always True
            email.get("llm_model"),
            _now_iso(),
            "draft",
        ),
    )
    return email_id


# ---------------------------------------------------------------------------
# Ingestion runs
# ---------------------------------------------------------------------------

def log_run(
    run_id: str,
    source: str,
    started_at: str,
    finished_at: str,
    records_found: int,
    records_new: int,
    errors: list,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO ingestion_runs
                (id, source, started_at, finished_at, records_found, records_new, errors)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source,
                started_at,
                finished_at,
                records_found,
                records_new,
                json.dumps(errors),
            ),
        )


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------
# Whitelisted sort keys only -- sort_by/sort_dir arrive from the query string,
# so they are never interpolated into SQL directly (SQL-injection guard).
# employee_band is a categorical string ("1-10","11-50","51-200","200+") and
# would sort wrong alphabetically ("1-10" < "11-50" < "200+" < "51-200"), so
# it's mapped through a CASE expression to a numeric rank before ordering.
SORT_COLUMNS: dict[str, str] = {
    "score": "e.qualification_score",
    "name": "c.legal_name COLLATE NOCASE",
    "county": "c.county COLLATE NOCASE",
    "status": "c.cro_status COLLATE NOCASE",
    "incorporated": "c.incorporation_date",
    "size": """
        CASE e.employee_band
            WHEN '1-10' THEN 1
            WHEN '11-50' THEN 2
            WHEN '51-200' THEN 3
            WHEN '200+' THEN 4
            ELSE 0
        END
    """,
}
DEFAULT_SORT_BY = "score"
DEFAULT_SORT_DIR = "desc"


def get_companies(
    county: Optional[str] = None,
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    status: Optional[str] = None,
    needs_review: Optional[bool] = None,
    sort_by: str = DEFAULT_SORT_BY,
    sort_dir: str = DEFAULT_SORT_DIR,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """
    Return companies with their latest enrichment score joined in.
    Supports filtering by county, score range, CRO status, and sorting by
    any column in SORT_COLUMNS (score/name/county/status/incorporated/size).
    """
    query = """
        SELECT
            c.id, c.cbi_reference, c.cro_number, c.legal_name, c.trading_name,
            c.cro_status, c.incorporation_date, c.registered_address, c.county,
            c.eircode, c.source, c.ingested_at,
            e.qualification_score, e.employee_band, e.recommended_angle, e.billing_pain_points,
            e.generated_at AS assessed_at,
            o.status AS email_status,
            -- "assessed and dropped, here's why" must be distinguishable from
            -- "nobody has looked at this yet" — both otherwise present as a
            -- null qualification_score.
            r.rejected_at AS rejected_at,
            r.reason      AS rejection_reason
        FROM companies c
        LEFT JOIN enrichment e ON e.company_id = c.id AND e.generated_at = (
            SELECT MAX(generated_at) FROM enrichment WHERE company_id = c.id
        )
        LEFT JOIN outreach_emails o ON o.company_id = c.id AND o.status = 'draft'
        LEFT JOIN assessment_rejections r ON r.company_id = c.id
        WHERE 1=1
    """
    params: list[Any] = []

    if county:
        query += " AND LOWER(c.county) = LOWER(?)"
        params.append(county)
    if min_score is not None:
        query += " AND e.qualification_score >= ?"
        params.append(min_score)
    if max_score is not None:
        query += " AND e.qualification_score <= ?"
        params.append(max_score)
    if status:
        query += " AND LOWER(c.cro_status) = LOWER(?)"
        params.append(status)

    sort_column = SORT_COLUMNS.get(sort_by, SORT_COLUMNS[DEFAULT_SORT_BY])
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    # SQLite has supported NULLS LAST since 3.30 (2019); older builds ignore
    # the clause and fall back to NULLS-first-on-ASC/NULLS-last-on-DESC,
    # which is an acceptable degrade, not a crash.
    query += f" ORDER BY {sort_column} {direction} NULLS LAST, c.legal_name ASC"
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    # Parse billing_pain_points JSON
    for row in rows:
        if row.get("billing_pain_points"):
            try:
                row["billing_pain_points"] = json.loads(row["billing_pain_points"])
            except (json.JSONDecodeError, TypeError):
                row["billing_pain_points"] = []

    return rows


def count_companies(
    county: Optional[str] = None,
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    status: Optional[str] = None,
) -> int:
    """
    Total matching row count for the same filters as get_companies, used by
    the frontend to drive real pagination instead of silently truncating
    the list at whatever `limit` happens to be.
    """
    query = """
        SELECT COUNT(*) as n
        FROM companies c
        LEFT JOIN enrichment e ON e.company_id = c.id
        WHERE 1=1
    """
    params: list[Any] = []
    if county:
        query += " AND LOWER(c.county) = LOWER(?)"
        params.append(county)
    if min_score is not None:
        query += " AND e.qualification_score >= ?"
        params.append(min_score)
    if max_score is not None:
        query += " AND e.qualification_score <= ?"
        params.append(max_score)
    if status:
        query += " AND LOWER(c.cro_status) = LOWER(?)"
        params.append(status)

    with get_db_connection() as conn:
        row = conn.execute(query, params).fetchone()
    return row["n"] if row else 0


def get_company_detail(company_id: str) -> Optional[dict]:
    """
    Return full detail for one company including enrichment + email body.
    """
    with get_db_connection() as conn:
        company = conn.execute(
            "SELECT * FROM companies WHERE id = ?", (company_id,)
        ).fetchone()

        if not company:
            return None

        enrichment = conn.execute(
            "SELECT * FROM enrichment WHERE company_id = ? ORDER BY generated_at DESC LIMIT 1",
            (company_id,),
        ).fetchone()

        email = conn.execute(
            "SELECT * FROM outreach_emails WHERE company_id = ? AND status = 'draft' ORDER BY generated_at DESC LIMIT 1",
            (company_id,),
        ).fetchone()

        contacts = conn.execute(
            "SELECT * FROM contacts WHERE company_id = ?", (company_id,)
        ).fetchall()

        rejection = conn.execute(
            "SELECT * FROM assessment_rejections WHERE company_id = ? "
            "ORDER BY rejected_at DESC LIMIT 1",
            (company_id,),
        ).fetchone()

    result = dict(company)

    if enrichment:
        e = dict(enrichment)
        try:
            e["billing_pain_points"] = json.loads(e.get("billing_pain_points") or "[]")
        except (json.JSONDecodeError, TypeError):
            e["billing_pain_points"] = []
        try:
            e["assessment_breakdown"] = json.loads(e.get("assessment_breakdown") or "{}")
        except (json.JSONDecodeError, TypeError):
            e["assessment_breakdown"] = {}
        try:
            e["narrative_assessment"] = json.loads(e.get("narrative_assessment") or "null")
        except (json.JSONDecodeError, TypeError):
            e["narrative_assessment"] = None
        result["enrichment"] = e
    else:
        result["enrichment"] = None

    result["email"] = dict(email) if email else None
    result["contacts"] = [dict(c) for c in contacts]

    if rejection:
        rej = dict(rejection)
        try:
            rej["guard_failures"] = json.loads(rej.get("guard_failures") or "[]")
        except (json.JSONDecodeError, TypeError):
            rej["guard_failures"] = []
        result["rejection"] = rej
    else:
        result["rejection"] = None

    return result


def get_unenriched_companies() -> list[dict]:
    """
    Return companies that have no enrichment record yet.
    """
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.* FROM companies c
            LEFT JOIN enrichment e ON e.company_id = c.id
            WHERE e.id IS NULL
              AND LOWER(c.cro_status) = 'normal'
            ORDER BY c.ingested_at ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_score_distribution() -> list[dict]:
    """Return count of companies per score bracket for the chart."""
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT
                CASE
                    WHEN qualification_score >= 70 THEN '70-100'
                    WHEN qualification_score >= 40 THEN '40-69'
                    WHEN qualification_score >= 1 THEN '1-39'
                    ELSE 'No Score'
                END AS bracket,
                COUNT(*) AS count
            FROM enrichment
            GROUP BY bracket
            ORDER BY bracket
        """).fetchall()
    return [dict(r) for r in rows]


def get_top_opportunities(limit: int = 5) -> list[dict]:
    """Return top N companies by qualification score for the opportunities panel."""
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT c.id, c.legal_name, c.county, c.trading_name,
                   e.qualification_score, e.employee_band, e.recommended_angle,
                   o.status AS email_status
            FROM companies c
            INNER JOIN enrichment e ON e.company_id = c.id
            LEFT JOIN outreach_emails o ON o.company_id = c.id AND o.status = 'draft'
            WHERE e.qualification_score IS NOT NULL
            ORDER BY e.qualification_score DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def clear_all_enrichments() -> int:
    """Delete all enrichment records and draft emails. Returns count of deleted enrichments."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM enrichment").fetchone()
        deleted = row["n"] if row else 0
        conn.execute("DELETE FROM outreach_emails")
        conn.execute("DELETE FROM enrichment")
    return deleted


def get_companies_without_email() -> list[dict]:
    """
    Return companies that have enrichment but no draft email yet.
    """
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.* FROM companies c
            INNER JOIN enrichment e ON e.company_id = c.id
            LEFT JOIN outreach_emails o ON o.company_id = c.id AND o.status = 'draft'
            WHERE o.id IS NULL
            ORDER BY e.qualification_score DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]
