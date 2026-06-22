"""Photo upload: find the (possibly hidden) file input and set_input_files from JSON."""
import os

from PIL import Image

from accounts_pilot.web.live import LiveSession
from tests.conftest import FakeLocator, FakePage, FakeRuntime

PAGE = [{"url": "http://x/photos", "heading": "Photos", "body": "add photos"}]


def test_photos_file_input_found():
    live = LiveSession()
    inp = FakeLocator(count=1)
    live.rt = FakeRuntime(FakePage(PAGE, locators={"input[type=file]": inp}))
    assert live._photos_file_input() is inp


def test_photos_file_input_absent():
    live = LiveSession()
    live.rt = FakeRuntime(FakePage(PAGE))
    assert live._photos_file_input() is None


def test_upload_photos_only_existing_files(tmp_path):
    live = LiveSession()
    live.rt = FakeRuntime(FakePage(PAGE))
    real = tmp_path / "exterior.jpg"
    Image.new("RGB", (1200, 900), (30, 80, 160)).save(str(real), "JPEG", quality=90)
    inp = FakeLocator(count=1)
    ok = live._upload_photos(inp, [str(real), str(tmp_path / "missing.jpg")])
    assert ok is True
    # the missing file is dropped; the real one is uploaded as a prepared (landscape/sized) JPEG
    assert inp.uploaded is not None and len(inp.uploaded) == 1
    assert os.path.exists(inp.uploaded[0]) and inp.uploaded[0].lower().endswith(".jpg")


def test_upload_photos_none_on_disk():
    live = LiveSession()
    live.rt = FakeRuntime(FakePage(PAGE))
    inp = FakeLocator(count=1)
    assert live._upload_photos(inp, ["C:/nope/missing.jpg"]) is False
    assert inp.uploaded is None
