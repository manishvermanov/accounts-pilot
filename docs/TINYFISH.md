# TinyFish Driver

An optional **AI fill driver**: instead of capturing per-OTA selectors, turn the
`PropertyProfile` into plain-English goals and let TinyFish (cloud web-agent) drive
the wizard — it adapts when the OTA reskins its pages, so no selector maintenance.

## What it does / doesn't

| Does (AUTO steps) | Does NOT (gates — owner-handled) |
|---|---|
| property type, name, stars, setting | account creation |
| address + map pin | CAPTCHA (TinyFish only *reduces* it; pairs a solver) |
| rooms, beds, occupancy, rates | email/phone OTP |
| facilities, photos, policies | bank / payout |
| contact, tax/GST | partner contract |

TinyFish's stealth lowers how often a CAPTCHA fires; by its own docs it can't reliably
*solve* hard ones (it pairs with a solver). So gates stay with the owner in the
assisted-onboarding model.

## Honest architecture note
TinyFish runs the browser **on its cloud**, not in the owner's local browser. So to fill
a Booking wizard it needs to be in the property's session — either you pass it the
account (→ back to the account/CAPTCHA gate on TinyFish's browser) or you transfer the
owner's session. It does **not** remove the gates; it removes the *selector grind* for
the data-fill. Use it as the fill engine, keep the gates owner-driven.

## Usage

```bash
# 1. see the goals it will run (no key needed)
python -m accounts_pilot.cli tinyfish --profile examples/test_property_full.json

# 2. actually run them (needs a key)
#    .env →  TINYFISH_API_KEY=sk-tinyfish-...
python -m accounts_pilot.cli tinyfish --profile examples/test_property_full.json --run
```

## API (verified 2026-06-09)
`POST https://agent.tinyfish.ai/v1/automation/run` · header `X-API-Key: <key>` ·
body `{url, goal, browser_profile}`. Sync run returns
`{run_id, status, num_of_steps, result:{result}, error}`. (`/run-sse` streams.)
Confirmed working against a neutral page. Docs: https://docs.tinyfish.ai

Code: `accounts_pilot/drivers/tinyfish.py` — `TinyFishDriver` (client) + `booking_goals(profile)`
(the profile→English mapping, reusable for any NL web agent).
