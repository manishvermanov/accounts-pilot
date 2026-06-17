"""Download a profile JSON's remote photo URLs to local files and rewrite each
photo to a local `path` (so the service can upload them via set_input_files).

Usage:  python scripts/download_photos.py <profile.json> [<photos_dir>]
"""
import json, os, re, sys, urllib.request

PROFILE = sys.argv[1] if len(sys.argv) > 1 else r"C:/Users/manis/Downloads/manchester-royals.json"
prof = json.load(open(PROFILE, encoding="utf-8"))

slug = re.sub(r"[^a-z0-9]+", "-", (prof.get("display_name") or prof["property_id"]).lower()).strip("-")
DEST = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                          "..", "data", "photos", slug)
DEST = os.path.abspath(DEST)
os.makedirs(DEST, exist_ok=True)

photos = prof.get("photos", [])
cache = {}                                   # url -> local path (dedupe repeated URLs)
ok = fail = skipped = 0
for i, ph in enumerate(photos):
    url = ph.get("url")
    if not url:
        if ph.get("path"):
            skipped += 1
        continue
    if url in cache:                         # same image referenced twice — reuse the file
        ph["path"] = cache[url]
        ok += 1
        continue
    base = os.path.basename(url.split("?")[0]) or f"img_{i}.jpg"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    local = os.path.join(DEST, f"{i:03d}_{base}")
    try:
        if not (os.path.exists(local) and os.path.getsize(local) > 0):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(local, "wb") as f:
                f.write(r.read())
        ph["path"] = local
        cache[url] = local
        ok += 1
    except Exception as e:
        fail += 1
        print(f"  ! failed [{i}] {url} -> {type(e).__name__}: {e}")

json.dump(prof, open(PROFILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# validate the rewritten profile
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from accounts_pilot.models.property_profile import PropertyProfile
PropertyProfile.model_validate(prof)
print(f"downloaded ok:{ok}  failed:{fail}  already-local:{skipped}")
print("photos dir:", DEST)
print("rewrote:", PROFILE, "(validated OK)")
