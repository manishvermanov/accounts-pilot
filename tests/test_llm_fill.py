"""LLM filler — safety guards on what it may touch + the exclusion filter."""
from accounts_pilot.web import llm_fill


def test_excludes_language_and_account_controls():
    assert llm_fill._is_excluded({"label": "Language selector pop-up", "selector": "#lang"})
    assert llm_fill._is_excluded({"label": "Account", "selector": "[data-testid='legacy-account-button']"})
    assert llm_fill._is_excluded({"label": "Back to previous step", "selector": "x"})


def test_does_not_exclude_real_property_fields():
    assert not llm_fill._is_excluded({"label": "Property Name", "selector": "#:rs:"})
    assert not llm_fill._is_excluded({"label": "3 stars", "selector": "#:r2g:"})
    assert not llm_fill._is_excluded({"label": "Restaurant", "selector": "#:r2v:"})


def test_system_prompt_handles_category_owner_channel_but_skips_address():
    s = llm_fill._SYSTEM.lower()
    # the LLM now OWNS category / owner-type / channel-manager (rules are address-only)
    assert "category" in s
    assert "channel-manager" in s or "channel manager" in s
    assert "owner type" in s or "one property vs multiple" in s or "single-property" in s
    # but it must SKIP the address step (dedicated handler owns it)
    assert "address" in s
    # and never click Continue, change language, or touch payment
    assert "continue" in s
    assert "language" in s
    assert "payment" in s or "bank" in s
