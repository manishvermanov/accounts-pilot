"""Offline 'Simulate' engine — dry-run any property JSON without a live OTA login.

The operator pastes their own JSON, picks an OTA, and this:
  1. validates it against PropertyProfile (catches schema errors before a real run),
  2. reports field coverage + photo pre-flight warnings (OTA-agnostic), and
  3. for OTAs with offline handlers, drives those handlers against synthetic
     OTA-SHAPED pages in a headless Chromium and reports exactly what got filled.

It is the UI-facing sibling of scripts/expedia_smoke.py: same idea (prove the
deterministic handlers fill a real DOM), but parameterised by an arbitrary profile
so the operator can test their OWN data. It NEVER touches the live per-OTA browser
session — it spins a throwaway LiveSession with a silenced _say(), so a connected
live run is never disturbed and data/logs/<ota>.log stays clean.
"""
from __future__ import annotations

import os
from typing import Callable

# ---- a tiny runtime the handlers can drive (they only need .page + .think) ----
class _Shim:
    def __init__(self, page):
        self.page = page

    def think(self, *a, **k):
        pass

    def detect_challenge(self):
        return None

    def try_advance(self):
        return False


def _slug(s: str) -> str:
    return "".join(ch for ch in str(s).upper() if ch.isalnum())[:6] or "OPT"


# --------------------------------------------------------------------------- #
# synthetic, profile-driven page fixtures (one per onboarding step). Each one
# guarantees the profile's own value is a selectable option, so the simulation
# answers "given a page that offers your value, does the handler pick it?".
# --------------------------------------------------------------------------- #
def _loc_page(prof: dict) -> str:
    a = prof.get("address", {}) or {}
    country_label = ("India" if str(a.get("country", "")).upper() in ("IN", "INDIA")
                     else (a.get("country") or ""))
    state = a.get("state") or ""
    country_opts = ("<option value=''>Select</option><option value='IN'>India</option>"
                    "<option value='US'>United States</option>"
                    "<option value='GB'>United Kingdom</option>")
    state_opts = "<option value=''>Select</option>"
    if state:
        state_opts += f"<option value='{_slug(state)}'>{state}</option>"
    state_opts += "<option value='ZZ'>Other Province</option>"
    return (
        "<h1>Where's your property located?</h1>"
        "<label>Search<input aria-label='Search for your address' "
        "placeholder='Search for your address'></label>"
        "<label>Address line 1<input name='addressLine1' aria-label='Address line 1'></label>"
        "<label>Address line 2<input name='addressLine2' aria-label='Address line 2'></label>"
        "<label>City<input name='city' aria-label='City'></label>"
        "<label>Postal code<input name='postalCode' placeholder='Postal code'></label>"
        f"<label>Country<select name='country'>{country_opts}</select></label>"
        f"<label>State/Province<select name='stateProvince'>{state_opts}</select></label>"
    )


def _times_page(prof: dict) -> str:
    pol = prof.get("policy", {}) or {}
    ci = pol.get("checkin_from", "14:00")
    co = pol.get("checkout_until", "11:00")

    def opts(*times):
        seen, body = set(), "<option value=''>Select</option>"
        for t in times:
            if t and t not in seen:
                seen.add(t)
                body += f"<option value='{t}'>{t}</option>"
        return body

    return (
        "<h1>Property policies</h1><p>Set your check-in time and check-out time.</p>"
        f"<label>Check-in time<select aria-label='Check-in time'>{opts('12:00', ci, '15:00')}</select></label>"
        f"<label>Check-out time<select aria-label='Check-out time'>{opts('10:00', co, '12:00')}</select></label>"
    )


def _rooms_page(prof: dict) -> str:
    rooms = prof.get("room_types", []) or []
    first = rooms[0] if rooms else {}
    name = first.get("name") or "Standard"
    beds = first.get("beds") or []
    bed = (beds[0].get("bed_type") if beds else "double") or "double"
    bed_label = str(bed).replace("_", " ").title()

    def opts(values):
        seen, body = set(), "<option value=''>Select</option>"
        for v in values:
            if v and v not in seen:
                seen.add(v)
                body += f"<option>{v}</option>"
        return body

    rt_first = name.split()[0] if " " in name else name
    return (
        "<h1>Set up your rooms</h1><p>Choose the room type and bed type.</p>"
        f"<label>Room type<select aria-label='Room type' name='roomType'>"
        f"{opts([name, rt_first, 'Deluxe', 'Suite'])}</select></label>"
        f"<label>Bed type<select aria-label='Bed type' name='bedType'>"
        f"{opts([bed_label, 'Single', 'Double', 'Queen', 'King'])}</select></label>"
    )


# ota -> [(step label, page builder, handler method name)]
_FLOWS: "dict[str, list[tuple[str, Callable[[dict], str], str]]]" = {
    "expedia": [
        ("Location", _loc_page, "_fill_expedia_location"),
        ("Check-in / Check-out times", _times_page, "_fill_expedia_times"),
        ("Rooms", _rooms_page, "_fill_expedia_rooms"),
    ],
}


# --------------------------------------------------------------------------- #
# DOM snapshot / diff — what did the handler actually fill?
# --------------------------------------------------------------------------- #
_SNAP_JS = """() => {
  const out = [];
  document.querySelectorAll('input,select,textarea').forEach((e, i) => {
    let val = '', text = '';
    if (e.tagName === 'SELECT') {
      val = e.value; const o = e.selectedOptions[0]; text = o ? (o.textContent || '').trim() : '';
    } else { val = e.value || ''; }
    const label = e.getAttribute('aria-label') || e.getAttribute('name')
                  || e.getAttribute('placeholder') || (e.tagName.toLowerCase() + '#' + i);
    out.push({ i, label, val, text });
  });
  return out;
}"""


