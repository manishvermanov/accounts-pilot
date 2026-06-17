"""Browser runtime — the hands that drive the wizard.

Default driver: Playwright (sync API).
Stealth swap-in: CloakBrowser, a drop-in stealth Chromium with the SAME Playwright
API. The adapter declares `needs_stealth` per step; this runtime honours it.

The runtime exposes a small primitive surface (goto/fill/select/click/upload/
screenshot/wait). It deliberately knows nothing about Booking.com — the adapter
tells it what to do. That separation is what keeps OTA logic out of the driver.

Every interaction primitive (fill/click/select/check/scroll) is routed through a
HumanActor by default: curved mouse paths, dwell, and character-by-character
typing. This is the BEHAVIOUR layer — distinct from CloakBrowser's FINGERPRINT
layer. Anti-bot systems score both. Set `humanize=False` (or HUMANIZE=false in
.env) only for fast local tests against non-hostile pages.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Optional

from accounts_pilot.config import settings
from accounts_pilot.runtime.human import HumanActor

# A realistic, current desktop-Chrome user agent (kept in sync with CHROME_MAJOR).
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Anti-fingerprint init script — runs BEFORE any page script, in every frame. Patches
# the tells bot-detection reads (navigator.webdriver, missing window.chrome, empty
# plugins/mimeTypes, headless WebGL vendor, permissions, etc.) so a legitimate operator
# automating their OWN account isn't false-flagged as a bot.
_STEALTH_JS = r"""
(() => {
  const def = (o,p,g)=>{ try{ Object.defineProperty(o,p,{get:g,configurable:true}); }catch(e){} };
  // 1) webdriver flag
  def(navigator,'webdriver',()=>undefined);
  // 2) window.chrome
  if(!window.chrome){ window.chrome = {}; }
  window.chrome.runtime = window.chrome.runtime || {};
  window.chrome.app = window.chrome.app || { isInstalled:false, InstallState:{}, RunningState:{} };
  window.chrome.csi = window.chrome.csi || function(){return {};};
  window.chrome.loadTimes = window.chrome.loadTimes || function(){return {};};
  // 3) languages + plugins + mimeTypes (headless reports empty)
  def(navigator,'languages',()=>['en-IN','en-US','en','hi']);
  const fakePlugins=[{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}];
  def(navigator,'plugins',()=>{ const a=fakePlugins.slice(); a.item=i=>a[i]; a.namedItem=n=>a.find(p=>p.name===n); a.refresh=()=>{}; return a; });
  def(navigator,'mimeTypes',()=>{ const a=[{type:'application/pdf'}]; a.item=i=>a[i]; a.namedItem=()=>null; return a; });
  // 4) hardware
  def(navigator,'hardwareConcurrency',()=>8);
  def(navigator,'deviceMemory',()=>8);
  def(navigator,'platform',()=>'Win32');
  def(navigator,'maxTouchPoints',()=>0);
  def(navigator,'vendor',()=>'Google Inc.');
  // 5) permissions.query (headless returns 'denied' for notifications oddly)
  try{
    const orig = navigator.permissions && navigator.permissions.query;
    if(orig){ navigator.permissions.query = (p)=> p && p.name==='notifications'
      ? Promise.resolve({state: Notification.permission}) : orig(p); }
  }catch(e){}
  // 6) WebGL vendor/renderer (headless reveals SwiftShader/Google)
  try{
    const patch = (proto)=>{ const g=proto.getParameter; proto.getParameter=function(p){
      if(p===37445) return 'Intel Inc.';                 // UNMASKED_VENDOR_WEBGL
      if(p===37446) return 'Intel Iris OpenGL Engine';   // UNMASKED_RENDERER_WEBGL
      return g.apply(this,arguments); }; };
    if(window.WebGLRenderingContext) patch(WebGLRenderingContext.prototype);
    if(window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
  }catch(e){}
  // 7) Notification + connection
  try{ def(navigator,'connection',()=>({rtt:50,downlink:10,effectiveType:'4g',saveData:false})); }catch(e){}
  // 8) drop Selenium/Chromedriver globals if any harness left them
  for(const k of ['cdc_adoQpoasnfa76pfcZLmcfl_Array','cdc_adoQpoasnfa76pfcZLmcfl_Promise','cdc_adoQpoasnfa76pfcZLmcfl_Symbol','__webdriver_evaluate','__driver_evaluate','__selenium_unwrapped']){
    try{ delete window[k]; delete document[k]; }catch(e){}
  }
})();
"""


class BrowserRuntime:
    def __init__(self, *, stealth: bool = False, proxy: Optional[str] = None,
                 humanize: Optional[bool] = None, storage_path: str = "storage_state.json"):
        self.stealth = stealth
        self.proxy = proxy or (settings.cloak_proxy or None)
        self.humanize = settings.humanize if humanize is None else humanize
        self.storage_path = storage_path            # per-OTA login session file
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self.human: Optional[HumanActor] = None

    # ---- lifecycle -------------------------------------------------------
    def __enter__(self) -> "BrowserRuntime":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        launch_kwargs = {
            "headless": settings.headless,
            "slow_mo": settings.slow_mo_ms or None,
        }
        proxy_cfg = {"server": self.proxy} if self.proxy else None

        if self.stealth:
            self._start_stealth(launch_kwargs, proxy_cfg)
        else:
            self._start_playwright(launch_kwargs, proxy_cfg)

        # reuse saved cookies/session if present (lets parked jobs resume without re-login)
        state_file = Path(self.storage_path)
        ctx_kwargs = {"storage_state": str(state_file)} if state_file.exists() else {}
        # realistic, consistent fingerprint (UA / viewport / locale / timezone)
        ctx_kwargs.setdefault("user_agent", _UA)
        ctx_kwargs.setdefault("viewport", {"width": 1440, "height": 900})
        ctx_kwargs.setdefault("locale", "en-IN")
        ctx_kwargs.setdefault("timezone_id", "Asia/Kolkata")
        self._context = self._browser.new_context(**ctx_kwargs)
        with contextlib.suppress(Exception):
            self._context.add_init_script(_STEALTH_JS)   # patch automation tells pre-page
        self.page = self._context.new_page()
        self.human = HumanActor(
            self.page,
            enabled=self.humanize,
            key_delay=(settings.key_delay_min_s, settings.key_delay_max_s),
            think_range=(settings.think_min_s, settings.think_max_s),
        )

    def _start_playwright(self, launch_kwargs, proxy_cfg) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
        # strip the automation switches Chrome adds + hide the AutomationControlled flag
        launch_kwargs.setdefault("args", [])
        launch_kwargs["args"] += [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run", "--no-default-browser-check", "--no-service-autorun",
            "--password-store=basic", "--disable-features=IsolateOrigins,site-per-process",
        ]
        # containers (e.g. GitHub Codespaces) run Chromium without a usable sandbox + small
        # /dev/shm — gate these on an env flag so local Windows/macOS runs are unaffected.
        import os
        if os.environ.get("AP_BROWSER_NO_SANDBOX", "").lower() in ("1", "true", "yes"):
            launch_kwargs["args"] += ["--no-sandbox", "--disable-dev-shm-usage"]
        launch_kwargs["ignore_default_args"] = ["--enable-automation"]
        self._browser = self._pw.chromium.launch(**launch_kwargs)

    def _start_stealth(self, launch_kwargs, proxy_cfg) -> None:
        """CloakBrowser path. Falls back to Playwright if not installed."""
        try:
            import cloakbrowser  # type: ignore
        except ImportError:
            print("[runtime] cloakbrowser not installed — falling back to plain Playwright. "
                  "`pip install cloakbrowser` for stealth.")
            self._start_playwright(launch_kwargs, proxy_cfg)
            return

        # CloakBrowser mirrors Playwright's launch surface.
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
        self._browser = cloakbrowser.launch(**launch_kwargs)
        self._pw = None  # cloakbrowser manages its own lifecycle

    def active_page(self):
        """Follow new tabs/windows. Some flows (e.g. MMT 'List New Property') open the
        next step in a NEW tab — without this, automation keeps driving the old tab."""
        try:
            live = [p for p in self._context.pages if not p.is_closed()]
            if live and live[-1] is not self.page:
                self.page = live[-1]
                with contextlib.suppress(Exception):
                    self.page.bring_to_front()
                with contextlib.suppress(Exception):
                    self.page.wait_for_load_state("domcontentloaded", timeout=6000)
                with contextlib.suppress(Exception):
                    self.page.add_init_script(_STEALTH_JS)
                self.human = HumanActor(
                    self.page, enabled=self.humanize,
                    key_delay=(settings.key_delay_min_s, settings.key_delay_max_s),
                    think_range=(settings.think_min_s, settings.think_max_s),
                )
        except Exception:
            pass
        return self.page

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self._context:
                self._context.storage_state(path=self.storage_path)
        with contextlib.suppress(Exception):
            if self._browser:
                self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw:
                self._pw.stop()

    # ---- primitives (all human-routed) -----------------------------------
    def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.page.goto(url, wait_until=wait_until)
        self.think()  # land-and-look pause before interacting

    def fill(self, selector: str, value: str) -> None:
        """Human typing: move → dwell → clear → char-by-char with jitter."""
        self.human.type(selector, value)

    def select(self, selector: str, value: str) -> None:
        self.human.select(selector, value)

    def click(self, selector: str) -> None:
        """Human click: curved mouse move → dwell → press/release."""
        self.human.click(selector)

    def check(self, selector: str) -> None:
        self.human.check(selector)

    def scroll_to(self, selector: str) -> None:
        self.human.scroll_to(selector)

    def think(self, lo: float | None = None, hi: float | None = None) -> None:
        """Human 'think' pause between actions."""
        if self.human:
            self.human.think(lo, hi)

    def upload(self, selector: str, files: list[str]) -> None:
        # file inputs are OS-dialog backed; no mouse path to humanise here
        self.page.set_input_files(selector, files)

    def wait_for(self, selector: str, *, timeout_ms: int = 15000) -> None:
        self.page.wait_for_selector(selector, timeout=timeout_ms)

    def has(self, selector: str, *, timeout_ms: int = 2000) -> bool:
        try:
            self.page.wait_for_selector(selector, timeout=timeout_ms)
            return True
        except Exception:
            return False

    def screenshot(self, path: str) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=path, full_page=True)
        return path

    # ---- capture & detection (for selector harvesting + gate handling) ---
    def dump_capture(self, label: str) -> str:
        """Save the current page's HTML + screenshot so selectors can be harvested."""
        d = settings.artifacts_dir / "capture"
        d.mkdir(parents=True, exist_ok=True)
        base = d / label
        try:
            base.with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        try:
            print(f"  [capture] {self.page.url}  →  {base}.html")
        except Exception:
            pass
        return str(base)

    def detect_challenge(self) -> Optional[str]:
        """Return 'captcha' | 'verification' | None for the current page."""
        captcha_markers = [
            "iframe[src*='recaptcha']", "iframe[src*='captcha']", "iframe[title*='captcha']",
            "[class*='captcha']", "#px-captcha", "[id*='challenge']",
        ]
        for sel in captcha_markers:
            if self.has(sel, timeout_ms=600):
                return "captcha"
        otp_markers = [
            "input[autocomplete='one-time-code']", "input[name*='code']",
            "input[name*='otp']", "input[name*='pin']",
        ]
        for sel in otp_markers:
            if self.has(sel, timeout_ms=600):
                return "verification"
        return None

    def find_captcha(self) -> Optional[dict]:
        """Read the challenge's kind + sitekey off the page (reading only).
        Returns {'kind':…, 'site_key':…} or None. site_key may be '' for
        behavioural challenges (DataDome/PX) — those aren't solver-friendly."""
        probes = [
            ("recaptcha_v2", "[data-sitekey].g-recaptcha, .g-recaptcha[data-sitekey]"),
            ("hcaptcha",     ".h-captcha[data-sitekey], [data-hcaptcha-sitekey]"),
            ("turnstile",    ".cf-turnstile[data-sitekey]"),
        ]
        for kind, sel in probes:
            if self.has(sel, timeout_ms=500):
                try:
                    el = self.page.query_selector(sel)
                    key = el.get_attribute("data-sitekey") or el.get_attribute("data-hcaptcha-sitekey") or ""
                except Exception:
                    key = ""
                return {"kind": kind, "site_key": key}
        # challenge present but no readable sitekey (likely behavioural)
        if self.detect_challenge() == "captcha":
            return {"kind": "unknown", "site_key": ""}
        return None

    def apply_captcha_token(self, token: str, *, kind: str = "recaptcha_v2") -> None:
        """Inject a token obtained from the solver into the page (plumbing — inert
        without a token from your `_solve` hook)."""
        if not token:
            return
        # reCAPTCHA v2/v3: set the hidden response field and fire change events.
        js = (
            "(t)=>{const f=document.getElementById('g-recaptcha-response');"
            "if(f){f.value=t;f.dispatchEvent(new Event('change',{bubbles:true}));}"
            "document.querySelectorAll('textarea[name=\"h-captcha-response\"],"
            "input[name=\"cf-turnstile-response\"]').forEach(e=>{e.value=t;});}"
        )
        try:
            self.page.evaluate(js, token)
        except Exception:
            pass

    def click_text(self, text: str, *, timeout_ms: int = 8000) -> bool:
        """Human-click the first element matching visible text. Returns False if absent."""
        sel = f"text={text}"
        if self.has(sel, timeout_ms=timeout_ms):
            self.human.click(sel)
            return True
        return False

    def try_advance(self) -> bool:
        """Click the wizard's primary 'continue' button. Polls a few rounds because
        Booking enables the button a beat AFTER the field is filled/selected. Each
        round tries the stable language-independent test-id first, then visible text.
        Returns False only if nothing clickable appears across all rounds."""
        css_candidates = (
            '[data-testid="FormButtonPrimary-enabled"]',
            'button[data-testid^="FormButtonPrimary"]:not([data-testid$="-disabled"])',
        )
        # NOTE: bare "Save" is deliberately omitted — it matches "Save & exit" and would
        # exit the wizard. Only explicit forward-navigation labels here.
        text_labels = ("Continue", "Next", "Save and continue", "Save & continue",
                       "Confirm", "Continuați", "Continuar", "Continuer", "Weiter")
        for attempt in range(4):                      # ~3-4s total, lets the button enable
            for sel in css_candidates:
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible() and loc.is_enabled():
                        self.human.click(sel)
                        self.think()
                        return True
                except Exception:
                    pass
            for label in text_labels:
                if self.click_text(label, timeout_ms=700):
                    self.think()
                    return True
            self.think(0.6, 1.0)                      # wait for a disabled button to enable
        return False
