"""Expedia adapter — independent self-listing (Expedia Partner Central).

Same shape as the Agoda / MakeMyTrip adapters: the operator logs in / registers once (and
clears any OTP/CAPTCHA), then the generic LLM walker in web/live.py drives Expedia's
onboarding form from the SAME property JSON. Expedia-specific page handlers get added here
and in the walker as we learn Expedia's actual pages — exactly how the others were built.

Independent by construction: its own browser + thread + storage_state_expedia.json +
page_maps_expedia.json. The MMT/Agoda page handlers in the walker are gated on `self.ota`,
so they never run for Expedia.
"""
from __future__ import annotations

from accounts_pilot.adapters.base import OTAAdapter, Step, StepKind
from accounts_pilot.models.job import GateKind
from accounts_pilot.models.property_profile import PropertyProfile
from accounts_pilot.runtime.browser import BrowserRuntime

# Expedia Partner Central "list your property" entry point (the in-app list flow; the
# operator signs in first, then this lands on the onboarding wizard).
CONNECT_URL = "https://apps.expediapartnercentral.com/en_US/list"


class ExpediaAdapter(OTAAdapter):
    ota = "expedia"
    display_name = "Expedia"

    def steps(self) -> list[Step]:
        # The live walker is LLM-driven, so this graph is intentionally minimal — it
        # exists to satisfy the interface and document the high-level flow.
        return [
            Step("account", "Log in / register on Expedia Partner Central", StepKind.GATE,
                 gate=GateKind.ACCOUNT, url=CONNECT_URL),
            Step("verify",  "Verify OTP / email",            StepKind.GATE, gate=GateKind.OTP),
            Step("onboard", "Onboard property (LLM-driven)", StepKind.AUTO),
        ]

    def run_step(self, step: Step, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        # No-op: the generic walker (web/live.py) performs the actual fill.
        return None

    def login(self, rt: BrowserRuntime, email: str, password: str) -> str:
        """Open Expedia Partner Central and best-effort pre-fill credentials. Returns
        'ok' | 'captcha' | 'verification'. The operator completes any CAPTCHA/OTP."""
        rt.goto(CONNECT_URL)
        rt.think()
        login_markers = ("input[type='email']", "input[name*='email' i]",
                         "input[name*='user' i]", "input[id*='email' i]",
                         "input[id*='user' i]")
        has_login = any(rt.has(sel, timeout_ms=2500) for sel in login_markers)
        if has_login:
            for sel in login_markers:
                if email and rt.has(sel, timeout_ms=800):
                    try:
                        rt.fill(sel, email)
                    except Exception:
                        pass
                    break
            for sel in ("input[type='password']", "input[name*='pass' i]"):
                if password and rt.has(sel, timeout_ms=800):
                    try:
                        rt.fill(sel, password)
                    except Exception:
                        pass
                    break
            rt.try_advance()
            rt.think()
        ch = rt.detect_challenge()
        return ch if ch else "ok"
