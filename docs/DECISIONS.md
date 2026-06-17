# Accounts Pilot — Decision Records (ADRs)

Each record: the decision, the alternatives, why we chose, and where the alternative
actually wins (so future-you can revisit honestly).

---

## ADR-001 — Python for the automation worker

**Decision:** Python 3.11+.

**Alternatives:** Node/TypeScript, Go, Java/C#.

**Why Python:** The stealth strategy is **CloakBrowser**, which is Python-only
(`from cloakbrowser import launch`). That single fact decides the language — Node forfeits
the stealth drop-in.

**Where the alternative wins:** If we used a *managed* stealth browser (ADR-003) instead of
self-hosted CloakBrowser, **Node/TS would be equal-or-better** and would match a JS backend.
This decision is downstream of ADR-003 — revisit them together.

---

## ADR-002 — Playwright over Selenium

**Decision:** Playwright (sync API) as the browser driver.

**Alternatives:** Selenium, Puppeteer, playwright-stealth.

**Why Playwright:**
- **Auto-waiting** — far less flaky on async, SPA-ish signup wizards.
- **Network interception** — catch OTP redirects and CAPTCHA sitekeys.
- **CloakBrowser is a Playwright drop-in** — swapping to stealth is a one-line `launch()` change. Selenium can't do this, full stop.

**Why not Selenium:** Detectable base (`navigator.webdriver`), manual waits, and — fatally —
**not a CloakBrowser drop-in**, so the stealth swap would mean a driver rewrite.

**Where the alternative wins:** Selenium's only edge is familiarity/community size. Irrelevant
against the stealth-swap requirement.

---

## ADR-003 — Self-hosted CloakBrowser over a managed browser service

**Decision:** Self-hosted CloakBrowser for stealth, swapped in per-step.

**Alternatives:** Browserbase, Bright Data Scraping Browser, ZenRows, Browserless (managed
stealth browser + proxies + CAPTCHA as a service).

**Why self-hosted:**
1. **PII/credential exposure.** We type bank details and create accounts. A managed service
   **sees every keystroke** of those sessions — unacceptable for credentialed onboarding.
2. **Cost at volume.** Per-session pricing is cheap at 10 properties, brutal at 1,000.
3. **Control.** When Booking.com reskins the wizard we patch our adapter, not wait on a vendor.

**Where the alternative wins (honestly):** Managed services collapse stealth + proxy + CAPTCHA +
the "run a real Chromium" infra problem into one bill — **fastest path to a demo.** Legitimate
sequencing: prototype on Browserbase, migrate to self-hosted when PII/volume kick in. We chose
self-hosted from the start because the bank/credential exposure is present from day one.

---

## ADR-004 — SQLite + framework-free loop now; Celery/Temporal later

**Decision:** v1 persists jobs in stdlib SQLite with a plain run/resume loop. The loop is
written framework-free so it can later be wrapped without rewriting the stepping logic.

**Alternatives:** Temporal, Celery+Redis, Airflow/Prefect, AWS Step Functions, raw cron.

**Why staged:**
- v1 needs **zero infra** to run locally and be inspected. SQLite is transparent and file-based.
- The jobs are genuinely long-running with human pauses → the **end-state is Temporal**
  (durable execution + `await signal` for human-in-loop). Celery+Redis is the lighter middle.
- Airflow/Prefect are batch-DAG tools — **wrong shape** for human-paused workflows.
  Step Functions is vendor-locked and awkward with long human waits + browser workers.

**Migration trigger:** when hands-off scale or multi-worker concurrency matters, wrap `run_job`
in Temporal (task = workflow, each gate = a signal). The core stepping code does not change.

---

## ADR-005 — Bank/payout details are NOT modeled in the Property Profile

**Decision:** The canonical Profile deliberately omits any bank/payout fields. Those steps are
human-only gates (`HUMAN_ONLY` in the gate handler).

**Why:** Defense in depth. If the data isn't in the model, the engine *cannot* auto-fill or
auto-submit it, even by mistake. Financial credentials never sit in the profile JSON, the DB,
or the repo. The human enters them directly into the OTA.

**Consequence:** "fully automated onboarding" is intentionally impossible — and that's the
correct posture for credentialed, contractual, financial steps.

---

## ADR-006 — CAPTCHA provider: accuracy over cost

**Decision:** Wire **2Captcha or CapSolver** as the default solver; keep AZcaptcha as a cheap
fallback behind the same interface.

**Why:** A failed solve wastes a whole onboarding session (re-login, re-fill). The expensive
thing is the *burned session*, not the per-solve fee, so optimize for **accuracy on hard
reCAPTCHA/Turnstile**, where budget solvers underperform.

**Note:** Self-hosted CloakBrowser aims to *avoid triggering* challenges at all; the solver is
the fallback for when one is served anyway. Stealth first, solve second.

---

## ADR-008 — Humanised behaviour is always-on, not optional

**Decision:** Every interaction primitive (click/fill/select/check/scroll) routes through a
`HumanActor` by default — curved Bézier mouse paths, dwell-before-click, character-by-character
typing, `think()` pauses. `humanize=true` is the default; `false` exists only for fast local tests.

**Why:** Anti-bot defence on OTA signups is **two layers**: fingerprint *and* behaviour.
CloakBrowser (ADR-003) only covers fingerprint. A teleporting cursor and instant form-fill are a
behavioural tell that DataDome/PerimeterX/reCAPTCHA risk scoring catches even when the fingerprint
is perfect. Shipping fingerprint-stealth without behaviour-stealth would still get flagged — so
behaviour humanisation is a default, not an add-on.

**Alternatives considered:**
- *Playwright's built-in `click`/`fill`* — teleport + instant; the exact tell we're avoiding.
- *`slow_mo`* — adds a uniform delay but still teleports the cursor; uniform timing is itself a tell.
- *ghost-cursor / pyclick libs* — good prior art; we implement a small Bézier mover in-house to
  avoid a dependency and keep timing tunable from config.

**Where the alternative wins:** For non-hostile internal pages, humanisation just slows things down
— hence the `humanize=false` escape hatch for tests. Against real OTAs it stays on.

**Limits (honest):** This raises the behavioural bar; it is not a guarantee. Truly adversarial
risk engines also weigh IP reputation (→ residential proxy), session history, and device
attestation. Humanisation + fingerprint stealth + a clean proxy + sane rate limits is the full
posture; mouse movement alone is necessary, not sufficient.

---

## ADR-007 — Human-in-the-loop is a feature, not a gap

**Decision:** Account, OTP, bank, and contract are first-class **gates** with their own job
states, not edge cases to be eliminated.

**Why:** These steps are credential creation, identity verification, financial entry, and
contract acceptance — each is something a person should own. Designing the state machine
*around* the pauses (rather than fighting them) is what makes the system safe, auditable, and
resumable. OTP/CAPTCHA can be auto-resolved for throughput; account/bank/contract stay human by
rule.
