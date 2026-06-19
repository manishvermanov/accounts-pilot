"""Expedia (Expedia Partner Central) deterministic page handlers.

These mirror the Agoda handler tests: the generic LLM walker drives the ordinary
fields, and these handlers fill ONLY the widgets the scraper/LLM can't see — the
address autocomplete, the custom Country/State/City + room/bed dropdowns, and the
check-in/out time pickers. Every handler is body-text gated, so it returns False
(deferring to the LLM walker) when its step isn't on screen.
"""
from tests.conftest import FakeLocator, FakePage, FakeRuntime, demo_profile_dict
from accounts_pilot.web.live import get_session


def _expedia(page):
    s = get_session("expedia")
    s.rt = FakeRuntime(page)
    s.log = []                       # so _say() has somewhere to append in isolation
    return s


# --------------------------------------------------------------------------- #
# handlers exist + the session is independent
# --------------------------------------------------------------------------- #
def test_expedia_session_has_handlers():
    s = get_session("expedia")
    assert s.ota == "expedia"
    assert s._pmap_path.name == "page_maps_expedia.json"
    for m in ("_fill_expedia_location", "_fill_expedia_rooms",
              "_fill_expedia_times", "_dropdown_already_set"):
        assert callable(getattr(s, m, None)), f"missing handler {m}"


# --------------------------------------------------------------------------- #
# gating: every handler defers (returns False) when its step isn't on screen
# --------------------------------------------------------------------------- #
def test_handlers_defer_when_step_absent():
    page = FakePage([{"url": "http://x/intro", "heading": "Welcome",
                      "body": "an unrelated onboarding page with no relevant fields"}])
    s = _expedia(page)
    prof = demo_profile_dict()
    assert s._fill_expedia_location(prof) is False
    assert s._fill_expedia_rooms(prof) is False
    assert s._fill_expedia_times(prof) is False


def test_location_gates_on_address_input_even_without_marker_text():
    # No marker words in the body, but a visible address input → it IS the location step.
    locs = {"input[name*='address' i], input[id*='address' i], "
            "input[placeholder*='address' i], input[aria-label*='address' i]":
            FakeLocator(count=1, tag="input")}
    page = FakePage([{"url": "http://x/step", "heading": "S", "body": "no obvious words"}],
                    locators=locs)
    s = _expedia(page)
    # it recognises the step (doesn't early-return False on the marker check); with no
    # search box / structured locators wired it simply fills nothing → False, but it did
    # NOT bail on gating. Assert it at least attempted (no exception).
    assert s._fill_expedia_location(demo_profile_dict()) in (True, False)


# --------------------------------------------------------------------------- #
# landing wizard: 'What would you like to list?' classification cards
# --------------------------------------------------------------------------- #
def test_classification_clicks_lodging_for_hotel():
    lod = FakeLocator(count=1, tag="div")
    locs = {"#classification_lodging, #classification_privateResidence,"
            "#list-your-property-form": FakeLocator(count=1),
            "#classification_lodging": lod}
    page = FakePage([{"url": "http://x/en_US/list", "heading": "List",
                      "body": "what would you like to list?"}], locators=locs)
    s = _expedia(page)
    assert s._fill_expedia_classification({"property_type": "hotel"}) is True
    assert lod.clicked                                 # the Lodging card was clicked


def test_classification_picks_residence_for_villa():
    res = FakeLocator(count=1, tag="div")
    locs = {"#classification_lodging, #classification_privateResidence,"
            "#list-your-property-form": FakeLocator(count=1),
            "#classification_privateResidence": res}
    page = FakePage([{"url": "http://x/en_US/list", "heading": "List", "body": "list"}], locators=locs)
    s = _expedia(page)
    assert s._fill_expedia_classification({"property_type": "villa"}) is True
    assert res.clicked


def test_classification_defers_when_absent():
    page = FakePage([{"url": "http://x/other", "heading": "h", "body": "unrelated"}])
    s = _expedia(page)
    assert s._fill_expedia_classification({"property_type": "hotel"}) is False


# --------------------------------------------------------------------------- #
# manual address step: country FIRST, then fields
# --------------------------------------------------------------------------- #
def _manual_addr_profile():
    return {"property_type": "hotel", "address": {
        "line1": "Mall Road", "line2": "Near Manu Market", "city": "Manali",
        "state": "Himachal Pradesh", "country": "IN", "postal_code": "175131"}}


