"""Read-only recon: point TinyFish at Booking's public join page and report what it
sees. No login, no credentials, no CAPTCHA solving, no submit. Just observation —
so we can see whether TinyFish reaches Booking and what defenses show up."""
from accounts_pilot.drivers.tinyfish import TinyFishDriver

d = TinyFishDriver()
print("ready:", d.ready, "| base_url:", d.base_url, "| profile:", d.browser_profile)
goal = (
    "Go to this page as someone about to list their property. "
    "Describe ONLY what you observe: the first step shown (e.g. an email field, a "
    "'Get started' button), and whether any CAPTCHA, security check, 'press and hold', "
    "or bot-detection challenge appears. "
    "Do NOT enter anything. Do NOT log in. Do NOT click submit. Just report what you see."
)
try:
    res = d.run_goal("https://join.booking.com/", goal, timeout_s=180)
    print("STATUS:", res.get("status"), "| steps:", res.get("num_of_steps"))
    print("OBSERVED:\n", (res.get("result") or {}).get("result"))
    if res.get("error"):
        print("ERROR:", res.get("error"))
except Exception as e:
    print("EXCEPTION:", type(e).__name__, str(e)[:500])
