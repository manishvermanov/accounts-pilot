"""Read-only: using the saved profile, check whether we're logged into Booking.
No login attempt, no fill — just reports what page the authenticated profile sees."""
from accounts_pilot.drivers.tinyfish import TinyFishDriver
from accounts_pilot.config import settings

d = TinyFishDriver()
print("profile:", settings.tinyfish_profile_id or "(default)")
goal = ("Report whether you are logged in to the Booking.com partner extranet. "
        "Do you see a sign-in / login page, or a property dashboard / setup wizard? "
        "Describe what you see. Do NOT log in, do NOT enter anything, do NOT click submit.")
try:
    res = d.run_goal("https://admin.booking.com/", goal, use_profile=True, timeout_s=180)
    print("STATUS:", res.get("status"), "| steps:", res.get("num_of_steps"))
    print("SEES:\n", (res.get("result") or {}).get("result"))
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:400])
