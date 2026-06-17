"""Multi-unit: detect the room sub-flow, and add each room type distinctly."""
from accounts_pilot.web.live import LiveSession
from tests.conftest import FakeLocator, FakePage, FakeRuntime


def _live_on(url):
    live = LiveSession()
    live.rt = FakeRuntime(FakePage([{"url": url, "heading": "H", "body": "b"}]))
    return live


def test_is_unit_flow_detects_room_setup_urls():
    assert _live_on("https://join.booking.com/become-a-host/unit-name.html?unit_id=87&flow_id=Core_Subflow_Room_Setup")._is_unit_flow()
    assert _live_on("https://join.booking.com/become-a-host/price.html?unit_id=87")._is_unit_flow()
    assert _live_on("https://x/price-overview.html?unit_id=87")._is_unit_flow()


def test_is_unit_flow_false_on_normal_pages():
    assert not _live_on("https://join.booking.com/become-a-host/address.html?aid=0")._is_unit_flow()
    assert not _live_on("https://join.booking.com/become-a-host/overview-room.html?return_path=%2Foverview-room.html")._is_unit_flow()


def test_current_unit_id_parses_url():
    live = _live_on("https://join.booking.com/become-a-host/bedroom.html?unit_id=870011822&flow_id=Core_Subflow_Room_Setup")
    assert live._current_unit_id() == "870011822"
    assert _live_on("https://x/address.html?aid=0")._current_unit_id() is None


def test_add_unit_clicked_while_rooms_remain():
    live = LiveSession()
    live._unit_order = []                         # 0 rooms started so far
    live._unit_target = 3
    live._room_list = [{"name": "Standard Room"}, {"name": "Deluxe"}, {"name": "Family Suite"}]
    live._photo_paths = []
    addbtn = FakeLocator(count=1, label="Add unit")
    page = FakePage([{"url": "http://x/overview", "heading": "Set up", "body": "units"}],
                    locators={"text:Add unit": addbtn})
    live.rt = FakeRuntime(page)
    assert live._try_progress_button() is True and addbtn.clicked is True


def test_existing_units_counted_on_rerun():
    """A re-run where 3 units already exist on the overview must NOT add a 4th."""
    live = LiveSession()
    live._unit_order = []                         # fresh run: nothing seen yet this run
    live._unit_target = 3
    live._room_list = [{}, {}, {}]
    live._photo_paths = []
    addbtn = FakeLocator(count=1, label="Add unit")
    body = ("standard room rooms of this type 6 deluxe rooms of this type 5 "
            "family suite rooms of this type 3")   # 3 units already present
    page = FakePage([{"url": "http://x/overview", "heading": "S", "body": body}],
                    locators={"text:Add unit": addbtn})
    live.rt = FakeRuntime(page)
    assert live._count_existing_units() == 3
    assert live._try_progress_button() is False    # already 3 → no 4th unit
    assert addbtn.clicked is False


def test_no_unit_cta_once_all_rooms_started():
    """Counted by unit_id: 3 distinct units started → never offer a 4th 'Add unit'."""
    live = LiveSession()
    live._unit_order = ["A", "B", "C"]            # 3 rooms already started
    live._unit_target = 3
    live._room_list = [{}, {}, {}]
    live._photo_paths = []
    addbtn = FakeLocator(count=1, label="Add unit")
    page = FakePage([{"url": "http://x/overview", "heading": "S", "body": "u"}],
                    locators={"text:Add unit": addbtn})
    live.rt = FakeRuntime(page)
    # units are done → 'Add unit' is NOT offered → it doesn't click it (would go to photos/stop)
    assert live._try_progress_button() is False
    assert addbtn.clicked is False
