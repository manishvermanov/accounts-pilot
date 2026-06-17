"""Local Playwright + AgentQL form filler.

AgentQL resolves the form's elements through its cloud API — **element-resolution
only**. Every action (typing, selecting, clicking, submitting) runs **locally** in
a visible Chromium. Resolution happens on the **EMPTY form**, before any value is
typed, so the DOM snapshot sent to AgentQL's API carries no real PII.

This is how we fill an OTA wizard inside the operator's own logged-in browser
(persistent context reuses the session) without hand-coding brittle selectors.

Config-driven: the target URL, the AgentQL query describing the form, the field
values, and the submit control are all inputs — nothing is hardcoded.

Run: `pip install agentql && agentql init` (or set AGENTQL_API_KEY), then
`python -m accounts_pilot.cli agentql-fill --config form.json [--dry-run]`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class FormField:
    el: str                  # alias used in the AgentQL query
    value: str = ""
    action: str = "fill"     # fill | select | check | click


@dataclass
class FormConfig:
    url: str
    query: str               # AgentQL query describing the form structure
    fields: list[FormField]
    submit: Optional[str] = None        # alias of the submit control
    user_data_dir: Optional[str] = None  # reuse a logged-in session
    success_text: Optional[str] = None   # text that confirms a successful submit
    screenshot_path: str = "form-run.png"


def fill_form(cfg: FormConfig, *, dry_run: bool = False) -> bool:
    """Fill (and optionally submit) a form locally; AgentQL resolves the fields."""
    if not os.environ.get("AGENTQL_API_KEY"):
        raise RuntimeError("AGENTQL_API_KEY is not set in the environment.")

    import agentql
    from playwright.sync_api import sync_playwright

    browser = None
    context = None
    page = None
    success = False

    with sync_playwright() as p:
        try:
            # ---- launch local, VISIBLE Chromium ----
            if cfg.user_data_dir:                     # reuse cookies / logged-in session
                context = p.chromium.launch_persistent_context(
                    user_data_dir=cfg.user_data_dir, headless=False)
                raw_page = context.pages[0] if context.pages else context.new_page()
            else:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context()
                raw_page = context.new_page()

            page = agentql.wrap(raw_page)
            page.goto(cfg.url)
            page.wait_for_page_ready_state()

            # ---- ONE element-resolution call, on the EMPTY form (no PII in DOM yet) ----
            els = page.query_elements(cfg.query)

            # ---- every action below is LOCAL native Playwright ----
            for fld in cfg.fields:
                loc = getattr(els, fld.el, None)
                if loc is None:
                    print(f"[warn] '{fld.el}' not resolved — skipped")
                    continue
                if fld.action == "fill":
                    loc.fill(fld.value)
                elif fld.action == "select":
                    loc.select_option(fld.value)
                elif fld.action == "check":
                    loc.check()
                elif fld.action == "click":
                    loc.click()
                print(f"[ok] {fld.action:<6} {fld.el} = {fld.value[:50]}")

            # ---- submit (unless dry run) ----
            if dry_run:
                print("DRY_RUN: form filled, submit skipped.")
                success = True
            elif cfg.submit:
                sub = getattr(els, cfg.submit, None)
                if sub is None:
                    raise RuntimeError(f"submit control '{cfg.submit}' not resolved")
                sub.click()
                page.wait_for_page_ready_state()
                if cfg.success_text:
                    body = (page.inner_text("body") or "").lower()
                    success = cfg.success_text.lower() in body
                else:
                    success = True
                print("SUCCESS: form submitted." if success
                      else "FAILURE: submitted but success text not found.")
            else:
                success = True

            return success

        except Exception as e:
            print(f"FAILURE: {type(e).__name__}: {e}")
            try:
                if page is not None:
                    page.screenshot(path=cfg.screenshot_path, full_page=True)
                    print(f"screenshot → {cfg.screenshot_path}")
            except Exception:
                pass
            return False
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass


# --- config loading + stored-property bridge ------------------------------
def load_config(data: dict) -> FormConfig:
    return FormConfig(
        url=data["url"],
        query=data["query"],
        fields=[FormField(**f) for f in data.get("fields", [])],
        submit=data.get("submit"),
        user_data_dir=data.get("user_data_dir"),
        success_text=data.get("success_text"),
        screenshot_path=data.get("screenshot_path", "form-run.png"),
    )


def resolve_property_refs(cfg: FormConfig, profile) -> FormConfig:
    """Replace field values like '@contact.email' with the property's STORED value."""
    dump = profile.model_dump()

    def lookup(path: str) -> str:
        cur = dump
        for part in path.split("."):
            if isinstance(cur, list):
                cur = cur[int(part)]
            elif isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return ""
            if cur is None:
                return ""
        return str(cur)

    for fld in cfg.fields:
        if isinstance(fld.value, str) and fld.value.startswith("@"):
            fld.value = lookup(fld.value[1:])
    return cfg
