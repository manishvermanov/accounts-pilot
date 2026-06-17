"""Probe: what does setup-session return? (so we wire the 'Connect & log in' button right).
Starts a short setup browser pointed at Booking's login — YOU would log in there."""
from accounts_pilot.drivers.tinyfish import TinyFishDriver
from accounts_pilot.config import settings

d = TinyFishDriver()
print("profile:", settings.tinyfish_profile_id)
try:
    s = d.setup_session(settings.tinyfish_profile_id, "https://admin.booking.com/", timeout_seconds=120)
    print("SETUP RESPONSE KEYS:", list(s.keys()))
    for k, v in s.items():
        print(f"  {k}: {str(v)[:120]}")
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:400])
