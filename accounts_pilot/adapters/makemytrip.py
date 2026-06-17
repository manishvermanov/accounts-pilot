"""MakeMyTrip (MMT / Connect) adapter — scaffold.

Like Booking.com, the operator logs in once (and clears any OTP/CAPTCHA); then the
generic LLM walker in web/live.py drives MMT's onboarding form from the SAME property
JSON. MMT-specific page handlers (its address widget, room flow, etc.) get added here
and in the walker as we learn MMT's actual pages — exactly how Booking.com was built.

Login URL is a sensible default for the MMT/Connect hotelier portal; adjust if your
onboarding entry point differs.
"""
from __future__ import annotations

from accounts_pilot.adapters.base import OTAAdapter, Step, StepKind
from accounts_pilot.models.job import GateKind
from accounts_pilot.models.property_profile import PropertyProfile
from accounts_pilot.runtime.browser import BrowserRuntime

# InGo-MMT / Goibibo extranet login (the merged MakeMyTrip + Goibibo hotelier portal).
CONNECT_URL = "https://in.goibibo.com/newextranet/login"


class MakeMyTripAdapter(OTAAdapter):
    ota = "makemytrip"
    display_name = "MakeMyTrip"

    def steps(self) -> list[Step]:
        # The live walker is LLM-driven, so this graph is intentionally minimal — it
        # exists to satisfy the interface and document the high-level flow.
        return [
            Step("account",  "Log in to MMT hotelier",     StepKind.GATE, gate=GateKind.ACCOUNT,
                 url=CONNECT_URL),
            Step("verify",   "Verify OTP",                 StepKind.GATE, gate=GateKind.OTP),
            Step("onboard",  "Onboard property (LLM-driven)", StepKind.AUTO),
        ]

    def run_step(self, step: Step, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        # No-op: the generic walker (web/live.py) performs the actual fill.
        return None

    def login(self, rt: BrowserRuntime, email: str, password: str) -> str:
        """Open the MMT hotelier portal and best-effort pre-fill credentials. Returns
        'ok' | 'captcha' | 'verification'. The operator completes any CAPTCHA/OTP."""
        rt.goto(CONNECT_URL)
        rt.think()
        # already signed in (persisted session)? then no login form is shown
        login_markers = ("input[type='email']", "input[name*='email' i]",
                          "input[name*='user' i]", "input[type='tel']")
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
