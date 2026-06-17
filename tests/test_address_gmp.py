"""The address box is a Google <gmp-place-autocomplete> web component (closed shadow
root). We click the host element + keyboard-type, since the inner <input> is unreachable.
"""
from accounts_pilot.web.live import LiveSession
from tests.conftest import FakeLocator, FakePage, FakeRuntime

PAGE = [{"url": "http://x/address", "heading": "Where is your property?",
         "body": "where is your property?"}]


def test_gmp_autocomplete_keyboard_select(monkeypatch):
    live = LiveSession()
    gmp = FakeLocator(count=1)
    page = FakePage(PAGE, locators={"gmp-place-autocomplete": gmp})
    live.rt = FakeRuntime(page)
    # the dropdown is in the closed shadow root → selection is verified, not DOM-clicked
    monkeypatch.setattr(live, "_gmp_committed", lambda: True)

    ok = live._fill_gmp_autocomplete(["Mall Road, Manali, Himachal Pradesh 175131"], "Manali")
    assert ok is True
    assert gmp.clicked is True
    assert any("Manali" in t for t in page.keyboard.typed)
    assert "ArrowDown" in page.keyboard.pressed and "Enter" in page.keyboard.pressed


def test_gmp_autocomplete_fails_if_no_place_selected(monkeypatch):
    """If no place commits (value stays empty), it must NOT report success."""
    live = LiveSession()
    gmp = FakeLocator(count=1)
    page = FakePage(PAGE, locators={"gmp-place-autocomplete": gmp})
    live.rt = FakeRuntime(page)
    monkeypatch.setattr(live, "_gmp_committed", lambda: False)
    assert live._fill_gmp_autocomplete(["Manali"], "Manali") is False


def test_gmp_autocomplete_absent_returns_false():
    live = LiveSession()
    live.rt = FakeRuntime(FakePage(PAGE))            # no gmp element registered
    assert live._fill_gmp_autocomplete(["Manali"], "Manali") is False
