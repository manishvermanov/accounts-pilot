"""Booking rule set — now ADDRESS-ONLY.

The address (Google-Places autocomplete + map pin) is the one control the LLM can't
do, so it's the only deterministic rule. Everything else — property category,
single/multiple, channel-manager, name, stars, amenities, yes/no — is owned by the
LLM, which reads the actual page (Booking ships several category/owner layouts that
hard-coded selectors couldn't all match).
"""
from accounts_pilot.models.property_profile import PropertyProfile
from accounts_pilot.web import booking_rules
from tests.conftest import demo_profile_dict


def _profile():
    return PropertyProfile.model_validate(demo_profile_dict())


def test_rules_are_address_only():
    rules = booking_rules.rules_for(_profile())
    assert [r[0] for r in rules] == ["address"]
    # brittle / variant-fragile selector rules must be GONE (LLM owns them now)
    flat = repr(rules)
    for gone in ("automation_id_property_type", "choose_owner_type",
                 "channel manager", "Property Name", "stars", "Restaurant"):
        assert gone not in flat, f"{gone!r} should not be a rule anymore"


def test_address_rule_carries_fields_and_candidates_from_json():
    rules = booking_rules.rules_for(_profile())
    kind, _, action, payload = rules[0]
    assert kind == "address" and action == "address"
    assert payload["fields"]["city"] == "Manali"
    assert payload["fields"]["country"] == "India"        # IN normalised to India
    assert payload["fields"]["postal"] == "175131"
    assert any("Manali" in c for c in payload["candidates"])


def test_amenity_word_boundary_minibar_is_not_bar():
    """Regression: 'minibar' must NOT tick the 'Bar' amenity."""
    labels = booking_rules.amenity_labels(_profile())
    assert "Bar" not in labels                # DEMO has minibar, not a bar
    assert "Restaurant" in labels             # restaurant: true
    assert "Non-smoking rooms" in labels      # smoking_allowed: false
