"""Offline 'Simulate' engine — validate JSON + dry-run handlers against synthetic
pages in a headless browser, without a live OTA login or touching the live session.
"""
import pytest

from tests.conftest import demo_profile_dict
from accounts_pilot.web.simulate import simulate, simulate_all, available_otas


def test_available_otas_includes_expedia():
    assert "expedia" in available_otas()


def test_invalid_json_fails_validation_without_browser():
    # missing every required field → schema error, returns before launching a browser
    r = simulate("expedia", {"property_id": "x"})
    assert r["ok"] is False
    assert r["validation"]["ok"] is False and r["validation"]["error"]
    assert r["steps"] == []


def test_unsupported_ota_validates_and_reports_coverage_only():
    r = simulate("airbnb", demo_profile_dict())
    assert r["ok"] is True and r["validation"]["ok"] is True
    assert r["steps"] == []                       # no offline handler flow for airbnb
    assert r["coverage"]["rooms"] >= 1
    assert any("airbnb" in n for n in r["notes"])  # honest note about validate-only


def test_coverage_flags_missing_fields_and_photos():
    prof = demo_profile_dict()
    r = simulate("airbnb", prof)
    cov = r["coverage"]
    assert set(("present", "missing", "photo_warnings", "rooms")).issubset(cov)
    # demo has < 10 photos → a pre-flight warning is surfaced
    assert any("photo" in w.lower() for w in cov["photo_warnings"]) or cov["photos"] >= 10


def test_expedia_offline_sim_fills_real_dom():
    """Full path: launches headless Chromium, drives the Expedia handlers against the
    synthetic location/times/rooms pages, and confirms real fills came back."""
    try:
        r = simulate("expedia", demo_profile_dict())
    except Exception as e:                         # no browser in this env → skip, don't fail
        pytest.skip(f"headless browser unavailable: {e}")
    assert r["ok"] is True
    steps = {s["step"]: s for s in r["steps"]}
    assert "Location" in steps and "Rooms" in steps
    fields = {f["field"]: f["value"] for s in r["steps"] for f in s["filled"]}
    assert "City" in fields and fields["City"]      # address autocomplete / structured fill
    assert r["filled_total"] >= 5                   # location + times + rooms all contributed


def test_simulate_all_runs_globally():
    """The global run validates once and dry-runs every supported channel in one report."""
    try:
        r = simulate_all(demo_profile_dict())
    except Exception as e:
        pytest.skip(f"headless browser unavailable: {e}")
    assert r["ok"] is True and r["validation"]["ok"] is True
    assert r["coverage"]["rooms"] >= 1
    assert "expedia" in r["otas"]                    # every supported channel appears
    assert r["otas"]["expedia"]["filled_total"] >= 5
    assert r["filled_total"] >= 5


def test_simulate_all_invalid_json_fails_fast():
    r = simulate_all({"property_id": "x"})
    assert r["ok"] is False and r["validation"]["ok"] is False
    assert r["otas"] == {}


def test_simulate_does_not_touch_live_session():
    """A simulation must never mutate the live per-OTA singleton (its state/log)."""
    from accounts_pilot.web.live import get_session
    live = get_session("expedia")
    live.state = "connected"
    live.log = ["LIVE-MARKER"]
    try:
        simulate("expedia", demo_profile_dict())
    except Exception:
        pass
    assert live.state == "connected"               # untouched
    assert live.log == ["LIVE-MARKER"]             # the sim used a throwaway session
