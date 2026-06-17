"""Assisted-onboarding flow orchestration (UI-facing).

Walks the adapter's step graph: AUTO steps are 'filled by the service', GATE steps
pause for the owner (account / OTP / bank / contract / captcha). The owner clears a
gate from the UI; the service fills the AUTO steps in between automatically.

State lives on the OnboardingJob (current_step = last completed; waiting_on = the
gate currently blocking). Persisted via JobStore.
"""
from __future__ import annotations

from typing import Optional

from accounts_pilot.adapters import get_adapter
from accounts_pilot.adapters.base import StepKind
from accounts_pilot.drivers import booking_goals
from accounts_pilot.models.job import GateKind, JobState, OnboardingJob
from accounts_pilot.models.property_profile import PropertyProfile

# owner-facing prompts per gate
GATE_PROMPT = {
    GateKind.ACCOUNT: "Log in to your Booking.com account (create it if needed), then click Done.",
    GateKind.OTP: "Enter the verification code Booking.com sent to your email or phone.",
    GateKind.CAPTCHA: "Solve the security check shown by Booking.com, then click Done.",
    GateKind.BANK: "Enter your payout / bank details on Booking.com, then click Done.",
    GateKind.CONTRACT: "Read and accept the Booking.com partner agreement, then click Done.",
}


def _steps(ota: str):
    return get_adapter(ota).steps()


def _index_of(steps, key: Optional[str]) -> int:
    if key is None:
        return -1
    for i, s in enumerate(steps):
        if s.key == key:
            return i
    return -1


def progress(job: OnboardingJob) -> None:
    """Advance over AUTO/SYSTEM steps; stop at the next GATE (set waiting_on)."""
    steps = _steps(job.ota)
    i = _index_of(steps, job.current_step) + 1
    while i < len(steps):
        s = steps[i]
        if s.kind is StepKind.GATE:
            job.waiting_on = s.gate
            job.state = {
                GateKind.ACCOUNT: JobState.AWAITING_ACCOUNT,
                GateKind.OTP: JobState.AWAITING_OTP,
                GateKind.CAPTCHA: JobState.AWAITING_CAPTCHA,
                GateKind.BANK: JobState.AWAITING_BANK,
                GateKind.CONTRACT: JobState.AWAITING_CONTRACT,
            }[s.gate]
            return
        # AUTO or SYSTEM → service handles it
        job.current_step = s.key
        job.waiting_on = None
        job.state = JobState.SUBMITTED if s.key == "submit" else JobState.FILLING
        i += 1
    job.state = JobState.SUBMITTED


def clear_gate(job: OnboardingJob, gate: GateKind, value: str = "") -> bool:
    """Owner cleared a gate. Mark it done and continue filling. Returns False on mismatch."""
    if job.waiting_on is not gate:
        return False
    steps = _steps(job.ota)
    # the blocking gate is the first step after current_step
    nxt = _index_of(steps, job.current_step) + 1
    if 0 <= nxt < len(steps) and steps[nxt].gate is gate:
        job.current_step = steps[nxt].key
    job.waiting_on = None
    progress(job)
    return True


def status_plan(job: OnboardingJob, profile: PropertyProfile) -> list[dict]:
    """The full step list with per-step status + (for AUTO) the fill goal text."""
    steps = _steps(job.ota)
    goals = {g["step"]: g["goal"] for g in booking_goals(profile)}
    done_idx = _index_of(steps, job.current_step)
    plan = []
    for i, s in enumerate(steps):
        if i <= done_idx:
            status = "done"
        elif job.waiting_on and i == done_idx + 1 and s.gate is job.waiting_on:
            status = "waiting"
        elif i == done_idx + 1:
            status = "next"
        else:
            status = "pending"
        plan.append({
            "key": s.key, "title": s.title, "kind": s.kind.value,
            "gate": s.gate.value if s.gate else None,
            "owner_action": s.kind is StepKind.GATE,
            "goal": goals.get(s.key),
            "prompt": GATE_PROMPT.get(s.gate) if s.gate else None,
            "status": status,
        })
    return plan
