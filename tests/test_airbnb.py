"""Airbnb 'become a host' deterministic page handlers.

Same shape as the Expedia/Agoda handler tests: the generic LLM walker drives the
ordinary fields, and these handlers own ONLY the widgets the scraper/LLM can't see —
the property-type / privacy-type cards, the manual address form, the floor-plan
steppers, the amenity tiles, and the title/description/price fields. Every handler is
body-text + URL gated, so it returns False (deferring to the LLM walker) when its step
isn't on screen.
"""
from tests.conftest import FakeLocator, FakePage, FakeRuntime, demo_profile_dict
from accounts_pilot.web.live import get_session


def _airbnb(page):
    s = get_session("airbnb")
    s.rt = FakeRuntime(page)
    s.log = []                       # so _say() has somewhere to append in isolation
    return s


# --------------------------------------------------------------------------- #
# handlers exist + the session is independent
# --------------------------------------------------------------------------- #
def test_airbnb_session_has_handlers():
    s = get_session("airbnb")
    assert s.ota == "airbnb"
    assert s._pmap_path.name == "page_maps_airbnb.json"
    for m in ("_fill_airbnb_structure", "_fill_airbnb_privacy_type", "_fill_airbnb_location",
              "_fill_airbnb_floor_plan", "_fill_airbnb_amenities", "_fill_airbnb_title",
              "_fill_airbnb_description", "_fill_airbnb_price"):
        assert callable(getattr(s, m, None)), f"missing handler {m}"


# --------------------------------------------------------------------------- #
# gating: every handler defers (returns False) when its step isn't on screen
# --------------------------------------------------------------------------- #
def test_handlers_defer_when_step_absent():
    page = FakePage([{"url": "http://x/intro", "heading": "Welcome",
                      "body": "an unrelated onboarding page with no relevant fields"}])
    s = _airbnb(page)
    prof = demo_profile_dict()
    assert s._fill_airbnb_structure(prof) is False
    assert s._fill_airbnb_privacy_type(prof) is False
    assert s._fill_airbnb_location(prof) is False
    assert s._fill_airbnb_floor_plan(prof) is False
    assert s._fill_airbnb_amenities(prof) is False
    assert s._fill_airbnb_title(prof) is False
    assert s._fill_airbnb_description(prof) is False
    assert s._fill_airbnb_price(prof) is False


# --------------------------------------------------------------------------- #
# structure: 'Which of these best describes your place?' property-type cards
# --------------------------------------------------------------------------- #
def test_structure_picks_hotel_for_hotel():
    card = FakeLocator(count=1, tag="button")
    page = FakePage([{"url": "http://x/become-a-host/1/structure", "heading": "Structure",
                      "body": "which of these best describes your place?"}],
                    locators={"text:Hotel": card})
    s = _airbnb(page)
    assert s._fill_airbnb_structure({"property_type": "hotel"}) is True
    assert card.clicked


def test_structure_picks_villa_for_villa():
    card = FakeLocator(count=1, tag="button")
    page = FakePage([{"url": "http://x/become-a-host/1/structure", "heading": "S",
                      "body": "which of these best describes your place?"}],
                    locators={"text:Villa": card})
    s = _airbnb(page)
    assert s._fill_airbnb_structure({"property_type": "villa"}) is True
    assert card.clicked


def test_structure_skips_when_already_selected():
    # a card already shows aria-checked=true → don't re-click (would deselect); just advance.
    locs = {"[aria-checked='true'], [aria-pressed='true'], input:checked": FakeLocator(count=1)}
    page = FakePage([{"url": "http://x/become-a-host/1/structure", "heading": "S",
                      "body": "which of these best describes your place?"}], locators=locs)
    s = _airbnb(page)
    assert s._fill_airbnb_structure({"property_type": "hotel"}) is True


# --------------------------------------------------------------------------- #
# privacy-type: An entire place / A room / A shared room
# --------------------------------------------------------------------------- #
def test_privacy_type_entire_place_for_hotel():
    card = FakeLocator(count=1, tag="button")
    page = FakePage([{"url": "http://x/become-a-host/1/privacy-type", "heading": "P",
                      "body": "what type of place will guests have?"}],
                    locators={"text:An entire place": card})
    s = _airbnb(page)
    assert s._fill_airbnb_privacy_type({"property_type": "hotel"}) is True
    assert card.clicked


def test_privacy_type_shared_room_for_hostel():
    card = FakeLocator(count=1, tag="button")
    page = FakePage([{"url": "http://x/become-a-host/1/privacy-type", "heading": "P",
                      "body": "what type of place will guests have?"}],
                    locators={"text:A shared room": card})
    s = _airbnb(page)
    assert s._fill_airbnb_privacy_type({"property_type": "hostel"}) is True
    assert card.clicked


# --------------------------------------------------------------------------- #
# location: manual address (country first, then structured fields)
# --------------------------------------------------------------------------- #
def _addr_profile():
    return {"property_type": "hotel", "address": {
        "line1": "Mall Road", "line2": "Near Manu Market", "city": "Manali",
        "state": "Himachal Pradesh", "country": "IN", "postal_code": "175131"}}


