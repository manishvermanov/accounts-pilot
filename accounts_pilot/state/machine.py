"""Job state machine — persistence + the run/resume loop.

v1 uses stdlib sqlite3 (zero infra, transparent, easy to inspect locally). The
run loop is deliberately framework-free so it can later be wrapped by Celery
(task per job) or Temporal (durable workflow + signals) without rewriting the
core stepping logic.

The loop walks the adapter's step graph from `current_step`, executing AUTO
steps and parking on the first GateRequired. A human/auto-resolver clears the
gate, then `resume` continues from the next step.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from accounts_pilot.adapters import get_adapter
from accounts_pilot.adapters.base import GateRequired, StepKind
from accounts_pilot.audit.log import AuditLog
from accounts_pilot.config import settings
from accounts_pilot.gates.handler import GateHandler, GateResolution
from accounts_pilot.models.job import JobState, OnboardingJob
from accounts_pilot.models.property_profile import PropertyProfile
from accounts_pilot.runtime.browser import BrowserRuntime


class JobStore:
    def __init__(self, db_path: Optional[str] = None):
        settings.ensure_dirs()
        self.db_path = str(db_path or settings.db_path)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    property_id TEXT NOT NULL,
                    ota TEXT NOT NULL,
                    state TEXT NOT NULL,
                    current_step TEXT,
                    waiting_on TEXT,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def save(self, job: OnboardingJob) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO jobs (job_id, property_id, ota, state, current_step, waiting_on, data, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     state=excluded.state, current_step=excluded.current_step,
                     waiting_on=excluded.waiting_on, data=excluded.data, updated_at=excluded.updated_at""",
                (job.job_id, job.property_id, job.ota, job.state.value,
                 job.current_step, job.waiting_on.value if job.waiting_on else None,
                 job.model_dump_json(), job.updated_at),
            )

    def get(self, job_id: str) -> Optional[OnboardingJob]:
        with self._conn() as c:
            row = c.execute("SELECT data FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return OnboardingJob.model_validate_json(row["data"]) if row else None

    def list(self) -> list[OnboardingJob]:
        with self._conn() as c:
            rows = c.execute("SELECT data FROM jobs ORDER BY updated_at DESC").fetchall()
        return [OnboardingJob.model_validate_json(r["data"]) for r in rows]


def run_job(
    job: OnboardingJob,
    profile: PropertyProfile,
    store: JobStore,
    *,
    dry_run: bool = False,
) -> OnboardingJob:
    """Walk the adapter step graph from the job's current position.

    Parks on the first gate that can't be auto-resolved. Idempotent-ish: safe to
    call again after a gate is cleared (it picks up at the next step).
    """
    adapter = get_adapter(job.ota)
    gates = GateHandler()
    audit = AuditLog(store.db_path)

    if dry_run:
        return _plan(job, adapter, profile, audit)

    step = adapter.next_step(job.current_step)
    stealth = any(s.needs_stealth for s in adapter.steps())

    with BrowserRuntime(stealth=stealth) as rt:
        while step is not None:
            job.transition(JobState.FILLING, step=step.key)
            store.save(job)
            audit.record(job.job_id, step.key, "start", step.title)

            try:
                adapter.run_step(step, rt, profile)
            except GateRequired as gr:
                outcome = gates.handle(gr.gate, context={"page_url": rt.page.url if rt.page else ""})
                if outcome.resolution is GateResolution.PARKED:
                    job.park(gr.gate, step=step.key, note=outcome.detail)
                    store.save(job)
                    audit.record(job.job_id, step.key, "parked", outcome.detail)
                    shot = rt.screenshot(str(settings.artifacts_dir / f"{job.job_id}_{step.key}.png"))
                    audit.record(job.job_id, step.key, "screenshot", shot)
                    return job
                # auto-resolved (OTP/CAPTCHA): record and continue same step's flow
                audit.record(job.job_id, step.key, "gate_resolved", outcome.detail)

            audit.record(job.job_id, step.key, "done", "")
            rt.think()  # human pause before moving to the next wizard step
            if step.kind is StepKind.SYSTEM and step.key == "submit":
                job.transition(JobState.SUBMITTED, step=step.key)
                store.save(job)
                audit.record(job.job_id, step.key, "submitted", "listing sent for review")
                return job

            step = adapter.next_step(step.key)

    job.transition(JobState.SUBMITTED)
    store.save(job)
    return job


class _DryRuntime:
    """No-op runtime: lets AUTO step handlers run (and print their mapping) with no browser."""
    page = None
    def goto(self, *a, **k): ...
    def fill(self, *a, **k): ...
    def click(self, *a, **k): ...
    def select(self, *a, **k): ...
    def check(self, *a, **k): ...
    def scroll_to(self, *a, **k): ...
    def think(self, *a, **k): ...
    def upload(self, *a, **k): ...


def inspect_fields(profile: PropertyProfile, ota: str) -> None:
    """Print, per step, exactly what the adapter would map from the profile — no browser."""
    adapter = get_adapter(ota)
    rt = _DryRuntime()
    print(f"\nField mapping for {ota} / {profile.property_id} ({profile.display_name})\n")
    for s in adapter.steps():
        if s.kind is StepKind.GATE:
            print(f"  [{s.key}] GATE → {s.gate.value} (human / auto-resolver — not auto-filled)")
            continue
        adapter.run_step(s, rt, profile)   # handlers print their mapped values
    print("\n(inspection only — no browser, nothing submitted)")


def _plan(job: OnboardingJob, adapter, profile: PropertyProfile, audit: AuditLog) -> OnboardingJob:
    """Dry run: print the step graph + AUTO/GATE classification, touch no browser."""
    print(f"\nPlan for {job.ota} / property={profile.property_id} ({profile.display_name})")
    print(f"  {profile.total_rooms} rooms across {len(profile.room_types)} room type(s)\n")
    for i, s in enumerate(adapter.steps(), 1):
        tag = s.kind.value.upper()
        extra = f" -> gate:{s.gate.value}" if s.gate else ""
        stealth = "  [stealth]" if s.needs_stealth else ""
        print(f"  {i:>2}. {tag:<6} {s.title}{extra}{stealth}")
    print("\n(dry run — nothing submitted)")
    return job
