"""Learned page-map store: persistence, stable-locator descriptors, replay."""
from accounts_pilot.web.live import LiveSession
from tests.conftest import FakeLocator, FakePage, FakeRuntime


class _PageWith:
    """A page whose locator(sel) returns a chosen FakeLocator (for descriptor tests)."""
    def __init__(self, sel_to_loc):
        self._m = sel_to_loc
        self.main_frame = self

    @property
    def frames(self):
        return [self]

    def locator(self, sel):
        return self._m.get(sel, FakeLocator(count=0))


def test_page_map_roundtrip(tmp_data_dir):
    live = LiveSession()
    data = {"DEMO::/x::Heading": [{"by": "label", "locator": "Property Name",
                                   "action": "fill", "value": "Maple Ridge Inn"}]}
    live._save_page_maps(data)
    # the cache file is per-OTA now (page_maps_<ota>.json) — check the session's own path
    assert live._pmap_path.exists()
    assert live._pmap_path.parent == tmp_data_dir
    assert live._load_page_maps() == data


def test_load_missing_returns_empty(tmp_data_dir):
    assert LiveSession()._load_page_maps() == {}


def test_commit_pending_persists_even_in_autopilot(tmp_data_dir):
    """A solved page must be saved to the static map file in EVERY mode (incl. autopilot),
    so the next run replays it with no LLM and the flow doesn't re-decide / fail partway."""
    live = LiveSession("expedia")
    live._autopilot_active = True                       # the mode that used to skip saving
    key = "EXPEDIA::/onboarding/x::Heading"
    entries = [{"by": "css", "locator": "#currency", "action": "select", "value": "INR"}]
    live._commit_pending((key, entries))
    assert live._pmap_path.exists()                     # written to page_maps_expedia.json
    assert live._lookup_map(key) == entries             # and replayable next run (no LLM)


def test_stable_descriptor_keeps_automation_id():
    live = LiveSession()
    live.rt = FakeRuntime(_PageWith({
        "#automation_id_property_type_204": FakeLocator(count=1, label="Hotel"),
    }))
    assert live._stable_descriptor("#automation_id_property_type_204") == \
        ("css", "#automation_id_property_type_204")


def test_stable_descriptor_converts_volatile_id_to_label():
    live = LiveSession()
    live.rt = FakeRuntime(_PageWith({
        "#\\:rs\\:": FakeLocator(count=1, label="Property Name"),
        "#:rs:": FakeLocator(count=1, label="Property Name"),
    }))
    by, loc = live._stable_descriptor("#:rs:")
    assert by == "label" and loc == "Property Name"


def test_stable_descriptor_none_when_absent():
    live = LiveSession()
    live.rt = FakeRuntime(_PageWith({}))   # locator count 0
    assert live._stable_descriptor("#:rx:") is None


def test_try_progress_button_clicks_add_unit():
    """On the post-wizard overview (no Continue), it clicks 'Add unit' to flow on."""
    live = LiveSession()
    live._unit_clicks = 0
    live._unit_target = 1                      # allow one unit CTA (set by _do_fill normally)
    addbtn = FakeLocator(count=1, label="Add unit")
    page = FakePage(
        [{"url": "http://x/overview", "heading": "Set up", "body": "add unit"}],
        locators={"text:Add unit": addbtn},
    )
    live.rt = FakeRuntime(page)
    assert live._try_progress_button() is True
    assert addbtn.clicked is True


def test_address_rule_skips_overview_page(monkeypatch):
    """The address handler must NOT fire on the overview (which merely says
    '...name, address, facilities...') — only on the real address widget page."""
    live = LiveSession()
    called = {"n": 0}
    monkeypatch.setattr(live, "_fill_address", lambda v: called.__setitem__("n", called["n"] + 1) or True)
    page = FakePage([{"url": "http://x/overview", "heading": "Set up",
                      "body": "the basics. add your property name, address, facilities, and more"}])
    live.rt = FakeRuntime(page)
    rule = ("address", "address", "address", {"fields": {"city": "Manali"}, "candidates": ["Manali"]})
    did, _ = live._apply_booking_rules([rule])
    assert called["n"] == 0 and did == 0


def test_address_rule_fires_on_real_address_page(monkeypatch):
    live = LiveSession()
    called = {"n": 0}
    monkeypatch.setattr(live, "_fill_address", lambda v: called.__setitem__("n", called["n"] + 1) or True)
    page = FakePage([{"url": "http://x/address", "heading": "Where is your property?",
                      "body": "where is your property? find your address"}])
    live.rt = FakeRuntime(page)
    rule = ("address", "address", "address", {"fields": {"city": "Manali"}, "candidates": ["Manali"]})
    did, _ = live._apply_booking_rules([rule])
    assert called["n"] == 1 and did == 1


def test_try_progress_button_none_when_no_cta():
    live = LiveSession()
    page = FakePage([{"url": "http://x/p", "heading": "P", "body": "b"}])
    live.rt = FakeRuntime(page)
    assert live._try_progress_button() is False


def test_apply_stored_fills_via_label_locator():
    live = LiveSession()
    target = FakeLocator(count=1, label="Property Name", editable=True)
    page = FakePage(
        [{"url": "http://x/n", "heading": "Tell us", "body": "name"}],
        locators={"label:Property Name": target},
    )
    live.rt = FakeRuntime(page)
    did, navigated = live._apply_stored(
        [{"by": "label", "locator": "Property Name", "action": "fill", "value": "Maple Ridge Inn"}])
    assert did == 1 and navigated is False
    assert target.filled == "Maple Ridge Inn"
