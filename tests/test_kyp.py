"""Partner verification (Know Your Partner) — deterministic fill via stable name= attrs."""
from accounts_pilot.web.live import LiveSession
from tests.conftest import FakeLocator, FakePage, FakeRuntime


def test_fill_kyp_individual_and_dob_format():
    live = LiveSession()
    sel = FakeLocator(count=1, tag="select")
    fn, ln, dob = FakeLocator(count=1), FakeLocator(count=1), FakeLocator(count=1)
    page = FakePage(
        [{"url": "http://x/know-your-partner.html", "heading": "Partner verification", "body": "b"}],
        locators={
            "select[name='owner_type']": sel,
            "input[name='first_name_of_owners']": fn,
            "input[name='last_name_of_owners']": ln,
            "input[name='dob_of_owners']": dob,
        })
    live.rt = FakeRuntime(page)
    pdata = {"compliance": {"owner_first_name": "Manish", "owner_last_name": "Verma",
                            "owner_dob": "1990-01-15", "legal_entity_name": "Maple Ridge Hospitality Pvt Ltd"},
             "contact": {"full_name": "Manish Verma"}}
    assert live._fill_kyp(pdata) is True
    assert sel.selected                              # owner_type was set (individual)
    assert fn.filled == "Manish" and ln.filled == "Verma"
    assert dob.filled == "1990-01-15"                # yyyy-mm-dd for the date input


def test_norm_date_handles_any_format():
    live = LiveSession()
    assert live._norm_date("15-01-1990") == "1990-01-15"     # dd-mm-yyyy
    assert live._norm_date("1990-01-15") == "1990-01-15"     # already yyyy-mm-dd
    assert live._norm_date("15/01/1990") == "1990-01-15"     # slashes
    assert live._norm_date("5-1-1990") == "1990-01-05"       # single digits padded


def test_fill_kyp_normalizes_ddmmyyyy_dob():
    live = LiveSession()
    sel = FakeLocator(count=1, tag="select")
    fn, ln, dob = FakeLocator(count=1), FakeLocator(count=1), FakeLocator(count=1)
    page = FakePage(
        [{"url": "http://x/know-your-partner.html", "heading": "Partner verification", "body": "b"}],
        locators={"select[name='owner_type']": sel,
                  "input[name='first_name_of_owners']": fn,
                  "input[name='last_name_of_owners']": ln,
                  "input[name='dob_of_owners']": dob})
    live.rt = FakeRuntime(page)
    pdata = {"compliance": {"owner_first_name": "Manish", "owner_last_name": "Verma",
                            "owner_dob": "15-01-1990"},          # dd-mm-yyyy in the JSON
             "contact": {"full_name": "Manish Verma"}}
    assert live._fill_kyp(pdata) is True
    assert dob.filled == "1990-01-15"                          # normalized for type=date


def test_fill_kyp_splits_full_name_when_owner_fields_absent():
    live = LiveSession()
    sel = FakeLocator(count=1, tag="select")
    fn, ln, dob = FakeLocator(count=1), FakeLocator(count=1), FakeLocator(count=1)
    page = FakePage(
        [{"url": "http://x/know-your-partner.html", "heading": "Partner verification", "body": "b"}],
        locators={"select[name='owner_type']": sel,
                  "input[name='first_name_of_owners']": fn,
                  "input[name='last_name_of_owners']": ln,
                  "input[name='dob_of_owners']": dob})
    live.rt = FakeRuntime(page)
    pdata = {"compliance": {"owner_dob": "1990-01-15"}, "contact": {"full_name": "Manish Verma"}}
    assert live._fill_kyp(pdata) is True
    assert fn.filled == "Manish" and ln.filled == "Verma"   # split from full_name
