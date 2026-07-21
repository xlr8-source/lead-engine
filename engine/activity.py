from __future__ import annotations
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

Status = Literal["pending", "running", "complete", "failed"]
Kind = Literal["assess", "outreach", "contacts_refresh"]
OnEvent = Callable[[str, str, str, Optional[dict]], None]  # (step, label, status, metadata)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class ActivityEvent:
    run_id: str
    step: str
    label: str
    status: Status
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Run:
    run_id: str
    company_id: str
    kind: str
    status: str = "running"
    events: list = field(default_factory=list)          # list[ActivityEvent], one per step (upserted)
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


class RunRegistry:
    """In-memory, single-process run tracker. No DB, no broker — matches
    this app's single uvicorn worker and zero existing task-queue infra."""

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}
        self._active: dict[tuple[str, str], str] = {}   # (company_id, kind) -> run_id

    def start_run(self, company_id: str, kind: Kind) -> str:
        with self._lock:
            self._prune_finished()
            run_id = str(uuid.uuid4())
            self._runs[run_id] = _Run(run_id=run_id, company_id=company_id, kind=kind)
            self._active[(company_id, kind)] = run_id
            return run_id

    def get_or_start_run(self, company_id: str, kind: Kind) -> tuple[str, bool]:
        """Atomic check-then-create: closes the race where two concurrent
        requests (double-click, two tabs) each see "no active run" and both
        spawn a background job for the same company+kind. Returns
        (run_id, is_new) — callers only spawn the background thread when
        is_new is True."""
        with self._lock:
            existing = self._active.get((company_id, kind))
            if existing:
                return existing, False
            self._prune_finished()
            run_id = str(uuid.uuid4())
            self._runs[run_id] = _Run(run_id=run_id, company_id=company_id, kind=kind)
            self._active[(company_id, kind)] = run_id
            return run_id, True

    def emit(self, run_id: str, step: str, label: str, status: Status, metadata: dict | None = None) -> None:
        """Always APPENDS a new event — never mutates an existing one in
        place. get_new_events() below hands SSE clients a slice of
        run.events indexed by "how many I've already sent"; mutating an
        earlier entry in place (e.g. flipping its status from running to
        complete) is invisible to that index-based diff once the client has
        already been sent that slot, so the step's `running` glyph would
        spin forever even after the run genuinely finished (confirmed live:
        a slow ~164s outreach run completed on the backend but the frontend
        panel never saw the "complete" transition for its one step).
        Consumers (frontend and get_run_status()) already treat this as an
        upsert-by-step-name log and take the latest entry per step, so
        appending here requires no consumer-side changes."""
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            now = _now_iso()
            started_at = now if status == "running" else None
            completed_at = now if status in ("complete", "failed") else None
            if started_at is None:
                # Reuse the real start time from this step's prior entry so
                # the terminal event reports actual elapsed time instead of
                # started == completed.
                prior = next((e for e in reversed(run.events) if e.step == step and e.started_at), None)
                if prior:
                    started_at = prior.started_at
            run.events.append(ActivityEvent(
                run_id=run_id, step=step, label=label, status=status,
                started_at=started_at, completed_at=completed_at,
                metadata=metadata or {},
            ))

    def finish_run(self, run_id: str, status: Literal["complete", "failed"], error: str | None = None) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            # A step left "running" when the run ends (the job raised
            # between that step's running/complete emits) would otherwise
            # render as a permanent spinner next to a FAILED header — append
            # a terminal entry for any such straggler, matching the run's
            # own outcome. Appended, not mutated in place, for the same
            # reason emit() appends (see its docstring) — mutating an
            # already-sent index is invisible to an in-flight SSE client.
            now = _now_iso()
            latest_by_step: dict[str, ActivityEvent] = {}
            for ev in run.events:
                latest_by_step[ev.step] = ev
            for step, ev in latest_by_step.items():
                if ev.status == "running":
                    run.events.append(ActivityEvent(
                        run_id=run_id, step=step, label=ev.label, status=status,
                        started_at=ev.started_at, completed_at=now, metadata=ev.metadata,
                    ))
            run.status = status
            run.error = error
            run.finished_at = time.time()
            key = (run.company_id, run.kind)
            if self._active.get(key) == run_id:
                del self._active[key]

    def get_new_events(self, run_id: str, since_index: int) -> tuple[list[dict], str, int]:
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return [], "not_found", since_index
            new = [e.to_dict() for e in run.events[since_index:]]
            return new, run.status, len(run.events)

    def get_run_status(self, run_id: str) -> dict | None:
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return None
            return {
                "run_id": run.run_id, "company_id": run.company_id, "kind": run.kind,
                "status": run.status, "error": run.error,
                "events": [e.to_dict() for e in run.events],
            }

    def get_active_run(self, company_id: str, kind: Kind) -> str | None:
        with self._lock:
            return self._active.get((company_id, kind))

    def list_active(self) -> list[dict]:
        with self._lock:
            return [
                {"company_id": cid, "kind": kind, "run_id": rid, "status": self._runs[rid].status}
                for (cid, kind), rid in self._active.items()
                if rid in self._runs
            ]

    def _prune_finished(self, max_age_s: int = 7200) -> None:
        # caller already holds self._lock
        cutoff = time.time() - max_age_s
        stale = [rid for rid, r in self._runs.items() if r.finished_at and r.finished_at < cutoff]
        for rid in stale:
            del self._runs[rid]


registry = RunRegistry()
