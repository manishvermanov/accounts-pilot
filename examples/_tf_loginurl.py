"""Start a setup session and construct an interactive DevTools view URL where a human
can see + click the remote Booking login. Prints the URL to test."""
import httpx
from accounts_pilot.drivers.tinyfish import TinyFishDriver
from accounts_pilot.config import settings

d = TinyFishDriver()
s = d.setup_session(settings.tinyfish_profile_id, "https://admin.booking.com/", timeout_seconds=300)
base = s["base_url"]                       # https://host/tf-xxx
host_path = base.replace("https://", "")   # host/tf-xxx
pages = httpx.get(base + "/json/list", headers={"X-API-Key": settings.tinyfish_api_key}, timeout=20).json()
page = next((p for p in pages if p.get("type") == "page"), pages[0])
page_id = page["id"]
login_url = f"{base}/devtools/inspector.html?wss={host_path}/devtools/page/{page_id}"
print("SESSION_ID:", s["session_id"])
print("PAGE TITLE:", page.get("title"), "| URL:", page.get("url", "")[:60])
print("LOGIN_URL:", login_url)