def test_location_fills_structured_fields():
    country = FakeLocator(count=1, tag="select", value="")
    a1 = FakeLocator(count=1, tag="input", value="")
    city = FakeLocator(count=1, tag="input", value="")
    state = FakeLocator(count=1, tag="input", value="")
    zip_ = FakeLocator(count=1, tag="input", value="")
    locs = {
        "select[name*='country' i]": country,
        "input[name*='addressLine1' i]": a1,
        "input[name*='city' i]": city,
        "input[name*='state' i]": state,
        "input[name*='zip' i]": zip_,
    }
    page = FakePage([{"url": "http://x/become-a-host/1/location", "heading": "Loc",
                      "body": "where's your place located?"}], locators=locs)
    s = _airbnb(page)
    assert s._fill_airbnb_location(_addr_profile()) is True
    assert a1.filled == "Mall Road" and city.filled == "Manali"
    assert state.filled == "Himachal Pradesh" and zip_.filled == "175131"
    assert country.selected is not None              # country was chosen first


def test_location_defers_when_absent():
    page = FakePage([{"url": "http://x/other", "heading": "h", "body": "unrelated"}])
    s = _airbnb(page)
    assert s._fill_airbnb_location(_addr_profile()) is False


# --------------------------------------------------------------------------- #
# floor plan: drive the Guests stepper toward target
# --------------------------------------------------------------------------- #
def test_floor_plan_drives_guests_stepper():
    inc = FakeLocator(count=1, tag="button")
    valel = FakeLocator(count=1, label="1")          # stepper currently shows 1
    locs = {"[data-testid='stepper-floorPlanGuests-increase-button']": inc,
            "[data-testid='stepper-floorPlanGuests-value']": valel}
    page = FakePage([{"url": "http://x/become-a-host/1/floor-plan", "heading": "Basics",
                      "body": "share some basics about your place"}], locators=locs)
    s = _airbnb(page)
    prof = {"property_type": "hotel", "room_types": [
        {"name": "Std", "count": 1, "max_adults": 2, "max_children": 0, "base_rate": 2000}]}
    assert s._fill_airbnb_floor_plan(prof) is True    # guests target 2, from 1 → clicks increase
    assert inc.clicked


def test_floor_counts_clamps_and_derives():
    s = get_session("airbnb")
    prof = {"room_types": [
        {"count": 6, "max_adults": 2, "max_children": 1, "bathroom": "private",
         "beds": [{"bed_type": "queen", "count": 1}], "base_rate": 2500},
        {"count": 10, "max_adults": 3, "max_children": 2, "bathroom": "private",
         "beds": [{"bed_type": "king", "count": 1}], "base_rate": 4000}]}
    c = s._airbnb_floor_counts(prof)
    assert c["Guests"] == 16                          # total capacity, clamped to Airbnb's max
    assert c["Bedrooms"] == 16 and c["Bathrooms"] == 16
    assert c["Beds"] == 16


# --------------------------------------------------------------------------- #
# amenities: toggle a declared amenity tile
# --------------------------------------------------------------------------- #
def test_amenities_toggles_wifi_tile():
    tile = FakeLocator(count=1, tag="button")
    page = FakePage([{"url": "http://x/become-a-host/1/amenities", "heading": "Amenities",
                      "body": "tell guests what your place has to offer"}],
                    locators={"text:Wifi": tile})
    s = _airbnb(page)
    prof = {"facilities": {"internet": {"wifi": True}}}
    assert s._fill_airbnb_amenities(prof) is True
    assert tile.clicked


def test_amenity_labels_map_facilities():
    s = get_session("airbnb")
    prof = demo_profile_dict()
    labels = s._airbnb_amenity_labels(prof)
    assert "Wifi" in labels
    assert "Free parking on premises" in labels      # parking available + free
    assert "Elevator" in labels and "Washer" in labels and "Breakfast" in labels
    assert "TV" in labels                             # from a room's room_amenities


# --------------------------------------------------------------------------- #
# title / description / price text fields
# --------------------------------------------------------------------------- #
def test_title_fills_display_name():
    ta = FakeLocator(count=1, tag="textarea", value="")
    page = FakePage([{"url": "http://x/become-a-host/1/title", "heading": "Title",
                      "body": "now, let's give your place a title"}],
                    locators={"textarea": ta})
    s = _airbnb(page)
    assert s._fill_airbnb_title({"display_name": "Maple Ridge Inn"}) is True
    assert ta.filled == "Maple Ridge Inn"


def test_title_truncates_to_50_chars():
    ta = FakeLocator(count=1, tag="textarea", value="")
    page = FakePage([{"url": "http://x/t", "heading": "T", "body": "give your place a title"}],
                    locators={"textarea": ta})
    s = _airbnb(page)
    long = "A" * 80
    assert s._fill_airbnb_title({"display_name": long}) is True
    assert len(ta.filled) == 50


def test_description_fills_text():
    ta = FakeLocator(count=1, tag="textarea", value="")
    page = FakePage([{"url": "http://x/become-a-host/1/description", "heading": "Desc",
                      "body": "create your description"}], locators={"textarea": ta})
    s = _airbnb(page)
    assert s._fill_airbnb_description({"description": "A cosy mountain-view hotel."}) is True
    assert ta.filled == "A cosy mountain-view hotel."


def test_price_fills_cheapest_base_rate():
    inp = FakeLocator(count=1, tag="input", value="")
    page = FakePage([{"url": "http://x/become-a-host/1/price", "heading": "Price",
                      "body": "now, set your price"}], locators={"input#price": inp})
    s = _airbnb(page)
    prof = {"room_types": [{"base_rate": 4000}, {"base_rate": 1999}, {"base_rate": 2500}]}
    assert s._fill_airbnb_price(prof) is True
    assert inp.filled == "1999"                       # the cheapest room is the entry price


def test_price_defers_when_absent():
    page = FakePage([{"url": "http://x/other", "heading": "h", "body": "unrelated"}])
    s = _airbnb(page)
    assert s._fill_airbnb_price({"room_types": [{"base_rate": 2000}]}) is False
