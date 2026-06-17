import json
from pathlib import Path

from accounts_pilot.adapters import get_adapter
from accounts_pilot.adapters.base import StepKind
from accounts_pilot.models.job import GateKind
from accounts_pilot.models.property_profile import PropertyProfile

SAMPLE = Path(__file__).parent.parent / "examples" / "sample_property.json"


def test_sample_profile_validates():
    profile = PropertyProfile.model_validate(json.loads(SAMPLE.read_text(encoding="utf-8")))
    assert profile.display_name == "The Riverside Inn"
    assert profile.total_rooms == 24
    assert len(profile.room_types) == 2


def test_gstin_length_enforced():
    import pytest
    from pydantic import ValidationError

    bad = json.loads(SAMPLE.read_text(encoding="utf-8"))
    bad["compliance"]["gstin"] = "TOO-SHORT"
    with pytest.raises(ValidationError):
        PropertyProfile.model_validate(bad)


def test_booking_step_graph():
    adapter = get_adapter("booking_com")
    steps = adapter.steps()
    keys = [s.key for s in steps]
    # the four human-only gates exist and are GATE-kind
    gate_kinds = {s.key: s.gate for s in steps if s.kind is StepKind.GATE}
    assert GateKind.ACCOUNT in gate_kinds.values()
    assert GateKind.BANK in gate_kinds.values()
    assert GateKind.CONTRACT in gate_kinds.values()
    assert GateKind.OTP in gate_kinds.values()
    # submit is the terminal system step
    assert keys[-1] == "submit"


def test_next_step_walks_graph():
    adapter = get_adapter("booking_com")
    first = adapter.next_step(None)
    assert first.key == "account"
    after_account = adapter.next_step("account")
    assert after_account.key == "verify"
