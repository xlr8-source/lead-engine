"""
ingestion/runner.py

Ingestion pipeline orchestrator.
Runs: fetch CBI register → parse → CRO cross-resolve → persist → log run.

Usage:
    python ingestion/runner.py
    python ingestion/runner.py --skip-download   # use cached register file
"""
import argparse
import io
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Windows cp1252 can't encode characters like ≥ that appear in CRO data.
# Force UTF-8 for stdout so print() doesn't crash.
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from ingestion.cbi_fetcher import fetch_cbi_register, DOWNLOAD_DIR
from ingestion.cbi_parser import parse_cbi_register
from ingestion.cro_resolver import resolve_against_cro
from db.dal import (
    get_db_connection,
    upsert_company,
    log_run,
)


def _find_cached_register() -> Path | None:
    """Return the most recently modified register file in data/, if any."""
    candidates = []
    for pattern in ("cbi_register*", "*.pdf", "*.csv", "*.xlsx"):
        candidates.extend(DOWNLOAD_DIR.glob(pattern))
    candidates = [path for path in candidates if not path.name.startswith("cro_")]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def run_ingestion(skip_download: bool = False) -> dict:
    """
    Full ingestion pipeline.
    Returns a summary dict with records_found, records_new, errors.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    errors = []
    records_found = 0
    records_new = 0

    print(f"\n{'='*60}")
    print(f"[runner] Ingestion run {run_id}")
    print(f"[runner] Started: {started_at}")
    print(f"{'='*60}")

    # -----------------------------------------------------------------------
    # Step 1: Fetch CBI register
    # -----------------------------------------------------------------------
    register_path = None
    if skip_download:
        register_path = _find_cached_register()
        if register_path:
            print(f"[runner] Using cached register: {register_path}")
        else:
            print("[runner] No cached register found -- fetching...")

    if register_path is None:
        try:
            register_path = fetch_cbi_register()
        except Exception as e:
            msg = f"CBI fetch failed: {e}"
            print(f"[runner] ERROR: {msg}")
            errors.append(msg)

    if register_path is None:
        finished_at = datetime.now(timezone.utc).isoformat()
        log_run(run_id, "cbi_register", started_at, finished_at, 0, 0, errors)
        print("[runner] Aborting: no register file available.")
        return {"run_id": run_id, "records_found": 0, "records_new": 0, "errors": errors}

    # -----------------------------------------------------------------------
    # Step 2: Parse
    # -----------------------------------------------------------------------
    try:
        firms = parse_cbi_register(register_path)
        records_found = len(firms)
        print(f"[runner] Parsed {records_found} firms from CBI register")
    except Exception as e:
        msg = f"CBI parse failed: {e}"
        print(f"[runner] ERROR: {msg}")
        errors.append(msg)
        firms = []

    if not firms:
        finished_at = datetime.now(timezone.utc).isoformat()
        log_run(run_id, "cbi_register", started_at, finished_at, 0, 0, errors)
        return {"run_id": run_id, "records_found": 0, "records_new": 0, "errors": errors}

    # -----------------------------------------------------------------------
    # Step 3: CRO cross-resolution
    # -----------------------------------------------------------------------
    try:
        enriched_firms, cro_degraded = resolve_against_cro(firms)
        if cro_degraded:
            msg = "CRO snapshot unavailable -- all firms flagged needs_review"
            print(f"[runner] WARNING: {msg}")
            errors.append(msg)
    except Exception as e:
        msg = f"CRO resolution failed: {e} -- proceeding without CRO data"
        print(f"[runner] WARNING: {msg}")
        errors.append(msg)
        enriched_firms = firms
        for f in enriched_firms:
            f.setdefault("needs_review", True)
            f.setdefault("cro_number", None)
            f.setdefault("cro_status", None)
            f.setdefault("incorporation_date", None)
            f.setdefault("cro_match_confidence", 0.0)

    # -----------------------------------------------------------------------
    # Step 4: Persist
    # -----------------------------------------------------------------------
    ingested_at = datetime.now(timezone.utc).isoformat()

    with get_db_connection() as conn:
        for firm in enriched_firms:
            if not firm.get("legal_name"):
                continue
            try:
                is_new = upsert_company(conn, firm, ingested_at)
                if is_new:
                    records_new += 1
            except Exception as e:
                msg = f"Failed to persist '{firm.get('legal_name', '?')}': {e}"
                print(f"[runner] ERROR: {msg}")
                errors.append(msg)

    finished_at = datetime.now(timezone.utc).isoformat()
    log_run(run_id, "cbi_register", started_at, finished_at, records_found, records_new, errors)

    print(f"\n{'='*60}")
    print(f"[runner] Run complete: {run_id}")
    print(f"[runner] Records found: {records_found}")
    print(f"[runner] Records new:   {records_new}")
    print(f"[runner] Errors:        {len(errors)}")
    print(f"{'='*60}\n")

    return {
        "run_id": run_id,
        "records_found": records_found,
        "records_new": records_new,
        "errors": errors,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PayBrix CBI ingestion runner")
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Use cached register file instead of downloading again",
    )
    args = parser.parse_args()

    # Ensure DB is initialised
    from db.init_db import init_db
    init_db()

    result = run_ingestion(skip_download=args.skip_download)
    sys.exit(0 if not result["errors"] else 1)
