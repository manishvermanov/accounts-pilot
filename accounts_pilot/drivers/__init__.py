"""Fill drivers — how property data gets entered into an OTA wizard.

  - Playwright/CloakBrowser (default): hand-coded per-OTA adapter + captured selectors.
  - TinyFish (optional): cloud AI web-agent driven by plain-English goals (no selectors).

Both consume the SAME PropertyProfile. Gates (account / CAPTCHA / OTP / bank /
contract) are NOT a driver's job — they stay owner-handled in the assisted model.
"""
from accounts_pilot.drivers.tinyfish import TinyFishDriver, booking_goals

__all__ = ["TinyFishDriver", "booking_goals"]
