"""
db/init_db.py -- Idempotent database initialiser.
Run directly: python db/init_db.py
Safe to run multiple times (IF NOT EXISTS guards in schema.sql).
"""
import sqlite3
import os
import sys
from pathlib import Path

# Allow running from repo root or db/ directory
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

DB_PATH = os.getenv("DB_PATH", "leads_vault.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(db_path: str = DB_PATH) -> None:
    db_file = Path(db_path)
    # If path is relative, resolve from repo root
    if not db_file.is_absolute():
        db_file = ROOT / db_file

    print(f"[init_db] Initialising database at: {db_file}")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(schema)
        _apply_lightweight_migrations(conn)
        conn.commit()
        print("[init_db] Schema applied successfully (idempotent -- safe to re-run).")
    finally:
        conn.close()


def _apply_lightweight_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive migrations needed by databases created from older schemas."""
    enrichment_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(enrichment)").fetchall()
    }
    if "recommended_angle" not in enrichment_cols:
        conn.execute("ALTER TABLE enrichment ADD COLUMN recommended_angle TEXT")

    companies_cols = {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
    for col in ("company_type", "last_annual_return", "last_accounts_date", "principal_object"):
        if col not in companies_cols:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} TEXT")

    # Fix #3: normalized-name dedup key. Column for pre-existing DBs, backfill
    # for rows inserted before the column existed, then the lookup index. The
    # index must be created here (not schema.sql) so it never runs against an
    # old DB before the column exists.
    if "legal_name_normalized" not in companies_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN legal_name_normalized TEXT")
    from ingestion.cro_resolver import normalize_legal_name
    backfill = conn.execute(
        "SELECT id, legal_name FROM companies "
        "WHERE legal_name_normalized IS NULL AND legal_name IS NOT NULL"
    ).fetchall()
    for row_id, legal_name in backfill:
        normalized = normalize_legal_name(legal_name)
        if normalized:
            conn.execute(
                "UPDATE companies SET legal_name_normalized = ? WHERE id = ?",
                (normalized, row_id),
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_companies_legal_name_normalized "
        "ON companies(legal_name_normalized)"
    )


if __name__ == "__main__":
    init_db()
