"""Airbnb adapter — independent self-listing (Airbnb host onboarding).

Same shape as the Agoda / Expedia / MakeMyTrip adapters: the operator logs in once (and
clears any OTP/CAPTCHA), then the generic LLM walker in web/live.py drives Airbnb's
"become a host" flow from the SAME property JSON. Airbnb-specific page handlers get added
here and in the walker as we learn Airbnb's actual pages.

Independent by construction: its own browser + thread + storage_state_airbnb.json +
page_maps_airbnb.json. The MMT/Agoda/Expedia page handlers in the walker are gated on
`self.ota`, so they never run for Airbnb.
"""
from __future__ import annotations

from accounts_pilot.adapters.base import OTAAdapter, Step, StepKind
from accounts_pilot.models.job import GateKind
from accounts_pilot.models.property_profile import PropertyProfile
from accounts_pilot.runtime.browser import BrowserRuntime

# Airbnb "become a host" self-onboarding entry point. India login that redirects
# into the host wizard after the operator signs in (Airbnb login is phone/email + OTP
# and heavily bot-protected, so the operator completes it; we then drive /become-a-host).
CONNECT_URL = "https://www.airbnb.co.in/login?redirect_url=%2Fbecome-a-host"


class AirbnbAdapter(OTAAdapter):
    ota = "airbnb"
    display_name = "Airbnb"

    def steps(self) -> list[Step]:
        # The live walker is LLM-driven, so this graph is intentionally minimal — it
        # exists to satisfy the interface and document the high-level flow.
        return [
            Step("account", "Log in / sign up on Airbnb",   StepKind.GATE,
                 gate=GateKind.ACCOUNT, url=CONNECT_URL),
            Step("verify",  "Verify OTP / email",            StepKind.GATE, gate=GateKind.OTP),
            Step("onboard", "Onboard property (LLM-driven)", StepKind.AUTO),
        ]

    def run_step(self, step: Step, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        # No-op: the generic walker (web/live.py) performs the actual fill.
        return None

    def login(self, rt: BrowserRuntime, email: str, password: str) -> str:
        """Open Airbnb host onboarding and best-effort pre-fill credentials. Returns
        'ok' | 'captcha' | 'verification'. Airbnb's login is phone/email + OTP and heavily
        bot-protected, so the operator almost always completes it manually."""
        rt.goto(CONNECT_URL)
        rt.think()
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
