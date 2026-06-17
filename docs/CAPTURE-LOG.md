# Selector-Capture Log — Booking.com

Live walk of `join.booking.com` to capture real DOM selectors for the adapter.
Captured selectors live in `accounts_pilot/adapters/booking_selectors.py`.

---

## Session 1 — 2026-06-08 (pre-auth screens)

**Tooling:** gstack `/browse` (headless Chromium), read-only (no input, no submit).

### Flow discovered
```
join.booking.com (marketing landing)
   │  "Get started now"
   ▼
account.booking.com/register?op_token=…   ← "Create your partner account" (EMAIL ONLY)
   │  email → password → email/phone verification
   ▼
admin.booking.com/… property questionnaire   ← the AUTO wizard lives here (behind auth)
```

### Captured — account gate ✅
| Field | Selector | Notes |
|---|---|---|
| Email | `#login_name_register` | `input[type=email][name=login_name_register]`, `data-ga-label="username"` |
| Continue | `form button[type='submit']` | CSS classes are build-hashed → scope by form+type or text "Continue" |
| Form action | `account.booking.com/register` (GET, carries `op_token`) | per-attempt OAuth token |

Screenshot: `Create your partner account` — single email field + Continue + Sign in.

### Key findings
1. **Account creation is the hard first gate.** Confirmed live: the property
   questionnaire is entirely behind email → password → verification. Matches the
   adapter's `account` GATE step.
2. **`op_token` per attempt.** No deep-linking into wizard steps; must flow through a
   logged-in session.
3. **CSS classes are hashed** (`YyPS4CCyBc09wPIEDhf6`…) and will change between builds.
   **Rule: select on `id` / `name` / `data-*` / role+text only.** This confirms the
   ARCHITECTURE.md guidance.

### Boundary hit (expected)
Everything past the account page requires creating an account + setting a password +
clearing email/phone verification (and likely a bot/CAPTCHA challenge). Those are
operator actions, not engine actions. **Capture stopped at the auth wall by design.**

---

## Next session — authenticated wizard (operator-assisted)

To capture the ~12 steps behind auth (`scope → … → submit`), the operator drives the
login, then the engine reads the DOM of the logged-in wizard:

1. **Operator** creates the Booking.com partner account + completes verification in a
   real browser (the human-only gate).
2. Import that session into `/browse`:
   ```bash
   browse cookie-import-browser chrome --domain admin.booking.com
   ```
   (or `/setup-browser-cookies`), so the headless session is authenticated.
3. **Engine** walks `admin.booking.com` wizard step-by-step, capturing selectors for
   each AUTO field into `booking_selectors.py` (status CAPTURED).
4. Wire each captured selector into the matching `_step_*` handler in
   `adapters/booking_com.py` (replace the `# TODO(selector)` lines).

Until step 1 happens, `pending_steps()` in `booking_selectors.py` lists what remains.