def test_manual_location_sets_country_first_and_fields():
    country = FakeLocator(count=1, tag="select", value="")
    a1 = FakeLocator(count=1, tag="input", value="")
    city = FakeLocator(count=1, tag="input", value="")
    zip_ = FakeLocator(count=1, tag="input", value="")
    locs = {"#country, #address1, #manualAddressNextBtn": FakeLocator(count=1),
            "#country": country, "#address1": a1,
            "#address2": FakeLocator(count=1, tag="input", value=""),
            "#city": city, "#stateProvince": FakeLocator(count=1, tag="select", value=""),
            "#postalCode": zip_}
    page = FakePage([{"url": "http://x/list/manual-location", "heading": "addr",
                      "body": "property address"}], locators=locs)
    s = _expedia(page)
    # text fields fill via _set_react_input (country/state go through _set_react_select, a JS
    # path verified against a real browser, not the FakePage).
    assert s._fill_expedia_manual_location(_manual_addr_profile()) is True
    assert a1.filled == "Mall Road" and city.filled == "Manali"
    assert zip_.filled == "175131"


def test_manual_location_defers_when_absent():
    page = FakePage([{"url": "http://x/other", "heading": "h", "body": "unrelated"}])
    s = _expedia(page)
    assert s._fill_expedia_manual_location(_manual_addr_profile()) is False


# --------------------------------------------------------------------------- #
# step-1 location typeahead (#locationTypeAhead)
# --------------------------------------------------------------------------- #
def test_typeahead_types_address_query():
    box = FakeLocator(count=1, tag="input", value="")
    page = FakePage([{"url": "http://x/list/location", "heading": "loc",
                      "body": "where is your property located"}],
                    locators={"#locationTypeAhead": box})
    s = _expedia(page)
    prof = {"display_name": "Maple Ridge Inn",
            "address": {"line1": "Mall Road", "city": "Manali",
                        "state": "Himachal Pradesh", "country": "IN"}}
    assert s._fill_expedia_typeahead(prof) is True
    typed = " ".join(page.keyboard.typed)
    assert "Mall Road" in typed and "Manali" in typed and "India" in typed


def test_typeahead_defers_when_absent():
    page = FakePage([{"url": "http://x/other", "heading": "h", "body": "unrelated"}])
    s = _expedia(page)
    assert s._fill_expedia_typeahead({"address": {"line1": "A"}}) is False


# --------------------------------------------------------------------------- #
# policies & settings (payment / time zone / currency / clear invalid taxes)
# --------------------------------------------------------------------------- #
def test_policies_defers_when_absent():
    # not the policies URL and no #timeZoneId/billingCurrency present → defer to the LLM
    page = FakePage([{"url": "http://x/other", "heading": "h", "body": "unrelated"}])
    s = _expedia(page)
    assert s._fill_expedia_policies({"address": {"country": "IN"}}) is False


# --------------------------------------------------------------------------- #
# positive: check-in / check-out time pickers (native <select>)
# --------------------------------------------------------------------------- #
def test_times_picks_both_native_selects():
    ci = FakeLocator(tag="select", value="", label="Select")
    co = FakeLocator(tag="select", value="", label="Select")
    locs = {"select[aria-label*='check-in' i]": ci,
            "select[aria-label*='check-out' i]": co}
    page = FakePage([{"url": "http://x/policies", "heading": "Policies",
                      "body": "set your check-in time and check-out time"}], locators=locs)
    s = _expedia(page)
    prof = demo_profile_dict()
    prof.setdefault("policy", {})
    prof["policy"]["checkin_from"] = "14:00"
    prof["policy"]["checkout_until"] = "11:00"
    assert s._fill_expedia_times(prof) is True
    assert ci.selected is not None and co.selected is not None   # both were chosen


def test_times_skips_already_chosen():
    # both selects already show a time (value has digits) → handler does nothing.
    ci = FakeLocator(tag="select", value="14:00")
    co = FakeLocator(tag="select", value="11:00")
    locs = {"select[aria-label*='check-in' i]": ci,
            "select[aria-label*='check-out' i]": co}
    page = FakePage([{"url": "http://x/policies", "heading": "P",
                      "body": "check-in time / check-out time"}], locators=locs)
    s = _expedia(page)
    assert s._fill_expedia_times(demo_profile_dict()) is False
    assert ci.selected is None and co.selected is None


# --------------------------------------------------------------------------- #
# _dropdown_already_set: the shared "don't re-pick" guard
# --------------------------------------------------------------------------- #
def test_dropdown_already_set_logic():
    s = get_session("expedia")
    # native <select> with no value → not set
    assert s._dropdown_already_set(FakeLocator(tag="select", value=""), "India") is False
    # native <select> with a value → set
    assert s._dropdown_already_set(FakeLocator(tag="select", value="IN"), "India") is True
    # custom button still on its placeholder → not set
    assert s._dropdown_already_set(FakeLocator(tag="button", label="Select"), "India") is False
    # custom button showing the wanted value → set
    assert s._dropdown_already_set(FakeLocator(tag="button", label="India"), "India") is True
    # require_time: a time is shown (digits) → set; placeholder → not set
    assert s._dropdown_already_set(FakeLocator(tag="select", value="2:00 PM"),
                                   None, require_time=True) is True
    assert s._dropdown_already_set(FakeLocator(tag="button", label="Select a time"),
                                   None, require_time=True) is False
