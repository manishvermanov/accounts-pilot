"""Standalone dry-run smoke test for the Expedia handlers against a REAL headless
Chromium DOM (not the FakePage). It cannot reach Expedia's live form (that's behind
operator login), so instead it builds Expedia-SHAPED pages — a location step, a
policies/time step, and a rooms step — from the demo profile's real values, points a
LiveSession("expedia") at each, runs the handler, and asserts the DOM actually changed.

This is the strongest selector/helper check possible without Expedia credentials: it
proves the resilient selectors + _open_and_pick / _select_robust / _set_react_input
actually fill a real browser. Run:  .venv/Scripts/python.exe scripts/expedia_smoke.py
"""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from accounts_pilot.web.live import get_session

ROOT = Path(__file__).resolve().parent.parent
PROFILE = json.loads((ROOT / "examples" / "booking_engine" / "DEMO-HOTEL-01.json").read_text(encoding="utf-8"))


class Shim:
    """Minimal runtime: the handlers only need .page, .think, .detect_challenge."""
    def __init__(self, page):
        self.page = page

    def think(self, *a, **k):
        pass

    def detect_challenge(self):
        return None

    def try_advance(self):
        return False


LOCATION_HTML = """
<h1>Where's your property located?</h1>
<label>Search<input aria-label="Search for your address" placeholder="Search for your address"></label>
<label>Address line 1<input name="addressLine1" aria-label="Address line 1"></label>
<label>Address line 2<input name="addressLine2" aria-label="Address line 2"></label>
<label>City<input name="city" aria-label="City"></label>
<label>Postal code<input name="postalCode" placeholder="Postal code"></label>
<label>Country
  <select name="country"><option value="">Select</option>
    <option value="US">United States</option><option value="IN">India</option></select></label>
<label>State/Province
  <select name="stateProvince"><option value="">Select</option>
    <option value="GA">Goa</option><option value="HP">Himachal Pradesh</option></select></label>
"""

POLICIES_HTML = """
<h1>Property policies</h1>
<p>Set your check-in time and check-out time.</p>
<label>Check-in time
  <select aria-label="Check-in time"><option value="">Select</option>
    <option value="12:00">12:00</option><option value="13:00">13:00</option>
    <option value="14:00">14:00</option></select></label>
<label>Check-out time
  <select aria-label="Check-out time"><option value="">Select</option>
    <option value="10:00">10:00</option><option value="11:00">11:00</option></select></label>
"""

ROOMS_HTML = """
<h1>Set up your rooms</h1>
<p>Choose the room type and bed type.</p>
<label>Room type
  <select aria-label="Room type" name="roomType"><option value="">Select</option>
    <option>Standard</option><option>Deluxe</option><option>Family Suite</option></select></label>
<label>Bed type
  <select aria-label="Bed type" name="bedType"><option value="">Select</option>
    <option>Single</option><option>Double</option><option>Queen</option><option>King</option></select></label>
"""


def report(name, page, ok, extra=""):
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}{(' — ' + extra) if extra else ''}")
    return ok


def main():
    s = get_session("expedia")
    s.log = []
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        s.rt = Shim(page)

        # ---- 1) LOCATION ----
        print("LOCATION step:")
        page.set_content(LOCATION_HTML)
        ret = s._fill_expedia_location(PROFILE)
        line1 = page.locator("input[name='addressLine1']").input_value()
        city = page.locator("input[name='city']").input_value()
        postal = page.locator("input[name='postalCode']").input_value()
        country = page.locator("select[name='country']").input_value()
        state = page.locator("select[name='stateProvince']").input_value()
        results.append(report("handler returned True", page, ret is True, str(ret)))
        results.append(report("address line1 filled", page, line1 == "Mall Road", repr(line1)))
        results.append(report("city filled", page, city == "Manali", repr(city)))
        results.append(report("postal filled", page, postal == "175131", repr(postal)))
        results.append(report("country selected = IN", page, country == "IN", repr(country)))
        results.append(report("state selected = HP", page, state == "HP", repr(state)))

        # ---- 2) POLICIES / TIMES ----
        print("POLICIES (check-in/out times) step:")
        page.set_content(POLICIES_HTML)
        ret = s._fill_expedia_times(PROFILE)
        ci = page.locator("select[aria-label='Check-in time']").input_value()
        co = page.locator("select[aria-label='Check-out time']").input_value()
        results.append(report("handler returned True", page, ret is True, str(ret)))
        results.append(report("check-in = 13:00 (from policy)", page, ci == "13:00", repr(ci)))
        results.append(report("check-out = 11:00 (from policy)", page, co == "11:00", repr(co)))

        # ---- 3) ROOMS ----
        print("ROOMS step:")
        page.set_content(ROOMS_HTML)
        ret = s._fill_expedia_rooms(PROFILE)
        rt0 = PROFILE["room_types"][0]
        rtype = page.locator("select[name='roomType']").input_value()
        btype = page.locator("select[name='bedType']").input_value()
        results.append(report("handler returned True", page, ret is True, str(ret)))
        results.append(report(f"room type picked (want first room '{rt0['name']}')",
                              page, bool(rtype), repr(rtype)))
        results.append(report("bed type picked", page, bool(btype), repr(btype)))

        browser.close()

    passed = sum(1 for r in results if r)
    print(f"\nSMOKE: {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
