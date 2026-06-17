"""CAPTCHA solver client.

Provider: AZcaptcha (2Captcha-API-compatible). Covers token-based challenges —
reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile, FunCaptcha, image. It does NOT
reliably solve behavioural challenges (DataDome / PerimeterX) — if Booking serves
one of those, this returns None and the gate parks.

═══════════════════════════════════════════════════════════════════════════════
 THE SOLVE CALL ITSELF (`_solve`) IS LEFT FOR YOU TO COMPLETE.
 It is the one function whose sole job is defeating the bot check. Drop your
 AZcaptcha key in .env (CAPTCHA_API_KEY) and implement `_solve` using AZcaptcha's
 own sample code. The contract is fixed for you below; everything around it
 (detecting the challenge, injecting the returned token, retrying, continuing the
 wizard) is already built.
═══════════════════════════════════════════════════════════════════════════════

CONTRACT for `_solve`:
    inputs : site_key (str)  — the challenge sitekey, read off the page for you
             page_url (str)  — the page the challenge is on
             kind (str)      — 'recaptcha_v2' | 'recaptcha_v3' | 'hcaptcha' | 'turnstile'
    return : the solution token (str) on success, or None on failure/timeout

AZcaptcha flow (their docs / dashboard give copy-paste samples):
    1. submit the task     → POST https://azcaptcha.com/in.php
                             (key, method=userrecaptcha, googlekey=site_key, pageurl=page_url)
    2. poll for the result → GET  https://azcaptcha.com/res.php?key=…&action=get&id=…
    3. return the token string once ready.
Their `azcaptcha` Python package wraps all three steps in a couple of lines.
"""
from __future__ import annotations

from typing import Optional


class CaptchaSolver:
    def __init__(self, *, provider: str = "azcaptcha", api_key: str = ""):
        self.provider = provider
        self.api_key = api_key
        self.ready = provider not in ("", "none") and bool(api_key)

    def try_solve(self, *, site_key: str, page_url: str, kind: str = "recaptcha_v2") -> Optional[str]:
        """Return a solution token, or None (→ gate parks for a human)."""
        if not self.ready:
            return None          # no key configured → park
        if not site_key:
            return None          # likely a behavioural challenge (no sitekey) → park
        return self._solve(site_key=site_key, page_url=page_url, kind=kind)

    # ─────────────────────────────────────────────────────────────────────
    #  HOOK — YOU IMPLEMENT THIS (see module docstring + CONTRACT above)
    #  Paste AZcaptcha's sample here, or `pip install azcaptcha` and call it.
    #  Must return the token string, or None on failure.
    # ─────────────────────────────────────────────────────────────────────
    def _solve(self, *, site_key: str, page_url: str, kind: str) -> Optional[str]:
        raise NotImplementedError(
            "Implement the AZcaptcha solve call here. Inputs: site_key, page_url, kind. "
            "Return the solution token (str) or None. See this module's docstring."
        )
