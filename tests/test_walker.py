"""The autonomous wizard-walker: LLM-first, learn-once-then-replay, gate-aware.

These tests drive LiveSession._do_fill against a scripted fake wizard (no browser,
no network), proving the behaviour the operator asked for:
  1. the LLM maps each new page ONCE, and its map is stored;
  2. a second run REPLAYS the stored map with zero LLM calls;
  3. the walk stops at a human-only bank/contract gate (never auto-fills money/legal).
"""
import json
import threading
import time

from accounts_pilot.web import llm_fill as llm_mod
from accounts_pilot.web.live import LiveSession
from tests.conftest import FakePage, FakeRuntime, demo_profile_dict


def _walk(live, profile, timeout=5.0):
    """Run the (blocking) walker in a thread and auto-press 'Done' at any pause gate.
    The bank/payout gate waits for the operator to type the account number — in a test
    there's no operator, so we resolve the wait so the walk can finish (and then stop)."""
    t = threading.Thread(target=live._do_fill, args=(profile,), daemon=True)
    t.start()
    end = time.time() + timeout
    while t.is_alive() and time.time() < end:
        if live.state in ("awaiting_captcha", "awaiting_otp"):
            live._captcha.set()                  # simulate the operator clicking 'Done'
        time.sleep(0.01)
    t.join(timeout=1.0)
    return t


WIZARD = [
    {"url": "http://book/category", "heading": "Choose your category", "body": "choose a category"},
    {"url": "http://book/name", "heading": "Tell us about your hotel", "body": "what is the name of your hotel"},
    {"url": "http://book/bank", "heading": "Your bank details", "body": "add your bank account and iban for payout"},
]


def _wire(monkeypatch, live, counter):
    """Stub the LLM + the stable-locator step so the walk is deterministic."""
    monkeypatch.setattr(llm_mod, "scrape_fields", lambda page: [])

    def fake_map(fields, profile, autopilot=False):
        counter["calls"] += 1
        return [{"selector": "#:rs:", "action": "fill", "value": "Maple Ridge Inn"}]

    monkeypatch.setattr(llm_mod, "map_actions", fake_map)
    # turn the throwaway #:rs: into a stable label so the stored map is real
    monkeypatch.setattr(live, "_stable_descriptor",
                        lambda sel: ("label", "Property Name"))


def test_llm_runs_once_then_replays_without_llm(tmp_data_dir, llm_enabled, monkeypatch):
    counter = {"calls": 0}
    live = LiveSession()
    _wire(monkeypatch, live, counter)

    # ---- run 1: learns the two fillable pages, pauses at the bank gate ----
    live.rt = FakeRuntime(FakePage([dict(p) for p in WIZARD]))
    _walk(live, demo_profile_dict())

    assert counter["calls"] == 2, "LLM should map each of the 2 non-gate pages exactly once"
    log1 = "\n".join(live.log)
    assert "learned this page" in log1
    assert "bank" in log1.lower() and "stopping" in log1.lower()

    # the maps were persisted to the per-OTA cache file (page_maps_<ota>.json)
    pmap_file = live._pmap_path
    assert pmap_file.exists()
    stored = json.loads(pmap_file.read_text(encoding="utf-8"))
    assert len(stored) == 2
    # stored entries use the STABLE label locator, not the volatile #:rs: id
    for entries in stored.values():
        assert entries and entries[0]["by"] == "label"
        assert ":rs:" not in json.dumps(entries)

    # ---- run 2: same property → replay from disk, ZERO LLM calls ----
    counter["calls"] = 0
    live.log = []
    live.rt = FakeRuntime(FakePage([dict(p) for p in WIZARD]))
    _walk(live, demo_profile_dict())

    assert counter["calls"] == 0, "second run must replay the learned map with no LLM"
    log2 = "\n".join(live.log)
    assert "replayed learned map" in log2
    assert live.state == "connected"


def test_walk_stops_at_bank_gate_before_filling(tmp_data_dir, llm_enabled, monkeypatch):
    counter = {"calls": 0}
    live = LiveSession()
    _wire(monkeypatch, live, counter)
    live.rt = FakeRuntime(FakePage([dict(p) for p in WIZARD]))
    _walk(live, demo_profile_dict())

    log = "\n".join(live.log)
    assert "bank / contract / payment" in log.lower() or "bank" in log.lower()
    # it never advanced PAST the gate page (index 2 is the last it reaches)
    assert live.rt.page.idx == 2


def test_stuck_page_is_not_cached(tmp_data_dir, llm_enabled, monkeypatch):
    """A page that the LLM maps but that never advances must NOT be saved — otherwise
    a one-off failure would poison the cache and repeat forever."""
    counter = {"calls": 0}
    live = LiveSession()
    _wire(monkeypatch, live, counter)
    # single page that can't advance (try_advance returns False on the last page)
    one = [{"url": "http://book/category", "heading": "Choose your category", "body": "choose a category"}]
    live.rt = FakeRuntime(FakePage(one))
    live._do_fill(demo_profile_dict())

    log = "\n".join(live.log)
    assert ("No ‘Continue’" in log or "No 'Continue'" in log or "stopping" in log.lower()
            or "won't advance" in log.lower() or "stuck" in log.lower())
    # nothing should have been persisted (the page never advanced)
    pmap_file = live._pmap_path
    if pmap_file.exists():
        assert json.loads(pmap_file.read_text(encoding="utf-8")) == {}
    assert "learned this page" not in log


def test_no_llm_key_warns_and_still_runs(tmp_data_dir, monkeypatch):
    """With no Azure key, the walker warns but doesn't crash (stable rules only)."""
    from accounts_pilot.config import settings
    monkeypatch.setattr(settings, "azure_openai_endpoint", "", raising=False)
    monkeypatch.setattr(settings, "azure_openai_key", "", raising=False)
    monkeypatch.setattr(settings, "azure_openai_deployment", "", raising=False)
    live = LiveSession()
    live.rt = FakeRuntime(FakePage([dict(p) for p in WIZARD]))
    _walk(live, demo_profile_dict())
    assert "No LLM key configured" in "\n".join(live.log)
    assert live.state == "connected"
