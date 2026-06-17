"""Multi-OTA: independent sessions per OTA (Booking.com, MakeMyTrip, Agoda)."""
from accounts_pilot.adapters import REGISTRY, OTA_LABELS, get_adapter
from accounts_pilot.web.live import get_session


def test_all_otas_registered():
    for ota in ("booking_com", "makemytrip", "agoda"):
        assert ota in REGISTRY and ota in OTA_LABELS
    assert OTA_LABELS["makemytrip"] == "MakeMyTrip"
    assert OTA_LABELS["agoda"] == "Agoda"


def test_makemytrip_adapter_shape():
    a = get_adapter("makemytrip")
    assert a.ota == "makemytrip"
    assert a.display_name == "MakeMyTrip"
    assert any(s.key == "onboard" for s in a.steps())   # has the LLM-driven onboard step


def test_agoda_adapter_shape():
    a = get_adapter("agoda")
    assert a.ota == "agoda"
    assert a.display_name == "Agoda"
    assert any(s.key == "onboard" for s in a.steps())


def test_agoda_session_is_independent():
    a = get_session("agoda")
    assert a.ota == "agoda"
    assert a._pmap_path.name == "page_maps_agoda.json"
    assert get_session("agoda") is a                    # same OTA → same live session
    assert get_session("agoda") is not get_session("makemytrip")


def test_expedia_and_airbnb_registered():
    for ota, label in (("expedia", "Expedia"), ("airbnb", "Airbnb")):
        assert ota in REGISTRY and OTA_LABELS[ota] == label
        a = get_adapter(ota)
        assert a.ota == ota and a.display_name == label
        assert any(s.key == "onboard" for s in a.steps())


def test_all_five_sessions_independent_with_own_maps():
    otas = ["booking_com", "makemytrip", "agoda", "expedia", "airbnb"]
    sessions = [get_session(o) for o in otas]
    # every session is a distinct object with its own per-OTA maps file
    assert len({id(s) for s in sessions}) == len(otas)
    for o, s in zip(otas, sessions):
        assert s.ota == o
        assert s._pmap_path.name == f"page_maps_{o}.json"
        assert get_session(o) is s                       # stable per-OTA singleton


def test_sessions_are_independent_per_ota():
    b = get_session("booking_com")
    m = get_session("makemytrip")
    assert b is not m
    assert b.ota == "booking_com" and m.ota == "makemytrip"
    # separate cache + login-session files so they never collide
    assert b._pmap_path.name == "page_maps_booking_com.json"
    assert m._pmap_path.name == "page_maps_makemytrip.json"
    # same OTA returns the SAME session (so the UI polls one live browser)
    assert get_session("makemytrip") is m


def test_status_includes_ota():
    assert get_session("makemytrip").status()["ota"] == "makemytrip"
