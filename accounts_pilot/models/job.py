"""The onboarding Job and its state model.

A job is one `property x OTA` onboarding. It MUST be resumable: a single
Booking.com run pauses for human account-setup, OTP, bank, and contract, then
for Booking.com's own multi-day review. You cannot hold a browser open for that,
so the job is persisted and resumed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobState(str, Enum):
    DRAFT = "draft"
    FILLING = "filling"
    AWAITING_ACCOUNT = "awaiting_account"     # human sets login credential
    AWAITING_OTP = "awaiting_otp"             # email/phone verification
    AWAITING_BANK = "awaiting_bank"           # human enters payout details
    AWAITING_CONTRACT = "awaiting_contract"   # human accepts partner terms
    AWAITING_CAPTCHA = "awaiting_captcha"     # solver / human
    SUBMITTED = "submitted"                   # sent to OTA
    UNDER_REVIEW = "under_review"             # OTA reviewing the listing
    LIVE = "live"                             # listing is published
    FAILED = "failed"
    NEEDS_FIX = "needs_fix"


# states where the engine is blocked waiting on something external
PARKED_STATES = {
    JobState.AWAITING_ACCOUNT,
    JobState.AWAITING_OTP,
    JobState.AWAITING_BANK,
    JobState.AWAITING_CONTRACT,
    JobState.AWAITING_CAPTCHA,
}


class GateKind(str, Enum):
    ACCOUNT = "account"
    OTP = "otp"
    BANK = "bank"
    CONTRACT = "contract"
    CAPTCHA = "captcha"


# which job-state a gate parks the job into
GATE_TO_STATE = {
    GateKind.ACCOUNT: JobState.AWAITING_ACCOUNT,
    GateKind.OTP: JobState.AWAITING_OTP,
    GateKind.BANK: JobState.AWAITING_BANK,
    GateKind.CONTRACT: JobState.AWAITING_CONTRACT,
    GateKind.CAPTCHA: JobState.AWAITING_CAPTCHA,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobEvent(BaseModel):
    at: str = Field(default_factory=_now)
    state: JobState
    step: Optional[str] = None
    note: Optional[str] = None


class OnboardingJob(BaseModel):
    job_id: str
    property_id: str
    ota: str                                  # e.g. "booking_com"
    state: JobState = JobState.DRAFT
    current_step: Optional[str] = None        # adapter step key the job is on
    waiting_on: Optional[GateKind] = None     # set when parked at a gate
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    history: list[JobEvent] = Field(default_factory=list)

    def transition(self, state: JobState, *, step: Optional[str] = None, note: Optional[str] = None) -> None:
        self.state = state
        if step is not None:
            self.current_step = step
        self.waiting_on = None
        self.updated_at = _now()
        self.history.append(JobEvent(state=state, step=step or self.current_step, note=note))

    def park(self, gate: GateKind, *, step: Optional[str] = None, note: Optional[str] = None) -> None:
        self.waiting_on = gate
        self.transition(GATE_TO_STATE[gate], step=step, note=note)
        self.waiting_on = gate  # transition() clears it; re-set for parked jobs
