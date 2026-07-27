"""Shared test helpers."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def wait_for_run(client, run_id: str, timeout: float = 20.0) -> dict:
    """Block until a background run reaches a terminal state, then return its
    final status dict.

    /api/enrich-all returns as soon as the run is queued, so tests that assert
    on what the run *did* have to wait for it rather than reading the response
    body — which now carries only the run_id and the starting counts.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/runs/{run_id}").json()
        if status["status"] in ("complete", "failed", "cancelled"):
            return status
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")
