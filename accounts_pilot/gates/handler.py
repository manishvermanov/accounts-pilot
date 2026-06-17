"""Gate handler — routes each GATE to auto-resolve or park-and-notify.

  ACCOUNT  -> park (human sets login credential)
  OTP      -> auto-resolve via OTPResolver if configured, else park
  BANK     -> park (human enters payout details; engine never auto-submits financials)
  CONTRACT -> park (human accepts the partner agreement)
  CAPTCHA  -> auto-resolve via CaptchaSolver if configured, else park

"Park" means: the state machine moves the job into the matching AWAITING_* state
and stops. A human completes the step (today via the OTA UI / later via the
dashboard) and calls `resume`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from accounts_pilot.config import settings
from accounts_pilot.gates.captcha import CaptchaSolver
from accounts_pilot.gates.otp import OTPResolver
from accounts_pilot.models.job import GateKind


class GateResolution(str, Enum):
    RESOLVED = "resolved"   # handled automatically; engine may continue
    PARKED = "parked"       # job parked for a human


@dataclass
class GateOutcome:
    resolution: GateResolution
    detail: str = ""
    value: str | None = None   # e.g. the OTP code or captcha token, when RESOLVED


class GateHandler:
    # gates that are always human-only, regardless of configuration
    HUMAN_ONLY = {GateKind.ACCOUNT, GateKind.BANK, GateKind.CONTRACT}

    def __init__(self):
        self.otp = OTPResolver()
        self.captcha = CaptchaSolver(
            provider=settings.captcha_provider,
            api_key=settings.captcha_api_key,
        )

    def handle(self, gate: GateKind, *, context: dict | None = None) -> GateOutcome:
        context = context or {}

        if gate in self.HUMAN_ONLY:
            return GateOutcome(GateResolution.PARKED, f"{gate.value}: human action required")

        if gate is GateKind.OTP:
            code = self.otp.try_resolve(channel=context.get("channel", "email"))
            if code:
                return GateOutcome(GateResolution.RESOLVED, "otp auto-read", value=code)
            return GateOutcome(GateResolution.PARKED, "otp: no resolver configured — enter manually")

        if gate is GateKind.CAPTCHA:
            token = self.captcha.try_solve(
                site_key=context.get("site_key", ""),
                page_url=context.get("page_url", ""),
                kind=context.get("kind", "recaptcha_v2"),
            )
            if token:
                return GateOutcome(GateResolution.RESOLVED, "captcha solved", value=token)
            return GateOutcome(GateResolution.PARKED, "captcha: no solver configured — solve manually")

        return GateOutcome(GateResolution.PARKED, f"{gate.value}: unhandled — parked")
