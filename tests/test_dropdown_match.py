"""The dropdown picker must select ONLY a precise match — never a loose fragment.

These lock the exact failure modes seen in the field: 'Deluxe' must not grab
'Deluxe Suite', a wanted value must not match a shorter fragment, geography names
with a parenthetical suffix must still match, and accents/case must not matter.
"""
from accounts_pilot.web.live import dropdown_tier


def best(options, want):
    """Return the option a real pick would choose (lowest tier, first on a tie), or None."""
    ranked = sorted(
        ((dropdown_tier(o, want), i, o) for i, o in enumerate(options)),
        key=lambda r: (r[0], r[1]),
    )
    top = ranked[0]
    return top[2] if top[0] < 99 else None


def test_exact_beats_prefix():
    # 'Deluxe' must land on 'Deluxe', never on 'Deluxe Suite'
    assert best(["Deluxe Suite", "Deluxe", "Super Deluxe"], "Deluxe") == "Deluxe"


def test_super_deluxe_is_distinct():
    assert best(["Deluxe", "Super Deluxe"], "Super Deluxe") == "Super Deluxe"


def test_parenthetical_suffix_matches():
    assert best(["Chhattisgarh (CG)", "Chandigarh"], "Chhattisgarh") == "Chhattisgarh (CG)"


def test_accent_and_case_insensitive():
    assert best(["Bengalūru", "Mumbai"], "bengaluru") == "Bengalūru"


def test_no_fragment_match():
    # 'Goa' must NOT match 'Goat' or 'Algoa' — no loose substring
    assert best(["Goat Farm", "Algoa Bay"], "Goa") is None


def test_no_partial_word_match():
    # 'Raipur' must not match a truncated 'Rai'
    assert dropdown_tier("Rai", "Raipur") == 99


def test_city_does_not_grab_compound():
    # picking the city 'Raipur' must not select 'Raipur Junction' over the exact city
    assert best(["Raipur Junction", "Raipur"], "Raipur") == "Raipur"


def test_word_boundary_prefix_allowed():
    # a genuine boundary prefix is fine when there's no exact option
    assert best(["Chhattisgarh State"], "Chhattisgarh") == "Chhattisgarh State"


def test_unrelated_returns_none():
    assert best(["Kerala", "Punjab", "Goa"], "Maharashtra") is None