def _snapshot(page) -> list:
    try:
        return page.evaluate(_SNAP_JS)
    except Exception:
        return []


def _diff(before: list, after: list) -> list:
    bym = {b["i"]: b for b in before}
    out = []
    for a in after:
        prev = (bym.get(a["i"], {}) or {}).get("val") or ""
        if (a["val"] or "") != prev:
            out.append({"field": a["label"], "value": a["text"] or a["val"]})
    return out


def _photo_warnings(photos: list) -> list:
    w = []
    if len(photos) < 10:
        w.append(f"Only {len(photos)} photo(s) — OTAs usually want ≥ 10.")
    small = 0
    for ph in photos:
        path = (ph or {}).get("path")
        if path and os.path.exists(path):
            try:
                if os.path.getsize(path) < 100 * 1024:
                    small += 1
            except Exception:
                pass
    if small:
        w.append(f"{small} photo(s) under 100 KB — may be rejected for low resolution.")
    return w


def _coverage(profile: dict, p) -> dict:
    a = profile.get("address", {}) or {}
    c = profile.get("contact", {}) or {}
    fields = {
        "display_name": profile.get("display_name"),
        "property_type": profile.get("property_type"),
        "address.line1": a.get("line1"),
        "address.city": a.get("city"),
        "address.state": a.get("state"),
        "address.postal_code": a.get("postal_code"),
        "contact.email": c.get("email"),
        "contact.phone": c.get("phone"),
    }
    present = {k: bool(v) for k, v in fields.items()}
    photos = profile.get("photos", []) or []
    return {
        "rooms": p.total_rooms,
        "room_types": len(p.room_types),
        "photos": len(photos),
        "present": present,
        "missing": [k for k, v in present.items() if not v],
        "photo_warnings": _photo_warnings(photos),
    }


def available_otas() -> list:
    """OTAs that have an offline handler simulation (the rest get validation + coverage)."""
    return sorted(_FLOWS)


def simulate(ota: str, profile: dict) -> dict:
    """Validate `profile`, report coverage, and (for supported OTAs) dry-run the
    deterministic handlers against synthetic pages. Returns a JSON-able report."""
    from accounts_pilot.models.property_profile import PropertyProfile

    report = {"ota": ota, "ok": True, "validation": {"ok": True},
              "coverage": {}, "steps": [], "notes": [], "filled_total": 0}
    try:
        p = PropertyProfile.model_validate(profile)
    except Exception as e:
        report["ok"] = False
        report["validation"] = {"ok": False, "error": str(e)}
        report["notes"].append("Fix the validation error above, then simulate again.")
        return report

    report["coverage"] = _coverage(profile, p)

    flows = _FLOWS.get(ota, [])
    if not flows:
        report["notes"].append(
            f"No offline handler simulation for ‘{ota}’ yet — validated the JSON + checked "
            f"field coverage only. Its custom widgets are exercised on the live OTA DOM. "
            f"(Offline sim available for: {', '.join(available_otas()) or 'none'}.)")
        return report

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        report["notes"].append(f"Headless browser unavailable ({type(e).__name__}: {e}) — "
                               f"validated the JSON + coverage only.")
        return report

    # throwaway session so a CONNECTED live session for this OTA is never touched, and
    # _say() is silenced so data/logs/<ota>.log isn't polluted by the simulation.
    from accounts_pilot.web.live import LiveSession
    s = LiveSession(ota)
    s.log = []
    s._say = s.log.append          # type: ignore[assignment]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        s.rt = _Shim(page)         # type: ignore[assignment]
        for step_label, builder, method in flows:
            page.set_content("<body>" + builder(profile) + "</body>")
            before = _snapshot(page)
            try:
                ret = bool(getattr(s, method)(profile))
                err = None
            except Exception as e:
                ret, err = False, f"{type(e).__name__}: {e}"
            after = _snapshot(page)
            step = {"step": step_label, "handler_fired": ret, "filled": _diff(before, after)}
            if err:
                step["error"] = err
            report["steps"].append(step)
        browser.close()

    report["filled_total"] = sum(len(st.get("filled", [])) for st in report["steps"])
    return report


def simulate_all(profile: dict) -> dict:
    """GLOBAL dry-run — not tied to one channel. Validates the JSON once, reports coverage
    once, then runs the offline handler simulation for EVERY OTA that has one. Returns a
    combined report the UI renders as one result. Channels without offline handlers are
    covered by the JSON validation + field coverage (their custom widgets only exist on the
    live OTA DOM)."""
    from accounts_pilot.models.property_profile import PropertyProfile

    report = {"ota": "all", "ok": True, "validation": {"ok": True},
              "coverage": {}, "otas": {}, "notes": [], "filled_total": 0}
    try:
        p = PropertyProfile.model_validate(profile)
    except Exception as e:
        report["ok"] = False
        report["validation"] = {"ok": False, "error": str(e)}
        report["notes"].append("Fix the validation error above, then simulate again.")
        return report

    report["coverage"] = _coverage(profile, p)
    supported = available_otas()
    if not supported:
        report["notes"].append("No offline handler simulation wired for any channel yet — "
                               "validated the JSON + field coverage only.")
        return report

    for ota in supported:
        r = simulate(ota, profile)         # re-validate (cheap) + run that channel's handlers
        report["otas"][ota] = {"steps": r.get("steps", []),
                               "filled_total": r.get("filled_total", 0)}
    report["filled_total"] = sum(o["filled_total"] for o in report["otas"].values())
    report["notes"].append(
        f"Offline handler simulation ran for: {', '.join(supported)}. Every other channel is "
        f"covered by the JSON validation + field coverage above (its custom widgets are only "
        f"present on the live OTA page).")
    return report
