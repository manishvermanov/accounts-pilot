"""Shared test fakes — let us drive the LiveSession wizard-walker and the runtime
without a real browser or network. No Playwright, no Azure, no Booking.com.
"""
import json
from pathlib import Path

import pytest

DEMO = Path(__file__).parent.parent / "examples" / "booking_engine" / "DEMO-HOTEL-01.json"


def demo_profile_dict() -> dict:
    return json.loads(DEMO.read_text(encoding="utf-8"))


class FakeLocator:
    """Stand-in for a Playwright Locator. Records the action taken so tests can assert."""

    def __init__(self, *, count=1, label="", value="", visible=True, editable=True,
                 tag="input", checked=False):
        self._count = count
        self._label = label
        self._value = value
        self._visible = visible
        self._editable = editable
        self._tag = tag
        self._checked = checked
        self.clicked = False
        self.filled = None
        self.checked_called = False
        self.selected = None
        self.pressed = []
        self.uploaded = None

    @property
    def first(self):
        return self

    def nth(self, _i):
        return self

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def is_editable(self):
        return self._editable

    def is_checked(self):
        return self._checked

    def click(self, **kw):
        self.clicked = True

    def fill(self, v, **kw):
        self.filled = v
        self._value = v

    def check(self, **kw):
        self.checked_called = True
        self._checked = True

    def select_option(self, *a, **kw):
        self.selected = kw or a

    def scroll_into_view_if_needed(self, **kw):
        pass

    def set_input_files(self, files, **kw):
        self.uploaded = files if isinstance(files, list) else [files]

    def press(self, k, **kw):
        self.pressed.append(k)

    def dispatch_event(self, _e):
        pass

    def inner_text(self):
        return self._label

    def input_value(self):
        return self._value

    def get_attribute(self, name):
        return {"placeholder": self._label, "aria-label": self._label}.get(name, "")

    def evaluate(self, js):
        # _is_input / tag checks ask for tagName; everything else wants the label
        if "tagName" in js:
            return self._tag
        return self._label

    def bounding_box(self):
        return {"x": 0, "y": 0, "width": 400, "height": 300}


class FakeKeyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    def type(self, text, **kw):
        self.typed.append(text)

    def press(self, key, **kw):
        self.pressed.append(key)


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def click(self, x, y, **kw):
        self.clicks.append((x, y))


class FakePage:
    """A scripted multi-page wizard. `script` is a list of {url, heading, body}."""

    def __init__(self, script, *, locators=None):
        self.script = script
        self.idx = 0
        self.main_frame = self
        self._locators = locators or {}     # css/text -> FakeLocator
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()

    @property
    def url(self):
        return self.script[self.idx]["url"]

    @property
    def frames(self):
        return [self]

    def inner_text(self, _sel):
        return self.script[self.idx].get("body", "")

    def wait_for_load_state(self, *a, **k):
        pass

    def evaluate(self, js, *a):
        if "querySelector" in js and ("heading" in js or "h1" in js):
            return self.script[self.idx]["heading"]
        return []

    def _resolve(self, key):
        return self._locators.get(key, FakeLocator(count=0))

    def locator(self, sel):
        return self._resolve(sel)

    def get_by_label(self, text, *a, **k):
        return self._resolve(f"label:{text}")

    def get_by_placeholder(self, text, *a, **k):
        return self._resolve(f"ph:{text}")

    def get_by_text(self, text, *a, **k):
        return self._resolve(f"text:{text}")

    def get_by_role(self, role, *a, **k):
        return self._resolve(f"role:{role}")


class FakeRuntime:
    """Drives the fake wizard. try_advance() moves to the next scripted page."""

    def __init__(self, page):
        self.page = page
        self.advances = 0

    def think(self, *a, **k):
        pass

    def detect_challenge(self):
        return None

    def try_advance(self):
        if self.page.idx < len(self.page.script) - 1:
            self.page.idx += 1
            self.advances += 1
            return True
        return False


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Point settings.db_path at a temp dir so page_maps.json lands in isolation."""
    from accounts_pilot.config import settings
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ap.db"), raising=False)
    return tmp_path


@pytest.fixture
def llm_enabled(monkeypatch):
    """Make LiveSession think an LLM is configured, without any real Azure call."""
    from accounts_pilot.config import settings
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://fake/openai/v1", raising=False)
    monkeypatch.setattr(settings, "azure_openai_key", "fake-key", raising=False)
    monkeypatch.setattr(settings, "azure_openai_deployment", "fake-deploy", raising=False)
