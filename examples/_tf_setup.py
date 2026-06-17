"""Provision a TinyFish setup-session so the user can log into Booking and save it
to the profile. Prints the full response so we can see the login interface (cdp_url
/ any viewer URL). Does NOT log in — that's the user's step."""
from accounts_pilot.drivers.tinyfish import TinyFishDriver
from accounts_pilot.config import settings

d = TinyFishDriver()
pid = settings.tinyfish_profile_id
print("profile:", pid)
try:
    s = d.setup_session(pid, "https://account.booking.com/sign-in", timeout_seconds=900)
    print("SETUP SESSION RESPONSE:")
    for k, v in s.items():
        print(f"  {k}: {v}")
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:500])
