"""Photo upload: find the (possibly hidden) file input and set_input_files from JSON."""
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
    real.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\xff\xd9")   # minimal jpeg bytes
    inp = FakeLocator(count=1)
    ok = live._upload_photos(inp, [str(real), str(tmp_path / "missing.jpg")])
    assert ok is True
    assert inp.uploaded == [str(real)]                          # missing one filtered out


def test_upload_photos_none_on_disk():
    live = LiveSession()
    live.rt = FakeRuntime(FakePage(PAGE))
    inp = FakeLocator(count=1)
    assert live._upload_photos(inp, ["C:/nope/missing.jpg"]) is False
    assert inp.uploaded is None
