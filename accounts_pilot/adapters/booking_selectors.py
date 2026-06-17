"""Captured Booking.com selectors.

Status legend:
  CAPTURED          — verified against the live DOM (date noted)
  TODO_BEHIND_AUTH  — behind account creation + verification; needs operator-assisted capture

CAPTURE NOTES (live walk 2026-06-08):
  - Flow: join.booking.com (marketing) → "Get started now" →
    account.booking.com/register → "Create your partner account" (EMAIL ONLY) →
    password → email/phone verification → admin.booking.com property questionnaire.
  - The account page is the HARD FIRST GATE. The whole property wizard (type, name,
    stars, rooms, rates, facilities, photos, policies, bank, contract) is behind it.
  - The register URL carries an `op_token` (per-attempt OAuth token). You cannot
    deep-link into a wizard step; you must flow through from a logged-in session.
  - ⚠️ CSS classes are BUILD-HASHED (e.g. `YyPS4CCyBc09wPIEDhf6`) and WILL change.
    Use stable ids / name / data-* / role+text. Never select on the hashed classes.
"""
from __future__ import annotations

SELECTORS: dict[str, dict] = {
    # ---- CAPTURED: account creation gate (account.booking.com/register) ----
    "account": {
        "_status": "CAPTURED",
        "_captured_on": "2026-06-08",
        "_url": "https://account.booking.com/register",
        "email": "#login_name_register",                 # input[type=email][name=login_name_register]
        "email_stable_alt": "[data-ga-label='username']",
        "continue": "form button[type='submit']",         # classes hashed → scope by form+type, or text "Continue"
        "_note": "HUMAN-ONLY gate. Engine navigates here and parks; it does not enter the email/password.",
    },

    # ---- everything below requires a logged-in session (operator-assisted capture) ----
    "verify":     {"_status": "TODO_BEHIND_AUTH", "_note": "email/phone OTP after account creation"},
    "scope":      {"_status": "TODO_BEHIND_AUTH", "_note": "single property vs group"},
    "prop_type":  {"_status": "TODO_BEHIND_AUTH", "_note": "property category picker"},
    "details":    {"_status": "TODO_BEHIND_AUTH", "_note": "name, star rating, setting"},
    "location":   {"_status": "TODO_BEHIND_AUTH", "_note": "address + map pin"},
    "rooms":      {"_status": "TODO_BEHIND_AUTH"},
    "rates":      {"_status": "TODO_BEHIND_AUTH"},
    "facilities": {"_status": "TODO_BEHIND_AUTH"},
    "photos":     {"_status": "TODO_BEHIND_AUTH"},
    "policies":   {"_status": "TODO_BEHIND_AUTH"},
    "contact":    {"_status": "TODO_BEHIND_AUTH"},
    "tax":        {"_status": "TODO_BEHIND_AUTH"},
    "bank":       {"_status": "TODO_BEHIND_AUTH", "_note": "HUMAN-ONLY"},
    "contract":   {"_status": "TODO_BEHIND_AUTH", "_note": "HUMAN-ONLY"},
    "submit":     {"_status": "TODO_BEHIND_AUTH"},
}


def captured_steps() -> list[str]:
    return [k for k, v in SELECTORS.items() if v.get("_status") == "CAPTURED"]


def pending_steps() -> list[str]:
    return [k for k, v in SELECTORS.items() if v.get("_status") == "TODO_BEHIND_AUTH"]
