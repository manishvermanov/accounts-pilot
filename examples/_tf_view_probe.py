"""Probe the setup-session base_url: is it an interactive browser view, or a raw CDP
endpoint? Determines how the 'Connect & log in' button works."""
import httpx
from accounts_pilot.drivers.tinyfish import TinyFishDriver
from accounts_pilot.config import settings

d = TinyFishDriver()
s = d.setup_session(settings.tinyfish_profile_id, "https://admin.booking.com/", timeout_seconds=120)
base = s["base_url"]
print("base_url:", base)
h = {"X-API-Key": settings.tinyfish_api_key}
for path in ("", "/json/version", "/json/list"):
    try:
        r = httpx.get(base + path, headers=h, timeout=20)
        ct = r.headers.get("content-type", "")
        print(f"\nGET {path or '/'} → {r.status_code} [{ct}]")
        print(r.text[:500])
    except Exception as e:
        print(f"\nGET {path or '/'} → ERROR {type(e).__name__}: {str(e)[:200]}")
