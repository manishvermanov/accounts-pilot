"""Throwaway: verify the TinyFish key + endpoint + client work, on a harmless
public page (read-only). Run: python examples/_smoke_tinyfish.py"""
from accounts_pilot.drivers.tinyfish import TinyFishDriver

d = TinyFishDriver()
print("ready:", d.ready, "| base_url:", d.base_url, "| profile:", d.browser_profile)
try:
    res = d.run_goal("https://example.com", "Return the text of the main heading on this page.", timeout_s=120)
    print("RESULT:", str(res)[:600])
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:400])
