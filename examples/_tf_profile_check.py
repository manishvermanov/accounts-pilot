"""Verify the BBU profile API works with the configured key. Creates a profile
(metadata only — no Booking login, no billable browser session)."""
from accounts_pilot.drivers.tinyfish import TinyFishDriver

d = TinyFishDriver()
print("api base:", d._api_base())
try:
    pid = d.create_profile("booking-accounts-pilot")
    print("PROFILE CREATED:", pid)
    print("→ add to .env:  TINYFISH_PROFILE_ID=" + str(pid))
except Exception as e:
    import traceback
    print("ERROR:", type(e).__name__, str(e)[:400])
