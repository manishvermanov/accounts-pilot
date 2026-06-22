"""Live browser session driven from the dashboard.

Connect opens a visible browser and reuses the saved login session (so Booking
never sees a fresh automated sign-in to block). It stays alive in a background
thread; the dashboard sends it commands (fill) which run IN THAT SAME THREAD
(Playwright pages are thread-affine) on the already-logged-in page.

Fill uses AgentQL to resolve the current page's fields (cloud, element-only),
then native Playwright fills them with the property's stored data. No fresh
login, no CAPTCHA solver, no brittle selectors.
"""
from __future__ import annotations

import os
import queue
import threading
from typing import Optional

from accounts_pilot.adapters import get_adapter
from accounts_pilot.config import settings
from accounts_pilot.photos import prepare_many, prepare_photo
from accounts_pilot.runtime.browser import BrowserRuntime


def _dd_norm(s) -> str:
    """Normalise a dropdown label for matching: strip accents, collapse whitespace, lowercase."""
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().lower()


def _dd_strip_paren(s: str) -> str:
    import re
    return re.sub(r"\s*\([^)]*\)\s*", " ", s).strip()


def dropdown_tier(opt, want) -> int:
    """How well a dropdown OPTION matches a WANTED value. Lower = better match:
      0  exact
      1  exact after dropping a '(…)' suffix  ('Chhattisgarh (CG)' == 'Chhattisgarh')
      2  option starts with want at a word boundary  ('Chhattisgarh State' for 'Chhattisgarh')
      3  want starts with option at a word boundary
      99 no match
    There is intentionally NO loose-substring tier — that's what made the picker grab
    'Deluxe Suite' for 'Deluxe' or click a stray page fragment. Exact always beats prefix."""
    o, w = _dd_norm(opt), _dd_norm(want)
    if not o or not w:
        return 99
    if o == w:
        return 0
    op, wp = _dd_strip_paren(o), _dd_strip_paren(w)
    if op and op == wp:
        return 1
    if wp and op.startswith(wp) and (len(op) == len(wp) or not op[len(wp)].isalnum()):
        return 2
    if op and wp.startswith(op) and (len(wp) == len(op) or not wp[len(op)].isalnum()):
        return 3
    return 99


class LiveSession:
    def __init__(self, ota: str = "booking_com"):
        self.ota = ota                                 # which OTA this session drives
        self.state = "idle"   # idle|starting|awaiting_captcha|awaiting_otp|connected|filling|error
        self.log: list[str] = []
        self.error = ""
        self.rt: Optional[BrowserRuntime] = None
        self._thread: Optional[threading.Thread] = None
        self._captcha = threading.Event()
        self._otp = threading.Event()
        self._otp_value = ""
        self._stop = threading.Event()                 # operator Kill — halts a running fill
        self._cmd: "queue.Queue[tuple]" = queue.Queue()

    def _say(self, m: str):
        self.log.append(m)
        try:                                          # persist to a per-OTA file (survives restarts)
            from pathlib import Path
            p = Path(settings.db_path).parent / "logs" / f"{self.ota}.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(m + "\n")
        except Exception:
            pass

    def _credentials(self, ota: str):
        """Per-OTA partner credentials (pre-filled at login; the operator still solves
        any CAPTCHA/OTP). Falls back to empty so the operator can type them in."""
        if ota == "booking_com":
            return settings.booking_partner_email, settings.booking_partner_password
        # other OTAs read OTA-specific env vars if present, else blank (manual login)
        prefix = ota.upper()
        return (os.environ.get(f"{prefix}_PARTNER_EMAIL", ""),
                os.environ.get(f"{prefix}_PARTNER_PASSWORD", ""))

    # ---- control (called from web thread) --------------------------------
    def start(self, ota: Optional[str] = None):
        if ota:
            self.ota = ota
        if self._thread and self._thread.is_alive():
            return
        self.state, self.log, self.error = "starting", [], ""
        self._captcha.clear(); self._otp.clear()
        self._thread = threading.Thread(target=self._run, args=(self.ota,), daemon=True)
        self._thread.start()

    def captcha_done(self):
        self._captcha.set()

    def stop(self):
        """Operator Kill — halt the fill that's currently running. Sets a flag the fill loop
        checks each pass, and releases any gate it's paused at, so it stops promptly and
        returns to 'connected' (the browser stays open for another Fill)."""
        self._stop.set()
        self._captcha.set(); self._otp.set()           # unblock a paused captcha/otp/bank gate
        self._say("⏹ Stop requested — halting the fill…")

    def submit_otp(self, code: str):
        self._otp_value = code
        self._otp.set()

    def fill(self, profile_data: dict):
        self._cmd.put(("fill", profile_data))

    def record(self):
        self._cmd.put(("record", None))

    def _dump_training(self, idx: int, url: str):
        """Capture an unmapped OTA page's full structure to data/training/<ota>/ — the
        raw material for mapping its flow once (the 'train' step of train-once/run-free).
        Records URL, heading, body text, and every scraped control (selector+label+type)."""
        import json
        from pathlib import Path
        from urllib.parse import urlparse
        from accounts_pilot.web import llm_fill
        page = self.rt.page
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        try:
            heading = page.evaluate(
                "() => { const e=document.querySelector('h1,h2,[role=heading]');"
                " return e? e.innerText.slice(0,160):''; }") or ""
        except Exception:
            heading = ""
        try:
            body = (page.inner_text("body") or "")[:4000]
        except Exception:
            body = ""
        controls = llm_fill.scrape_fields(page)
        path_slug = (urlparse(url).path or "page").strip("/").replace("/", "_") or "page"
        out_dir = Path(settings.db_path).parent / "training" / self.ota
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{idx:02d}_{path_slug}.json"
        out.write_text(json.dumps({
            "ota": self.ota, "index": idx, "url": url, "heading": heading,
            "body_excerpt": body, "controls": controls,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        self._say(f"  · 📚 captured page → training/{self.ota}/{out.name} ({len(controls)} controls)")

    def _do_record(self):
        """Capture the CURRENT page's fields (selector + label + type) to a recordings
        file, so we can build a deterministic Booking field map from a manual walk-through."""
        import json
        from pathlib import Path
        from accounts_pilot.web import llm_fill
        try:
            page = self.rt.page
            url = page.url
            key = self._page_key(url)
            fields = llm_fill.scrape_fields(page)
            path = Path(settings.db_path).parent / "booking_pages.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            data[key] = {"url": url, "fields": fields}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self._say(f"📝 Recorded {len(fields)} fields on “{key}” — {len(data)} page(s) captured so far.")
        except Exception as e:
            self._say(f"Record error: {type(e).__name__}: {e}")

    def status(self) -> dict:
        return {"ota": self.ota, "state": self.state, "log": self.log[-30:], "error": self.error}

    # ---- worker thread (owns the browser) --------------------------------
    def _run(self, ota: str):
        adapter = get_adapter(ota)
        try:
            self.rt = BrowserRuntime(stealth=False, humanize=True,
                                     storage_path=f"storage_state_{ota}.json")
            self.rt.start()
            self._say(f"Opening {getattr(adapter, 'display_name', ota)}…")
            email, password = self._credentials(ota)
            result = adapter.login(self.rt, email, password)
            for _ in range(8):
                if result == "captcha":
                    self.state = "awaiting_captcha"
                    self._say("Solve the CAPTCHA in the browser window, then click Done.")
                    self._captcha.wait(); self._captcha.clear()
                    self.rt.try_advance()
                elif result == "verification":
                    self.state = "awaiting_otp"
                    self._say("Booking sent a code — enter it below.")
                    self._otp.wait(); self._otp.clear()
                    for sel in ("input[autocomplete='one-time-code']", "input[name*='code']",
                                "input[name*='otp']", "input[name*='pin']"):
                        if self.rt.has(sel, timeout_ms=1500):
                            self.rt.fill(sel, self._otp_value); break
                    self.rt.try_advance()
                else:
                    break
                self.rt.think()
                nxt = self.rt.detect_challenge()
                result = nxt if nxt else "ok"

            self.state = "connected"
            self._say("Connected ✓ — logged in. Navigate to a form page, then click Fill.")

            # command loop — keep the browser alive, run fills in THIS thread
            while True:
                try:
                    cmd = self._cmd.get(timeout=1800)
                except queue.Empty:
                    break
                if cmd[0] == "stop":
                    break
                if cmd[0] == "fill":
                    self._do_fill(cmd[1])
                if cmd[0] == "record":
                    self._do_record()
        except Exception as e:
            self.state = "error"
            self.error = str(e)
            self._say(f"Error: {type(e).__name__}: {e}")
        finally:
            try:
                if self.rt:
                    self.rt.stop()
            except Exception:
                pass

    # ---- fill plan + selector cache -------------------------------------
    TYPE_CATEGORY = {
        "hotel": "Hotel, B&Bs & More", "guesthouse": "Hotel, B&Bs & More",
        "bnb": "Hotel, B&Bs & More", "hostel": "Hotel, B&Bs & More",
        "resort": "Hotel, B&Bs & More", "apartment": "Apartment",
        "aparthotel": "Apartment", "homestay": "Homes", "villa": "Homes",
        "holiday_home": "Homes",
    }

    # detailed-category label (the 2nd category page: Hotel / Guesthouse / B&B / …)
    TYPE_LABEL = {
        "hotel": "Hotel", "guesthouse": "Guesthouse", "bnb": "Bed and breakfast",
        "hostel": "Hostel", "homestay": "Homestay", "resort": "Hotel",
        "apartment": "Apartment", "aparthotel": "Condo hotel", "villa": "Country House",
        "holiday_home": "Homestay",
    }

    def _plan(self, p):
        """(alias, value, action) per field — decisions driven by the JSON."""
        return [
            ("property_type_button", None, "click"),   # page 1: 4 big cards
            ("category_card", None, "click"),          # page 2: detailed category list
            ("property_name_input", p.display_name, "fill"),
            ("star_rating_select", str(p.star_rating) if p.star_rating else "", "select"),
            ("description_textarea", p.description, "fill"),
            ("address_line_1_input", p.address.line1, "fill"),
            ("city_input", p.address.city, "fill"),
            ("postal_code_input", p.address.postal_code, "fill"),
            ("phone_input", p.contact.phone, "fill"),
            ("email_input", p.contact.email, "fill"),
        ]

    def _query(self, p):
        cat = self.TYPE_CATEGORY.get(p.property_type.value, "Hotel, B&Bs & More")
        label = self.TYPE_LABEL.get(p.property_type.value, "Hotel")
        return ("{ "
                f'property_type_button(the "List your property" button in the "{cat}" category card) '
                f'category_card(the clickable property-category option labeled exactly "{label}") '
                "property_name_input star_rating_select description_textarea "
                "address_line_1_input city_input postal_code_input phone_input email_input }")

    @property
    def _cache_path(self):
        from pathlib import Path
        return Path(settings.db_path).parent / "agentql_selectors.json"

    def _load_cache(self) -> dict:
        import json
        try:
            return json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache(self, cache: dict):
        import json
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    # ---- learned page maps (LLM once → replay key-free) ------------------
    @property
    def _pmap_path(self):
        from pathlib import Path
        return Path(settings.db_path).parent / f"page_maps_{self.ota}.json"

    def _load_page_maps(self) -> dict:
        import json
        try:
            return json.loads(self._pmap_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_page_maps(self, maps: dict):
        import json
        self._pmap_path.parent.mkdir(parents=True, exist_ok=True)
        self._pmap_path.write_text(json.dumps(maps, indent=2, ensure_ascii=False), encoding="utf-8")

    def _map_key(self, p) -> str:
        """Stable per-property, per-page key — path + heading only (NO volatile query
        token), so a learned map matches the same page on a later run."""
        from urllib.parse import urlparse
        try:
            path = urlparse(self.rt.page.url).path
        except Exception:
            path = ""
        try:
            h = self.rt.page.evaluate(
                "() => { const e=document.querySelector('h1,h2,[role=heading]');"
                " return (e? e.innerText : '').slice(0,80); }")
        except Exception:
            h = ""
        pid = str(getattr(p, "property_id", "") or "")
        return f"{pid}::{path}::{(h or '').strip()}"

    def _element_label(self, loc) -> str:
        js = r"""el => {
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
            if (el.id){ const l=document.querySelector('label[for=\"'+CSS.escape(el.id)+'\"]'); if(l) return l.innerText.trim(); }
            const lp = el.closest('label'); if (lp) return lp.innerText.trim().slice(0,80);
            if (el.placeholder) return el.placeholder;
            let p=el.parentElement,h=0; while(p&&h<3){ const x=p.querySelector('label,legend,h2,h3,strong'); if(x&&x.innerText.trim()) return x.innerText.trim().slice(0,80); p=p.parentElement; h++; }
            return (el.innerText||el.value||'').trim().slice(0,80);
        }"""
        try:
            return (loc.evaluate(js) or "").strip()
        except Exception:
            return ""

    def _stable_descriptor(self, selector: str):
        """Turn a live selector into a STABLE, replayable locator for the cache file.
        KEEP already-stable selectors verbatim — a real id, a name=, or a test-id
        (`data-testid` OR MMT's hyphenated `data-test-id`, automation_id). These replay
        precisely. Only when the selector is a fragile DOM path (nth-of-type) do we fall
        back to the element's visible LABEL — but a label like 'Yes'/'No' is ambiguous on
        a page with many of them, so prefer the test-id path whenever one exists."""
        if not selector:
            return None
        try:
            loc = self.rt.page.locator(selector).first
            if loc.count() == 0:
                return None
        except Exception:
            return None
        s = selector.lower()
        # a real, non-volatile #id: starts with a letter, no ':' (React useId → ':rs:' etc.)
        real_id = (selector.startswith("#") and selector[1:2].isalpha()
                   and ":" not in selector and "\\" not in selector)
        stable = (real_id or "[name=" in s or "data-testid" in s
                  or "data-test-id" in s or "automation_id" in s or "[id=" in s)
        if stable:
            return ("css", selector)
        # fragile DOM-path selector → try the element's OWN test-id first, then a label
        tid = None
        try:
            tid = loc.evaluate(
                "el => el.getAttribute('data-test-id') || el.getAttribute('data-testid') "
                "|| (el.id && !/^[:r]/.test(el.id) ? el.id : null)")
        except Exception:
            tid = None
        if tid:
            try:                            # pin to whichever stable attribute actually holds it
                if loc.evaluate("(el,v)=>el.getAttribute('data-test-id')===v", tid):
                    return ("css", f'[data-test-id="{tid}"]')
                if loc.evaluate("(el,v)=>el.getAttribute('data-testid')===v", tid):
                    return ("css", f'[data-testid="{tid}"]')
                if loc.evaluate("(el,v)=>el.id===v", tid):
                    return ("css", f'#{tid}')
            except Exception:
                pass
        lab = self._element_label(loc)
        if lab:
            return ("label", lab)
        return ("css", selector)        # last resort (may not survive id churn)

    def _maps_cache(self) -> dict:
        """Fast tier: session-level in-memory cache of learned page maps. Survives across
        Fill runs within the running server (unlike a per-run reload)."""
        c = getattr(self, "_pmap_cache", None)
        if c is None:
            c = {}
            self._pmap_cache = c
        return c

    def _lookup_map(self, mkey: str):
        """Two-tier lookup. Returns the stored entries for `mkey`, or None to fall through
        to the LLM:
          1) in-memory cache  (fast)
          2) the durable file page_maps_<ota>.json  (on a cache miss; promotes the hit)
        """
        cache = self._maps_cache()
        if mkey in cache:
            return cache[mkey]                       # cache hit
        disk = self._load_page_maps()                # file tier — re-read so external edits land
        if mkey in disk:
            cache[mkey] = disk[mkey]                 # promote file → cache
            return disk[mkey]
        return None                                  # miss on both → ask the LLM

    def _store_map(self, mkey: str, entries) -> None:
        """Write a learned map to BOTH tiers — the in-memory cache and the durable file."""
        self._maps_cache()[mkey] = entries
        disk = self._load_page_maps()
        disk[mkey] = entries
        try:
            self._save_page_maps(disk)
        except Exception:
            pass

    def _commit_pending(self, pending) -> None:
        """Persist a just-learned page map to cache+file — called ONLY after the page
        advanced, so we never store a mapping that didn't actually move the wizard forward.
        Skipped in autopilot mode so dummy values never poison the durable maps file."""
        if not pending:
            return
        if getattr(self, "_autopilot_active", False):
            return
        mkey, entries = pending
        try:
            self._store_map(mkey, entries)
            self._say(f"  · learned this page → stored {len(entries)} field(s) (next run: no LLM here)")
        except Exception as e:
            self._say(f"  · couldn't save learned map: {type(e).__name__}: {e}")

    def _apply_stored(self, entries) -> tuple:
        """Replay a learned page map with plain Playwright — no LLM call."""
        page = self.rt.page
        did = 0; navigated = False
        for e in entries or []:
            by = e.get("by"); locv = e.get("locator")
            action = e.get("action"); val = e.get("value", "")
            loc = self._locate(by, locv)
            if loc is None:
                continue
            before = self._page_sig()
            if self._act(loc, str(locv)[:30], val, action, cached=True):
                did += 1
            if action in ("click", "check"):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=2500)
                except Exception:
                    pass
                if self._page_sig() != before:
                    navigated = True; break
        return did, navigated

    @staticmethod
    def _page_key(url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).path or url

    def _page_sig(self) -> str:
        """Content signature — Booking keeps the same URL across SPA steps, so detect
        page changes by the main heading + url, not url alone."""
        try:
            h = self.rt.page.evaluate(
                "() => { const e=document.querySelector('h1,h2,[role=heading]');"
                " return (e? e.innerText : (document.title||'')).slice(0,160); }")
        except Exception:
            h = ""
        try:
            url = self.rt.page.url
        except Exception:
            url = ""
        return f"{url}::{h}"

    def _page_fingerprint(self) -> str:
        """A snapshot of the page's fillable STATE: every input/checkbox/radio value, the
        text shown on dropdown-trigger buttons, and how many error/invalid markers remain.
        Two passes with the same fingerprint mean nothing actually changed — so re-filling
        already-filled fields is NOT counted as progress (that was the loop)."""
        try:
            return self.rt.page.evaluate(r"""() => {
              let v='';
              document.querySelectorAll('input,textarea,select').forEach(e=>{
                v += ((e.type==='checkbox'||e.type==='radio') ? (e.checked?'1':'0')
                     : (e.value||'')) + '';
              });
              // button-dropdowns show the chosen value as their text
              let d='';
              document.querySelectorAll('[data-testid] button,[role=combobox],[class*=dropdown] button')
                .forEach(b=>{ d += (b.innerText||'').slice(0,24) + ''; });
              const err = document.querySelectorAll(
                '[intent=error],[aria-invalid=true],[class*=negative-strong]').length;
              const n = document.querySelectorAll('input,textarea,select,button,[role=option]').length;
              return n + '|' + err + '|' + (v + d).slice(0, 4000);
            }""") or ""
        except Exception:
            return ""

    def _cacheable_selector(self, locator) -> Optional[str]:
        """Compute a stable selector for a resolved element, so plain Playwright can
        replay it later with NO AI key."""
        js = """el => {
            if (el.id) return '#' + CSS.escape(el.id);
            const nm = el.getAttribute('name'); if (nm) return el.tagName.toLowerCase()+'[name=\"'+nm+'\"]';
            const t = el.getAttribute('data-testid'); if (t) return '[data-testid=\"'+t+'\"]';
            function seg(e){ let i=1,s=e.previousElementSibling; while(s){ if(s.tagName===e.tagName)i++; s=s.previousElementSibling; } return e.tagName.toLowerCase()+':nth-of-type('+i+')'; }
            let parts=[],e=el; while(e&&e.nodeType===1&&e.tagName!=='HTML'){ parts.unshift(seg(e)); e=e.parentElement; }
            return parts.join(' > ');
        }"""
        try:
            return locator.evaluate(js)
        except Exception:
            return None

    def _click_robust(self, loc):
        """Click fast; if the element is hidden/covered (Booking hides the real radio/
        checkbox behind a styled label), fall back to a JS click that fires anyway.
        Never waits the 30s default — a hidden input would otherwise stall the page."""
        try:
            loc.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        try:
            loc.click(timeout=3500)
            return
        except Exception:
            pass
        # hidden input → click its <label> (or the element itself) via JS
        loc.evaluate("e => { const l = e.id && document.querySelector('label[for=\\''+e.id+'\\']'); (l||e).click(); }")

    def _check_robust(self, loc):
        try:
            if loc.is_checked():
                return
        except Exception:
            pass
        try:
            loc.check(timeout=3500)
            return
        except Exception:
            pass
        loc.evaluate("e => { const l = e.id && document.querySelector('label[for=\\''+e.id+'\\']'); if (e.tagName==='INPUT' && e.checked) return; (l||e).click(); }")

    def _click_text_option(self, val) -> bool:
        """After opening a custom dropdown, click the option whose text/value matches
        `val`. Options vary wildly (role=option, <li>, or just styled <div>s), so we look
        inside any open listbox/menu/dropdown container AND for explicit option roles,
        prefer a LEAF element (fewest children) with exact text, then a contains match.
        Pierces shadow DOM."""
        js = r"""(v) => {
          const vis=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0;};
          const txt=n=>((n.innerText||n.getAttribute&&n.getAttribute('data-value')||n.textContent||'')).replace(/\s+/g,' ').trim();
          const norm=s=>(s||'').toLowerCase();
          const out=[];
          function walk(root){
            // explicit options anywhere
            try{ root.querySelectorAll('[role=option],li,[class*=option],[class*=Option],[data-value]').forEach(n=>{if(vis(n)&&txt(n))out.push(n);}); }catch(e){}
            // any leaf-ish element inside an OPEN dropdown/menu/listbox popup
            try{
              root.querySelectorAll('[role=listbox],[class*=menu],[class*=Menu],[class*=options],[class*=Options],[class*=popover],[class*=Popover],[class*=dropdown],[class*=Dropdown]').forEach(c=>{
                if(!vis(c))return;
                c.querySelectorAll('div,span,p,a').forEach(n=>{
                  const t=txt(n);
                  if(vis(n)&&t&&n.children.length<=1 && !/^select\b/i.test(t)
                     && n.tagName!=='INPUT' && !n.querySelector('input,textarea')) out.push(n);
                });
              });
            }catch(e){}
            let all=[]; try{ all=root.querySelectorAll('*'); }catch(e){}
            all.forEach(el=>{ if(el.shadowRoot) walk(el.shadowRoot); });
          }
          walk(document);
          const exact = out.filter(n=>norm(txt(n))===norm(v)).sort((a,b)=>a.children.length-b.children.length)[0];
          if(exact) return exact;
          return out.filter(n=>norm(txt(n)).includes(norm(v))).sort((a,b)=>txt(a).length-txt(b).length)[0] || null;
        }"""
        for ctx in self._frames():
            try:
                h = ctx.evaluate_handle(js, str(val))
                el = h.as_element()
                if el is not None:
                    el.click()
                    return True
            except Exception:
                pass
        return False

    def _pick_first_option(self) -> bool:
        """After opening a custom dropdown, click its FIRST visible option — a last resort
        for a REQUIRED dropdown whose options we can't predict (e.g. MMT 'room view'), so
        the page can still advance. Scoped to a visible listbox/menu so we don't click stray
        list items elsewhere on the page."""
        js = r"""() => {
          const vis=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0;};
          const txt=n=>((n.innerText||n.textContent||'')).replace(/\s+/g,' ').trim();
          const conts=[...document.querySelectorAll(
            '[role=listbox],[class*=menu],[class*=Menu],[class*=options],[class*=Options],[class*=popover],[class*=Popover],[class*=dropdown],[class*=Dropdown],ul')];
          for(const c of conts){
            if(!vis(c)) continue;
            let opts=[...c.querySelectorAll('[role=option],li,[class*=option],[class*=Option],[data-value]')]
              .filter(n=>vis(n)&&txt(n));
            if(!opts.length)  // styled-<div> options with no helpful class — take leaf elements with text
              opts=[...c.querySelectorAll('div,span,p,a')].filter(n=>vis(n)&&txt(n)&&n.children.length<=1
                     && n.tagName!=='INPUT' && !n.querySelector('input,textarea'));
            // skip a leading placeholder/label if it looks like 'Select …'
            opts=opts.filter(n=>!/^select\b/i.test(txt(n)));
            if(opts.length) return opts[0];
          }
          return null;
        }"""
        for ctx in self._frames():
            try:
                h = ctx.evaluate_handle(js)
                el = h.as_element()
                if el is not None:
                    el.click()
                    return True
            except Exception:
                pass
        return False

    def _open_and_pick(self, loc, values, strict: bool = False) -> bool:
        """Select from a custom dropdown WITHOUT knowing its option markup. Snapshot the
        page's visible clickable texts, OPEN the dropdown, then diff — the texts that newly
        appeared ARE the options (whatever their <div>/<li>/role). Click the one matching a
        value. Robust across every OTA's styled dropdowns.

        strict=True → NEVER fall back to the first option (use for geography: country /
        state / city / nationality, where a wrong value like the alphabetical-first
        'Andaman and Nicobar Islands' is far worse than leaving it blank for the operator)."""
        page = self.rt.page
        scan = r"""() => {
          const vis=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0;};
          const txt=n=>((n.innerText||n.textContent||'')).replace(/\s+/g,' ').trim();
          const out=[];
          document.querySelectorAll('[role=option],li,div,span,p,a').forEach(n=>{
            if(!vis(n))return; const t=txt(n);
            if(!t||t.length>60)return; if(n.children.length>1)return;
            if(n.tagName==='INPUT'||n.querySelector('input,textarea'))return;
            let cur=''; try{cur=getComputedStyle(n).cursor;}catch(e){}
            if(cur==='pointer'||(n.getAttribute&&n.getAttribute('role')==='option')) out.push(t);
          });
          return out;
        }"""
        try:
            before = set(page.evaluate(scan))
        except Exception:
            before = set()
        try:
            self._click_robust(loc)           # open the dropdown
            self.rt.think(0.7, 1.1)
        except Exception:
            return False
        # searchable dropdown? (Agoda's open a floating list with a 'Search' box) — type a
        # filter term so the matching option actually renders before we pick.
        if values:
            try:
                term = str(values[-1])[:24]   # most general candidate (e.g. 'Deluxe')
                for sel in ("[data-testid='floater-container'] input",
                            "input[placeholder='Search']",
                            "[role='listbox'] input", "[class*='SearchField'] input"):
                    sb = page.locator(sel).last
                    if sb.count() and sb.is_visible():
                        sb.fill(term)
                        self.rt.think(0.7, 1.1)
                        break
            except Exception:
                pass
        tag = r"""([beforeList, wantList]) => {
          const before=new Set(beforeList);
          const want=new Set(wantList.map(s=>(s||'').toLowerCase().trim()));
          const vis=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0;};
          const txt=n=>((n.innerText||n.textContent||'')).replace(/\s+/g,' ').trim();
          document.querySelectorAll('[data-ap-opt]').forEach(e=>e.removeAttribute('data-ap-opt'));
          let i=0; const opts=[];
          document.querySelectorAll('[role=option],li,div,span,p,a').forEach(n=>{
            if(!vis(n))return; const t=txt(n);
            if(!t||t.length>60)return; if(n.children.length>1)return;
            if(n.tagName==='INPUT'||n.querySelector('input,textarea'))return;
            let cur=''; try{cur=getComputedStyle(n).cursor;}catch(e){}
            if(!(cur==='pointer'||(n.getAttribute&&n.getAttribute('role')==='option')))return;
            // keep an option if it's NEWLY-revealed OR it exactly matches something we want
            // (a wanted value like 'Free' must not be dropped just because that text already
            //  shows elsewhere on the page, e.g. another amenity's chosen value).
            if(before.has(t) && !want.has(t.toLowerCase()))return;
            if(/^(select|choose)\b/i.test(t))return;   // skip the placeholder itself
            n.setAttribute('data-ap-opt', String(i)); opts.push({idx:i, text:t}); i++;
          });
          return opts;
        }"""
        try:
            opts = page.evaluate(tag, [list(before), [str(v) for v in (values or [])]]) or []
        except Exception:
            opts = []
        # 1) pick the STRONGEST precise match among the newly-revealed options (so 'Deluxe'
        #    lands on 'Deluxe', never on 'Deluxe Suite')
        best = None                                   # (tier, opt)
        for v in (values or []):
            if not _dd_norm(v):
                continue
            for o in (opts or []):
                t = dropdown_tier(o["text"], v)
                if t < 99 and (best is None or t < best[0]):
                    best = (t, o)
            if best and best[0] == 0:
                break
        if best:
            try:
                self._click_robust(page.locator(f'[data-ap-opt="{best[1]["idx"]}"]').first)
                self._say(f"    · dropdown → ‘{best[1]['text']}’")
                return True
            except Exception:
                pass

        # 2) broad scan — long/virtualised lists (State/City) don't reveal every row in the
        #    diff. Scan ONLY inside the open dropdown container (never the whole page, so a
        #    stray clickable can't be hit), and require the SAME precise tiered match.
        pick_js = r"""([wantList]) => {
          const norm=s=>(s||'').normalize('NFKD').replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
          const strip=s=>s.replace(/\s*\([^)]*\)\s*/g,' ').trim();
          const want=wantList.map(norm).filter(s=>s.length>1);
          if(!want.length)return null;
          const vis=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0;};
          const txt=n=>((n.innerText||n.textContent||'')).replace(/\s+/g,' ').trim();
          const tierOf=(o,w)=>{const op=strip(o),wp=strip(w);
            if(o===w)return 0; if(op===wp)return 1;
            if(wp&&op.startsWith(wp)&&(op.length===wp.length||!/[a-z0-9]/.test(op[wp.length])))return 2;
            if(op&&wp.startsWith(op)&&(wp.length===op.length||!/[a-z0-9]/.test(wp[op.length])))return 3;
            return 99;};
          let roots=Array.from(document.querySelectorAll("[role=listbox],[data-testid='floater-container'],[class*='loater'],[class*='ropdown'],[class*='enu']")).filter(vis);
          if(!roots.length)roots=[document.body];      // last resort, still precise-match only
          document.querySelectorAll('[data-ap-pick]').forEach(e=>e.removeAttribute('data-ap-pick'));
          let best=null;
          for(const root of roots){
            root.querySelectorAll('[role=option],li,div,span,p,a').forEach(n=>{
              if(!vis(n))return; const t=txt(n); if(!t||t.length>60)return;
              if(n.children.length>1)return;
              if(n.tagName==='INPUT'||n.querySelector('input,textarea'))return;
              let cur=''; try{cur=getComputedStyle(n).cursor;}catch(e){}
              if(!(cur==='pointer'||(n.getAttribute&&n.getAttribute('role')==='option')))return;
              const nt=norm(t);
              for(const w of want){const tr=tierOf(nt,w); if(tr<99&&(!best||tr<best.tier))best={tier:tr,node:n,text:t};}
            });
          }
          if(best){best.node.setAttribute('data-ap-pick','1');best.node.scrollIntoView({block:'center'});return best.text;}
          return null;
        }"""
        try:
            hit = page.evaluate(pick_js, [[str(v) for v in (values or []) if v]])
        except Exception:
            hit = None
        if hit:
            try:
                self._click_robust(page.locator('[data-ap-pick="1"]').first)
                try:
                    page.evaluate("()=>document.querySelectorAll('[data-ap-pick]').forEach(e=>e.removeAttribute('data-ap-pick'))")
                except Exception:
                    pass
                self._say(f"    · dropdown → ‘{hit}’")
                return True
            except Exception:
                pass
        # 3) fallback. STRICT (geography) → NEVER guess the first option; leave it blank for
        #    the operator and close the list. Non-strict → first option as before.
        if strict:
            self._say(f"    · dropdown — no match for {values}; left blank (won't guess a wrong value)")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        if opts:
            try:
                self._click_robust(page.locator(f'[data-ap-opt="{opts[0]["idx"]}"]').first)
                self._say(f"    · dropdown → ‘{opts[0]['text']}’ (first option)")
                return True
            except Exception:
                pass
        return False

    def _is_counter(self, loc) -> bool:
        """A +/- stepper widget (e.g. MMT 'Number of beds', 'Max adults') — the value input
        is disabled; you change it by clicking plus/minus, not typing."""
        try:
            return loc.evaluate("""el => {
                if (el.getAttribute && el.getAttribute('data-test-id')==='counter-input') return true;
                let c = el.parentElement;
                for (let i=0;i<6 && c;i++){
                    if (c.querySelector && (c.querySelector('[data-test-id=counter-plus-icon]')
                        || c.querySelector('[class*=plus]'))) return true;
                    c=c.parentElement; }
                return false;
            }""") is True
        except Exception:
            return False

    def _set_counter(self, loc, target) -> bool:
        """Drive a +/- stepper to `target` by clicking plus/minus. Bails if a click doesn't
        move the value (the stepper is disabled — usually a gating field isn't set yet)."""
        try:
            target = int(float(str(target).strip()))
        except Exception:
            return False
        find_btn = """(el, dir) => {
            let c = el.parentElement;
            for (let i=0;i<6 && c;i++){
                if (c.querySelector && c.querySelector('[data-test-id=counter-plus-icon]')) break;
                c=c.parentElement; }
            if(!c) return null;
            const sel = dir>0 ? '[data-test-id=counter-plus-icon]' : '[data-test-id=counter-minus-icon]';
            let b = c.querySelector(sel);
            if(!b){ b = c.querySelector(dir>0 ? '[class*=plus]' : '[class*=minus]'); }
            return b;
        }"""
        read = "el => { const v=(el.value||'').trim(); return v===''?0:Number(v); }"
        prev = None
        for _ in range(80):
            try:
                cur = int(loc.evaluate(read))
            except Exception:
                return False
            if cur == target:
                return True
            if prev is not None and cur == prev:
                return False                          # last click didn't move it → disabled
            prev = cur
            try:
                h = loc.evaluate_handle(find_btn, 1 if target > cur else -1)
                el = h.as_element()
                if el is None:
                    return False
                el.click()
            except Exception:
                return False
            self.rt.think(0.12, 0.25)
        return False

    def _select_robust(self, loc, val) -> bool:
        """Set a dropdown to `val` whether it's a native <select> (by value/label/
        partial option text) or a custom dropdown (open it, click the matching option).
        The LLM sometimes passes the scraped 'value | text' pair (e.g. '13:00 | 13:00')
        — try each part."""
        raw = str(val)
        cands = []
        if "|" in raw:
            cands += [p.strip() for p in raw.split("|") if p.strip()]
        cands.append(raw.strip())
        seen, cleaned = set(), []
        for c in cands:                               # dedupe, keep order
            if c and c not in seen:
                seen.add(c); cleaned.append(c)
        try:
            tag = loc.evaluate("e => e.tagName.toLowerCase()")
        except Exception:
            tag = ""
        if tag == "select":
            for c in cleaned:
                for how in ("value", "label"):
                    try:
                        loc.select_option(**{how: c}, timeout=3000); return True
                    except Exception:
                        pass
            for c in cleaned:                         # match by exact/partial option text
                try:                                  # case-INSENSITIVE: the LLM/enum often
                    opt = loc.evaluate(               # passes 'queen' for an option 'Queen'
                        "(e,v)=>{ const lv=(v||'').toLowerCase().trim(); "
                        "for(const o of e.options){ const ot=(o.textContent||'').toLowerCase().trim(); "
                        "if((o.value||'').toLowerCase().trim()===lv || ot===lv || ot.includes(lv)) "
                        "return o.value; } return null; }", c)
                    if opt is not None:
                        loc.select_option(value=opt, timeout=3000); return True
                except Exception:
                    pass
            return False
        # custom dropdown: open it, diff the newly-revealed options, click the match (else
        # the first real option). DOM-agnostic — works without knowing the option markup.
        if self._open_and_pick(loc, cleaned):
            return True
        # legacy fallbacks (dropdown may already be open from _open_and_pick's click)
        try:
            for c in cleaned:
                if self._click_text_option(c):
                    return True
            if self._pick_first_option():
                return True
        except Exception:
            pass
        return False

    def _resolve_photo_paths(self, photos) -> list[str]:
        """Turn the profile's photos into LOCAL file paths Playwright can upload.

        Each Photo carries a local `path` and/or a remote `url`. The MIS only ever stores
        S3 URLs (there is never a file in a local folder on the host), so a `url` that
        isn't downloaded means an EMPTY upload queue — and photo-gated OTA steps (Agoda's
        'Add at least 3 photos to continue') never advance. So: use a real local file if
        present, otherwise download the URL once into a temp cache (keyed by URL hash, so
        re-runs don't re-fetch) and return that path."""
        import hashlib
        import tempfile
        import urllib.parse
        import urllib.request

        out: list[str] = []
        # parallel metadata, so the upload routing knows each photo's MIS tag (caption) and
        # which room it belongs to (room_type) — both come straight from the MIS, no ML needed.
        self._photo_meta = []
        cache_dir = os.path.join(tempfile.gettempdir(), "accounts_pilot_photos")
        os.makedirs(cache_dir, exist_ok=True)
        photos = list(photos or [])

        def _keep(local_path, ph):
            out.append(local_path)
            self._photo_meta.append({
                "path": local_path,
                "caption": (getattr(ph, "caption", None) or "").strip(),
                "room_type": (getattr(ph, "room_type", None) or "").strip(),
            })

        # how many actually need fetching (so we can show progress instead of dead silence)
        to_fetch = [ph for ph in photos
                    if not (getattr(ph, "path", None) and os.path.exists(getattr(ph, "path", None)))
                    and getattr(ph, "url", None)]
        if to_fetch:
            self._say(f"  · photos: fetching {len(to_fetch)} image(s) from the MIS URLs "
                      "(one-time; cached for next run)…")
        downloaded = 0
        fetch_i = 0
        for ph in photos:
            path = getattr(ph, "path", None)
            url = getattr(ph, "url", None)
            # 1) a real local file always wins
            if path and os.path.exists(path):
                _keep(path, ph)
                continue
            # 2) otherwise pull the remote url (S3 / CDN) once, cached by url hash
            if not url:
                continue
            ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"
            dest = os.path.join(cache_dir, hashlib.sha1(url.encode("utf-8")).hexdigest() + ext)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                _keep(dest, ph)
                continue
            fetch_i += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (AccountsPilot)"})
                with urllib.request.urlopen(req, timeout=15) as r:   # short timeout so one dead url can't freeze fill
                    data = r.read()
                if not data:
                    raise ValueError("empty body")
                with open(dest, "wb") as f:
                    f.write(data)
                _keep(dest, ph)
                downloaded += 1
                self._say(f"  · photo {fetch_i}/{len(to_fetch)} downloaded ({len(data)//1024} KB)")
            except Exception as e:
                short = (url[:60] + "…") if len(url) > 60 else url
                self._say(f"  ⚠ photo {fetch_i}/{len(to_fetch)}: couldn't download {short} "
                          f"({type(e).__name__}) — skipped")
        if not out:
            if to_fetch:
                self._say("  ⚠ photos: none of the MIS URLs produced an image — the photo gate will "
                          "block. Check the URLs are public (not expired signed links).")
        elif downloaded:
            self._say(f"  ✓ photos — {len(out)} image(s) ready "
                      f"({downloaded} freshly downloaded, {len(out) - downloaded} from cache)")
        else:
            self._say(f"  ✓ photos — {len(out)} image(s) ready (all from cache)")
        return out

    def _photo_preflight(self) -> None:
        """Warn (in the live log) about photos the OTAs will reject — BEFORE we try to
        upload — so the operator fixes the JSON instead of debugging an OTA toast.
        Common OTA rules: each image 100KB–30MB, >=10 photos, landscape preferred."""
        MIN_KB, MAX_MB, MIN_COUNT = 100, 30, 10
        listed = self._photo_paths
        on_disk = self._all_photo_paths
        if not listed:
            self._say("  ⚠ photos: none in the JSON — OTAs that require photos will block "
                      "(add ≥10 images, each 100KB–30MB, landscape).")
            return
        missing = [p for p in listed if p not in on_disk]
        too_small, too_big, portrait, unreadable = [], [], 0, 0
        for p in on_disk:
            try:
                kb = os.path.getsize(p) / 1024
                if kb < MIN_KB:
                    too_small.append((os.path.basename(p), kb))
                elif kb > MAX_MB * 1024:
                    too_big.append((os.path.basename(p), kb / 1024))
            except Exception:
                continue
            try:
                from PIL import Image
                w, h = Image.open(p).size
                if w <= h:
                    portrait += 1
            except Exception:
                unreadable += 1                        # PIL absent or not an image — can't check dims
        n = len(on_disk)
        if n < MIN_COUNT:
            self._say(f"  ⚠ photos: {n} usable — most OTAs need ≥{MIN_COUNT} (MakeMyTrip rejects fewer).")
        if missing:
            self._say(f"  ⚠ photos: {len(missing)} path(s) not found on disk (skipped): "
                      f"{', '.join(os.path.basename(m) for m in missing[:3])}"
                      + (" …" if len(missing) > 3 else ""))
        # NOTE: orientation, portrait→landscape, oversize, and odd formats are all
        # auto-corrected at upload time by the shared photo processor (accounts_pilot/photos.py),
        # so these are informational — NOT blockers the operator must fix in the MIS.
        autofix = []
        if portrait and not unreadable:
            autofix.append(f"{portrait} portrait→landscape")
        if too_big:
            autofix.append(f"{len(too_big)} over {MAX_MB}MB→resized")
        if too_small:
            autofix.append(f"{len(too_small)} under {MIN_KB}KB→upscaled")
        if autofix:
            self._say(f"  · photos: {', '.join(autofix)} — the photo-fixer will auto-correct these on upload")
        if n >= MIN_COUNT and not missing:
            self._say(f"  ✓ photos pre-flight: {n} images ready (auto-fixed to upright/landscape/sized).")

    def _photos_file_input(self):
        """The Photos upload page has a (often hidden) <input type=file>. Find it across
        frames — set_input_files works even when it's visually hidden behind a dropzone."""
        for ctx in self._frames():
            try:
                inp = ctx.locator("input[type=file]").first
                if inp.count() > 0:
                    return inp
            except Exception:
                pass
        return None

    def _ensure_filechooser(self):
        """Register a Playwright file-chooser handler so ANY click that opens an upload
        dialog (e.g. 'Add photos') is auto-fed the JSON photos — the native OS file
        dialog never appears and never blocks. This is the robust upload path."""
        if getattr(self, "_fc_registered", False):
            return
        try:
            page = self.rt.page

            def _on_chooser(fc):
                # feed from the remaining-photo queue, capped per batch (MMT takes 20 at a time)
                q = getattr(self, "_photo_queue", None)
                if q is None:
                    q = list(getattr(self, "_all_photo_paths", []) or [])
                    self._photo_queue = q
                if not q:
                    return
                batch = getattr(self, "_photo_batch", 0) or len(q)
                files = q[:batch]
                del q[:batch]
                files = self._prep_files(files)        # EXIF-orient → landscape → sized JPEG (all OTAs)
                if not files:
                    return
                try:
                    fc.set_files(files)
                    self._say(f"  ✓ photos — fed {len(files)} file(s) to the dialog "
                              f"({len(q)} left)")
                except Exception:
                    try:
                        fc.set_files(files[0])
                    except Exception:
                        pass

            page.on("filechooser", _on_chooser)
            self._fc_registered = True
        except Exception:
            pass

    def _upload_photos(self, inp, paths) -> bool:
        """Upload the property photos from the JSON to the page's file input."""
        good = self._prep_files([p for p in (paths or []) if p and os.path.exists(p)])
        if not good:
            self._say("  · photos: nothing on disk to upload (add paths to photos[] in the JSON)")
            return False
        try:
            inp.set_input_files(good)                 # multiple at once
            self._say(f"  ✓ photos — uploaded {len(good)} file(s); waiting for processing…")
        except Exception:
            try:
                inp.set_input_files(good[0])          # input not multiple → first only
                self._say("  ✓ photos — uploaded 1 file (input accepts one at a time)")
            except Exception as e:
                self._say(f"  – photos upload failed: {type(e).__name__}: {e}")
                return False
        self.rt.think(4.0, 6.0)                       # let thumbnails generate
        return True

    def _click_add_more_photos(self) -> bool:
        """Click an 'Add more photos' control to reveal the next upload slot (OTAs that
        cap a batch, e.g. MMT's 20-at-a-time, need this between batches)."""
        for lab in ("Add more photos", "Add more images", "Add more", "Upload more",
                    "Add photos", "Upload photos", "+ Add more", "Add more media"):
            loc = self._locate("text", lab)
            if loc is not None:
                try:
                    self._click_robust(loc); return True
                except Exception:
                    pass
        return False

    def _upload_photos_chunked(self) -> bool:
        """Upload all photos, respecting a per-batch cap. MakeMyTrip only accepts 20 images
        per upload, so feed 20, click 'Add more', feed the next 20, until all are uploaded.
        Other OTAs take everything in one go (batch = all)."""
        good = [p for p in (getattr(self, "_all_photo_paths", []) or []) if os.path.exists(p)]
        if not good:
            self._say("  · photos: nothing on disk to upload (add paths to photos[] in the JSON)")
            return False
        batch = 20 if self.ota == "makemytrip" else len(good)
        self._photo_batch = batch
        self._photo_queue = list(good)
        if len(good) > batch:
            self._say(f"  · photos: {len(good)} total — uploading in batches of {batch}")
        rounds = 0
        while self._photo_queue and rounds < 16:
            rounds += 1
            finp = self._photos_file_input()
            if finp is None:
                self._say(f"  · photos: no upload input here ({len(self._photo_queue)} left)")
                break
            chunk = self._prep_files(self._photo_queue[:batch])   # orient → landscape → sized
            try:
                finp.set_input_files(chunk)
                del self._photo_queue[:batch]
                self._say(f"  ✓ photos — uploaded {len(chunk)} ({len(self._photo_queue)} left)")
            except Exception as e:
                self._say(f"  – photos upload failed: {type(e).__name__}: {e}")
                break
            self.rt.think(4.0, 6.0)                   # let thumbnails generate
            if not self._photo_queue:
                break
            if not self._click_add_more_photos():     # reveal the next batch's input
                self._say(f"  · photos: uploaded a batch; couldn't find 'Add more' for the "
                          f"remaining {len(self._photo_queue)} — add them manually.")
                break
            self.rt.think(1.5, 2.5)
        self._photo_queue = []                        # done — chooser handler won't re-feed
        return True

    @staticmethod
    def _photo_norm(s) -> str:
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())

    @staticmethod
    def _agoda_photo_ok(path) -> bool:
        """Agoda rejects anything that isn't jpg/png, is >10MB, or is under 800×600.
        Filtering to these BEFORE upload is the difference between the ≥3 gate clearing
        and Agoda silently dropping the batch ('Some photos weren't added')."""
        try:
            if os.path.splitext(path)[1].lower() not in (".jpg", ".jpeg", ".png"):
                return False
            if os.path.getsize(path) > 10 * 1024 * 1024:
                return False
            try:
                from PIL import Image
                w, h = Image.open(path).size
                if w < 800 or h < 600:
                    return False
            except Exception:
                pass                                   # PIL missing → can't check dims, allow
            return True
        except Exception:
            return False

    def _agoda_prep_image(self, path):
        """Prepare ONE image for upload via the shared OTA photo processor (EXIF-orient →
        landscape white-pad → resize into the size window → clean JPEG). Returns the
        prepared path or None. See accounts_pilot/photos.py."""
        return prepare_photo(path)

    def _prep_files(self, paths) -> list:
        """Run a batch of images through the shared OTA photo processor before upload — so
        EVERY OTA (Agoda, MakeMyTrip, Booking, …) uploads upright, landscape, sized JPEGs."""
        return prepare_many(paths)

    def _expand_agoda_rooms(self, page) -> None:
        """Reveal Agoda's collapsed 'Room photos' section so each room's dropzone appears.
        The expander is a button/toggle (not one of the data-element-name='add-photos'
        dropzones), so clicking buttons labelled 'Add photos' opens the room sections."""
        try:
            if page.get_by_text("Room photos", exact=False).count() == 0:
                return
            cand = page.locator('button:has-text("Add photos"), [role="button"]:has-text("Add photos")')
            for i in range(min(cand.count(), 4)):
                try:
                    cand.nth(i).scroll_into_view_if_needed(timeout=2000)
                    cand.nth(i).click(timeout=2500)
                    self.rt.think(1.0, 1.8)
                except Exception:
                    continue
        except Exception:
            pass

    def _fill_agoda_photos(self) -> bool:
        """Agoda's Photos page is NOT one upload box — it has a separate dropzone per
        section: 'Property Photos' (hard-gated at ≥3 to continue) plus one per room type
        (Deluxe, Deluxe Suite, …). The generic uploader dumped ALL ~90 photos into the
        first input (Agoda choked, kept ~2, gate never cleared) and never fed the rooms.

        Here we route by the tags the MIS already attaches to every image — no ML needed:
          • property-tagged photos (Exterior / Reception-Lobby / Common / Amenities …)
            → the Property dropzone, best 'main photo' (Exterior/Entrance) first;
          • each room's photos (Photo.room_type, set from the MIS room_type_name)
            → that room's dropzone, mapped by the wizard's room order.
        A capped handful per section clears the gate and gives every room real photos."""
        all_meta = [m for m in (getattr(self, "_photo_meta", None) or [])
                    if m.get("path") and os.path.exists(m["path"])]
        if not all_meta:                               # no meta → fall back to the flat path list
            all_meta = [{"path": p, "caption": "", "room_type": ""}
                        for p in (getattr(self, "_all_photo_paths", []) or []) if os.path.exists(p)]
        if not all_meta:
            self._say("  · photos: nothing resolved from the MIS URLs")
            return False

        # property pool = photos with NO room_type, best 'main photo' (Exterior/Entrance) first
        PRIORITY = ["exterior", "facade", "entrance", "building", "reception", "lobby",
                    "common", "amenit", "pool", "restaurant", "room", "bath"]

        def _prio(cap):
            c = (cap or "").lower()
            return next((i for i, kw in enumerate(PRIORITY) if kw in c), len(PRIORITY))

        property_pool = [m["path"] for m in sorted(
            [m for m in all_meta if not m.get("room_type")], key=lambda m: _prio(m.get("caption")))]
        room_buckets: dict = {}
        for m in all_meta:
            if m.get("room_type"):
                room_buckets.setdefault(self._photo_norm(m["room_type"]), []).append(m["path"])
        leftover_room = [m["path"] for m in all_meta if m.get("room_type")]
        if not property_pool:                          # untagged data → use anything so the gate clears
            property_pool = [m["path"] for m in all_meta]

        def _prep_all(pool):
            """Prepare EVERY image in the pool (orient → landscape → sized JPEG); skip unusable.
            No cap — Agoda accepts a large multi-file set in one go, so we upload them all."""
            out = []
            for p in pool:
                q = self._agoda_prep_image(p)
                if q and q not in out:
                    out.append(q)
            return out

        used_buckets: set = set()

        def _match_room(head: str):
            key = self._photo_norm(head)
            cands = [bk for bk in room_buckets if bk not in used_buckets]
            for bk in cands:                           # exact name match (Deluxe → deluxe)
                if bk == key:
                    used_buckets.add(bk); return bk
            for bk in cands:                           # substring either way
                if bk and (bk in key or key in bk):
                    used_buckets.add(bk); return bk
            if cands:                                  # else the largest unused bucket
                bk = max(cands, key=lambda b: len(room_buckets[b]))
                used_buckets.add(bk); return bk
            return None

        try:
            page = self.rt.page
        except Exception as e:
            self._say(f"  – photos: no page ({type(e).__name__})")
            return False

        done: set = set()

        def _feed_round() -> int:
            """Feed every dropzone currently on the page that we haven't filled yet."""
            try:
                zones = page.locator('[data-element-name="add-photos"]')
                n = zones.count()
                heads = [t.strip() for t in page.locator("h2").all_inner_texts()]
            except Exception:
                return 0
            got = 0
            for i in range(n):
                head = heads[i] if i < len(heads) else ""
                is_prop = ("propert" in head.lower()
                           or (i == 0 and not any("propert" in (h or "").lower() for h in heads)))
                label = head or ("Property" if is_prop else f"room {i}")
                if label in done:
                    continue
                if is_prop:
                    base = property_pool
                else:
                    bk = _match_room(head)
                    if bk:
                        base = room_buckets.get(bk, [])
                    else:                              # no bucket left → hand it the remaining room pool
                        base = list(leftover_room)
                        leftover_room.clear()
                if not base:
                    continue
                self._say(f"  · photos — '{label}': preparing {len(base)} image(s)…")
                chunk = _prep_all(base)                # ALL of them, no cap
                if not chunk:
                    continue
                try:
                    zones.nth(i).locator('input[type=file]').first.set_input_files(chunk)
                    done.add(label); got += 1
                    self._say(f"  ✓ photos — '{label}': uploaded ALL {len(chunk)} image(s)")
                    # many files take longer to upload — wait proportionally before moving on
                    self.rt.think(min(4 + len(chunk) * 0.4, 30), min(7 + len(chunk) * 0.5, 38))
                except Exception as e:
                    self._say(f"  – photos '{label}': {type(e).__name__}: {e}")
            return got

        self._say(f"  · photos: routing by MIS tags "
                  f"({len(property_pool)} property, {len(leftover_room)} room images)")
        total = _feed_round()                          # property + any rooms already visible
        # let the property '≥3' gate settle (uploads are async)
        try:
            gate = page.get_by_text("at least 3 photos", exact=False)
            for _ in range(15):
                if gate.count() == 0:
                    break
                self.rt.think(2.0, 2.5)
            self._say("  ✓ photos — property gate cleared" if gate.count() == 0
                      else "  · photos — gate still showing; uploads may still be in flight")
        except Exception:
            pass
        # rooms are often collapsed or render late — reveal them, then feed what's new
        try:
            self._expand_agoda_rooms(page)
        except Exception:
            pass
        total += _feed_round()
        if total:
            self._say(f"  ✓ photos — filled {len(done)} section(s): {', '.join(sorted(done))}")
        return total > 0

    def _norm_date(self, s) -> str:
        """Normalise a date to yyyy-mm-dd (what <input type=date> requires), accepting
        dd-mm-yyyy, yyyy-mm-dd, dd/mm/yyyy, etc. so the JSON format doesn't matter."""
        import re
        s = (str(s) or "").strip()
        m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)        # yyyy-mm-dd
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", s)        # dd-mm-yyyy
        if m:
            return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        return s

    def _find_visible(self, ctx, sel):
        """Return the first VISIBLE editable element matching sel (a duplicate hidden
        field from the other tab/path is common — skip those)."""
        try:
            loc = ctx.locator(sel)
            n = loc.count()
            for i in range(min(n, 8)):
                el = loc.nth(i)
                try:
                    if el.is_visible() and el.is_editable():
                        return el
                except Exception:
                    pass
            return loc.first if n > 0 else None
        except Exception:
            return None

    def _set_react_input(self, loc, val) -> bool:
        """Set a (possibly React-controlled) input so the value STICKS. Try Playwright
        fill; if it doesn't take (React reverts it — common for <input type=date>), set
        via the native value setter + bubbling input/change events that React listens for."""
        val = str(val)
        try:
            loc.fill(val, timeout=3000)
        except Exception:
            pass
        try:
            if (loc.input_value() or "").strip() == val:
                return True
        except Exception:
            pass
        try:
            loc.evaluate(
                "(el,v)=>{ const d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
                " (d&&d.set ? d.set : function(x){el.value=x;}).call(el,v);"
                " el.dispatchEvent(new Event('input',{bubbles:true}));"
                " el.dispatchEvent(new Event('change',{bubbles:true})); }", val)
            try:
                return bool((loc.input_value() or "").strip())
            except Exception:
                return True
        except Exception:
            return False

    def _set_react_select(self, loc, want) -> bool:
        """Set a React-controlled <select> so the value STICKS and React's onChange fires
        (so a dependent dropdown like State repopulates for the chosen Country). Playwright's
        select_option sets the option but a controlled <select> can revert it; instead match
        an option by value/text and set via the NATIVE value setter + bubbling input/change.
        `want` may be a 'a|b|c' candidate list (most specific first). Exact match wins over a
        substring match (so 'IN' picks India by value, not 'Argentina' by includes)."""
        cands = [c.strip() for c in str(want).split("|") if c.strip()] or [str(want)]
        try:
            val = loc.evaluate(
                "(el, cands) => {"
                " const norm = s => (s||'').toLowerCase().trim();"
                " const setVal = v => { const d=Object.getOwnPropertyDescriptor("
                "   window.HTMLSelectElement.prototype,'value');"
                "   (d&&d.set?d.set:function(x){el.value=x;}).call(el, v);"
                "   el.dispatchEvent(new Event('input',{bubbles:true}));"
                "   el.dispatchEvent(new Event('change',{bubbles:true})); };"
                " for (const c of cands) { const nc=norm(c);"     # exact value/text first
                "  for (const o of el.options) {"
                "   if (norm(o.value)===nc || norm(o.textContent)===nc) { setVal(o.value); return o.value; } } }"
                " for (const c of cands) { const nc=norm(c);"     # then a substring of the label
                "  for (const o of el.options) {"
                "   if (o.value && norm(o.textContent).includes(nc)) { setVal(o.value); return o.value; } } }"
                " return null; }", cands)
            return val is not None
        except Exception:
            return False

    def _fill_kyp(self, profile_data) -> bool:
        """Deterministic 'Partner verification' (Know Your Partner) fill — the page has
        STABLE name= attributes, so we don't need the LLM. Picks the INDIVIDUAL option
        (avoids the business path's 25%-owner DOB demands), then fills owner first/last
        name and DOB. The DOB is an <input type=date> → value MUST be yyyy-mm-dd."""
        comp = (profile_data or {}).get("compliance", {}) or {}
        contact = (profile_data or {}).get("contact", {}) or {}
        full = (contact.get("full_name") or "").strip().split()
        first = comp.get("owner_first_name") or (full[0] if full else "")
        last = comp.get("owner_last_name") or (" ".join(full[1:]) if len(full) > 1 else "")
        dob = self._norm_date(comp.get("owner_dob", ""))     # -> yyyy-mm-dd for type=date
        company = comp.get("legal_entity_name", "")
        did = 0
        # 1) owner type -> individual (this reveals the owner-name fields)
        for ctx in self._frames():
            try:
                s = ctx.locator("select[name='owner_type']").first
                if s.count() > 0 and s.is_visible():
                    for kind, v in (("value", "individual"),
                                    ("label", "I'm an individual running a business")):
                        try:
                            s.select_option(**{kind: v}, timeout=4000)
                            try:
                                s.dispatch_event("change")
                            except Exception:
                                pass
                            did += 1; break
                        except Exception:
                            pass
                    break
            except Exception:
                pass
        self.rt.think(1.0, 1.8)                              # let the owner fields render
        # 2) fill owner identity by stable name= (React-safe: native setter + events,
        #    so the <input type=date> DOB actually sticks)
        targets = (("first_name_of_owners", first), ("last_name_of_owners", last),
                   ("dob_of_owners", dob))
        for ctx in self._frames():
            for name, val in targets:
                if not val:
                    continue
                f = self._find_visible(ctx, f"input[name='{name}']")
                if f is not None and self._set_react_input(f, val):
                    did += 1
        # 3) a company / legal-name field if this variant shows one
        if company:
            for ctx in self._frames():
                try:
                    cn = ctx.locator("input[name='owner_company_name'], "
                                     "#automation_id_kyp_owner_company_name").first
                    if cn.count() > 0 and cn.is_visible():
                        cn.fill(company, timeout=4000); did += 1; break
                except Exception:
                    pass
        if did:
            self._say(f"  ✓ KYP — individual owner {first} {last}, dob {dob} ({did} field(s) set)")
        return did > 0

    # MMT 'Type of Hotel' card → our PropertyType. The sub-type cards MMT offers are
    # Hotel / Resort / Lodge / Guest House / Palace / Houseboat / Motel.
    _MMT_CATEGORY = {
        "hotel": "Hotel", "apartment": "Homestays & Villas", "aparthotel": "Hotel",
        "guesthouse": "Hotel", "homestay": "Homestays & Villas", "bnb": "Hotel",
        "hostel": "Hotel", "resort": "Hotel", "villa": "Homestays & Villas",
        "holiday_home": "Homestays & Villas",
    }
    _MMT_SUBTYPE = {
        "hotel": "Hotel", "aparthotel": "Hotel", "guesthouse": "Guest House",
        "bnb": "Guest House", "hostel": "Lodge", "resort": "Resort",
    }

    def _fill_mmt_property_type(self, profile_data) -> bool:
        """MMT 'Which property type would you like to list?' — a CUSTOM card UI (divs, not
        form controls), so the scraper/LLM can't see it. Pick the top-level category and
        the 'Type of Hotel' sub-type from the JSON, then click 'List Property'."""
        page = self.rt.page
        try:
            has_radio = page.locator(
                "[data-test-id='onboarding-propertySelection-propertyTypeRadio']").count() > 0
        except Exception:
            has_radio = False
        if not has_radio:
            return False                                  # not this page

        ptype = str(profile_data.get("property_type", "hotel")).lower()
        category = self._MMT_CATEGORY.get(ptype, "Hotel")
        subtype = self._MMT_SUBTYPE.get(ptype, "Hotel")

        # 1) top-level category card (Hotel vs Homestays & Villas) — pick by its <h3>
        try:
            cat = page.locator(
                "[data-test-id='onboarding-propertySelection-propertyTypeRadio'] > div"
            ).filter(has=page.locator(f"h3:text-is('{category}')")).first
            if cat.count():
                self._click_robust(cat)
                self._say(f"  · MMT property type → category ‘{category}’")
                self.rt.think(0.4, 0.9)
        except Exception:
            pass

        # 2) 'Type of Hotel' sub-type card — pick by its exact <span> label
        try:
            sub = page.locator(
                "[data-test-id='onboarding-propertySelection-subPropertyTypeRadio'] > div"
            ).filter(has=page.locator(f"span:text-is('{subtype}')")).first
            if sub.count():
                self._click_robust(sub)
                self._say(f"  · MMT property type → sub-type ‘{subtype}’")
                self.rt.think(0.4, 0.9)
        except Exception:
            pass

        # 3) advance — 'List Property' (enabled once a sub-type is chosen)
        for lab in ("List Property", "Continue", "Next", "Proceed"):
            loc = self._locate("text", lab)
            if loc is not None:
                try:
                    self._click_robust(loc)
                    self._say(f"  · MMT → clicked ‘{lab}’")
                    return True
                except Exception:
                    pass
        return False

    @staticmethod
    def _digits10(phone: str) -> str:
        """Indian 10-digit subscriber number — the +91 dropdown supplies the country code,
        so feeding '+918076707050' into the number box doubles it. Strip to the last 10."""
        import re
        d = re.sub(r"\D", "", phone or "")
        if len(d) > 10 and d.startswith("91"):
            d = d[2:]
        return d[-10:] if len(d) >= 10 else d

    def _mmt_pick_dropdown(self, trigger, value, label) -> bool:
        """MMT custom dropdowns ('Select rating' / 'Select a year') aren't <select>s — they
        open a list of divs/li. Open the trigger, then click the option matching `value`."""
        page = self.rt.page
        value = str(value)
        try:
            if trigger.count() == 0 or not trigger.first.is_visible():
                return False                          # already chosen / not present
            trigger.first.scroll_into_view_if_needed(timeout=3000)
            self._click_robust(trigger.first)
            self.rt.think(0.5, 0.9)
        except Exception:
            return False
        for sel in (f"[role='option']:text-is('{value}')",
                    f"li:text-is('{value}')",
                    f"[role='listbox'] li:has-text('{value}')",
                    f"ul[role='listbox'] >> text='{value}'",
                    f"[class*='option']:has-text('{value}')",
                    f"li:has-text('{value}')"):
            try:
                opt = page.locator(sel).first
                if opt.count() and opt.is_visible():
                    self._click_robust(opt)
                    self._say(f"  · MMT {label} → {value}")
                    self.rt.think(0.3, 0.6)
                    return True
            except Exception:
                pass
        # fallback: type-to-filter then Enter (many custom selects support it)
        try:
            page.keyboard.type(value, delay=60)
            self.rt.think(0.4, 0.7)
            page.keyboard.press("Enter")
            self._say(f"  · MMT {label} → typed {value}")
            return True
        except Exception:
            return False

    def _fill_mmt_basic_info(self, profile_data, profile) -> bool:
        """MMT onboarding step 1 ('Basic Info'). The scraper only sees the 4 text inputs;
        the star-rating + 2 year dropdowns and the channel-manager radio are custom divs.
        Fill it all deterministically from the JSON (name = property name, NOT contact)."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "")
        except Exception:
            return False
        if "Hotel Star Rating" not in body and "basic-info" not in (page.url or ""):
            return False                              # not this page

        name = profile_data.get("display_name") or getattr(profile, "display_name", "") or ""
        email = (profile_data.get("contact", {}) or {}).get("email", "")
        phone = (profile_data.get("contact", {}) or {}).get("phone", "")
        star = profile_data.get("star_rating")
        did = 0

        # text inputs (by placeholder — stable across MMT's nested generic divs)
        for ph, val in (("Enter the full name", name),
                        ("Enter email ID", email),
                        ("Enter number", self._digits10(phone))):
            if not val:
                continue
            el = self._find_visible(page, f"input[placeholder='{ph}']")
            if el is not None and self._set_react_input(el, val):
                did += 1

        # star rating dropdown
        if star:
            if self._mmt_pick_dropdown(page.get_by_text("Select rating", exact=True),
                                       star, "star rating"):
                did += 1

        # two 'Select a year' dropdowns — #0 = built, #1 = accepting bookings since
        years = page.get_by_text("Select a year", exact=True)
        try:
            ny = years.count()
        except Exception:
            ny = 0
        built = str(profile_data.get("year_built") or "2026")
        since = str(profile_data.get("accepting_since") or "2026")
        if ny >= 1 and self._mmt_pick_dropdown(years.nth(0), built, "year built"):
            did += 1
        # re-query: the first pick may have re-rendered the list
        years = page.get_by_text("Select a year", exact=True)
        if years.count() and self._mmt_pick_dropdown(years.nth(0), since, "accepting since"):
            did += 1

        # channel manager radio → 'No' (no CM integration during onboarding)
        try:
            no = page.locator(
                "xpath=//*[contains(normalize-space(.),'channel manager')]"
                "/following::*[normalize-space(text())='No'][1]").first
            if no.count() and no.is_visible():
                self._click_robust(no)
                self._say("  · MMT channel manager → No")
                did += 1
        except Exception:
            pass

        if did:
            self._say(f"  ✓ MMT Basic Info — name ‘{name}’, {star}★, "
                      f"phone {self._digits10(phone)} ({did} field(s))")
        return did > 0

    def _fill_mmt_location(self, profile_data) -> bool:
        """MMT onboarding step 2 ('Location'). A MUI Autocomplete over Google Places — a
        REAL <input placeholder='Search here'> (not Booking's closed shadow root). Type the
        address + pick the first prediction; that reveals House/Pincode + a terms checkbox
        on the SAME page, which 'save and continue' needs filled. Handle all of it."""
        page = self.rt.page
        try:
            box = page.locator(
                "[data-test-id='onboarding-location-locationSearch'] input").first
            if box.count() == 0:
                return False
        except Exception:
            return False

        addr = profile_data.get("address", {}) or {}
        query = ", ".join(p for p in (addr.get("line1"), addr.get("city"),
                                      addr.get("state")) if p)

        # A) search box still empty → type + pick the first prediction (this transforms
        #    the page into the detail form). Do this ONCE; next pass fills the details.
        try:
            already = (box.input_value() or "").strip()
        except Exception:
            already = ""
        if box.is_visible() and not already and query:
            try:
                self._click_robust(box)
                box.fill("")
                page.keyboard.type(query, delay=75)   # char-by-char so Places fires
                self.rt.think(1.3, 1.9)               # wait for predictions
            except Exception:
                return False
            picked = False
            for sel in ("ul[role='listbox'] li[role='option']",
                        "[role='listbox'] [role='option']",
                        ".MuiAutocomplete-popper li", ".pac-container .pac-item"):
                try:
                    opt = page.locator(sel).first
                    if opt.count() and opt.is_visible():
                        self._click_robust(opt); picked = True; break
                except Exception:
                    pass
            if not picked:
                try:
                    page.keyboard.press("ArrowDown"); self.rt.think(0.3, 0.5)
                    page.keyboard.press("Enter")
                except Exception:
                    pass
            self._say(f"  · MMT location → ‘{query}’ (picked match)")
            self.rt.think(0.8, 1.3)
            return True                               # let the detail fields render

        # B) detail fields revealed after the pick — fill House, Pincode, tick terms.
        did = 0
        building = addr.get("line2") or profile_data.get("display_name") or addr.get("line1")
        pin = str(addr.get("postal_code") or "")
        targets = (("Please add details", building),     # House/Building/Apartment No.
                   ("Enter Pincode", pin))               # Pincode
        for ph, val in targets:
            if not val:
                continue
            el = self._find_visible(page, f"input[placeholder='{ph}']")
            if el is not None:
                try:
                    if (el.input_value() or "").strip():
                        continue                      # already filled
                except Exception:
                    pass
                if self._set_react_input(el, val):
                    did += 1
        # terms-and-conditions checkbox ('I agree…') — the LLM won't reliably tick this
        try:
            cb = page.locator(
                "xpath=//*[contains(normalize-space(.),'I agree to the terms')]"
                "//input[@type='checkbox'] | "
                "//*[contains(normalize-space(.),'terms and conditions')]/preceding::"
                "input[@type='checkbox'][1]").first
            if cb.count() == 0:
                cb = page.locator("input[type='checkbox']").first
            if cb.count() and not cb.is_checked():
                self._check_robust(cb); did += 1
                self._say("  · MMT location → agreed to terms")
        except Exception:
            pass
        if did:
            self._say(f"  ✓ MMT location details — building ‘{building}’, pincode {pin} "
                      f"({did} field(s))")
        return did > 0

    def _mmt_present_set(self, profile_data) -> set:
        """Tokens describing ONLY what this property explicitly HAS in the JSON — used to answer
        MMT amenity Yes/No. Conservative on purpose: we only say 'Yes' to an amenity that the
        JSON clearly lists; everything else is 'No'. No broad inference."""
        f = profile_data.get("facilities", {}) or {}
        toks = set()
        def add(*xs):
            for x in xs:
                if x:
                    toks.add(str(x).lower())
        if (f.get("parking") or {}).get("available"): add("parking")
        if (f.get("internet") or {}).get("wifi"):      add("wifi", "internet")
        if (f.get("breakfast") or {}).get("available"): add("breakfast")
        # facility flag → the specific amenity word(s) only (no loose synonyms)
        flagwords = {"restaurant": ["restaurant"], "bar": ["bar"], "room_service": ["room service"],
                     "spa": ["spa"], "fitness_center": ["gym", "fitness"], "swimming_pool": ["swimming pool"],
                     "laundry": ["laundry"], "airport_shuttle": ["airport"], "elevator": ["elevator", "lift"],
                     "business_center": ["business"], "ev_charging": ["ev charging"], "family_rooms": ["family"]}
        for k, ws in flagwords.items():
            if f.get(k): add(*ws)
        for x in (f.get("other") or []):                add(x)
        for x in (profile_data.get("amenities") or []): add(x)
        for rt in (profile_data.get("room_types") or []):
            for a in (rt.get("room_amenities") or []):  add(a)
        if "ac" in toks: add("air conditioning")
        if "tv" in toks: add("television")
        if any("front desk" in t or "reception" in t for t in toks): add("reception")
        if any("power backup" in t or "power" == t for t in toks): add("power backup")
        return toks

    def _amenity_present(self, name: str, toks: set) -> bool:
        """STRICT, word-level match — only 'Yes' when a JSON token genuinely names this amenity
        (shares a significant word, or a multi-word token appears in the name). Avoids loose
        substring false-positives so we never tick an amenity the operator didn't give."""
        import re
        # generic words that appear across many UNRELATED amenities — never let one of these,
        # on its own, count as a match (e.g. 'service' must not tick 'Fax service' just because
        # the JSON has 'Room Service'; 'room' must not tick 'Conference room').
        STOP = {"room", "rooms", "service", "services", "center", "centre", "area", "areas",
                "free", "paid", "hour", "hours", "call", "desk", "self", "local", "indoor",
                "outdoor", "property", "hotel", "guest", "guests", "house", "front", "and",
                "with", "the", "for", "per", "system", "station", "point", "available"}
        nl = (name or "").lower().strip()
        if not nl:
            return False
        # STRONGEST signal first: a whole multi-word JSON token literally appears in the name
        # (e.g. token 'room service' inside amenity 'Room service'). This must run BEFORE the
        # stopword early-exit, so a real amenity whose every word is generic still matches.
        twords = set()
        for t in toks:
            if len(t) > 4 and t in nl:
                return True
            for w in re.split(r"[^a-z0-9]+", t):
                if len(w) > 2 and w not in STOP:
                    twords.add(w)
        nwords = {w for w in re.split(r"[^a-z0-9]+", nl) if len(w) > 2 and w not in STOP}
        if not nwords:
            return False
        return bool(nwords & twords)                   # else a shared SIGNIFICANT word → present

    def _mmt_amenity_subdropdown(self, aid: str, name: str, profile_data: dict) -> None:
        """A 'Yes' amenity often reveals required 'Select' sub-dropdown(s) (e.g. Wifi/Parking
        Free-vs-Paid, Room-service hours). Fill them FROM THE JSON where we can (free wifi/
        parking), otherwise the first sensible option."""
        page = self.rt.page
        nl = (name or "").lower()
        f = profile_data.get("facilities", {}) or {}
        free_pref = ["Free", "Complimentary", "Free of charge", "No charge"]
        paid_pref = ["Paid", "Chargeable", "Paid (chargeable)"]
        want = []
        if "wifi" in nl or "internet" in nl:
            want = free_pref if (f.get("internet") or {}).get("free", True) else paid_pref
        elif "parking" in nl:
            want = free_pref if (f.get("parking") or {}).get("type", "free") == "free" else paid_pref
        # fill EVERY still-empty sub-dropdown within this amenity's inputs
        filled = False
        for cont in (f"[data-test-id='amenity-inputs-{aid}']",
                     f"[data-test-id='amenity-wrapper-{aid}']"):
            try:
                dds = page.locator(f"{cont} .dropdown-wrapper")
                for i in range(dds.count()):
                    dd = dds.nth(i)
                    if not dd.is_visible():
                        continue
                    if "select" not in (dd.inner_text() or "").lower():
                        continue                       # already chosen
                    self._open_and_pick(dd, want)      # JSON value if it matches, else first
                    filled = True
                    self.rt.think(0.2, 0.4)
                if filled:
                    return
            except Exception:
                pass
        # fallback to known test-ids if the container lookup found nothing
        for sel in (f"[data-test-id='amenities.{aid}.subAmenities-dropdown']",
                    f"[data-test-id='amenity-charge-type-{aid}']"):
            try:
                dd = page.locator(sel).first
                if dd.count() and dd.is_visible() and "select" in (dd.inner_text() or "").lower():
                    self._open_and_pick(dd, want); return
            except Exception:
                pass

    def _mmt_answer_visible_amenities(self, toks: set, profile_data: dict) -> int:
        """Answer every amenity Yes/No in the CURRENTLY-shown category list."""
        page = self.rt.page
        done = 0
        try:
            items = page.locator("[data-test-id^='amenity-item-']")
            n = min(items.count(), 60)
        except Exception:
            return 0
        ids = []
        for i in range(n):
            try:
                tid = items.nth(i).get_attribute("data-test-id")  # amenity-item-50002
                if tid:
                    ids.append(tid.rsplit("-", 1)[-1])
            except Exception:
                pass
        for aid in ids:
            try:
                txt = page.locator(f"[data-test-id='amenity-text-{aid}']").first
                name = (txt.inner_text() if txt.count() else "") or ""
                yes = self._amenity_present(name, toks)
                sel = f"[data-test-id='amenity-radio-{aid}-radio-button-{'true' if yes else 'false'}']"
                btn = page.locator(sel).first
                if btn.count() == 0:
                    continue
                inp = btn.locator("input[type='radio']").first
                already = False
                try:
                    already = inp.count() > 0 and inp.is_checked()
                except Exception:
                    already = False
                if not already:
                    self._click_robust(btn); done += 1; self.rt.think(0.05, 0.15)
                if yes:
                    self._mmt_amenity_subdropdown(aid, name, profile_data)
            except Exception:
                pass
        return done

    def _fill_mmt_amenities(self, profile_data) -> bool:
        """MMT 'Property Amenities' — amenities are grouped in left-side category tabs
        (Mandatory, Security, Basic Facilities …). The scraper only sees the open tab, so
        click THROUGH every category and answer all Yes/No (+ any 'Select' sub-dropdown)."""
        page = self.rt.page
        try:
            cats = page.locator("[data-test-id^='amenity-category-']")
            ncat = cats.count()
        except Exception:
            ncat = 0
        if ncat == 0:
            return False
        toks = self._mmt_present_set(profile_data)
        cat_ids = []
        for i in range(ncat):
            try:
                tid = cats.nth(i).get_attribute("data-test-id")
                if tid:
                    cat_ids.append(tid)
            except Exception:
                pass
        did = 0
        for ctid in cat_ids:
            try:
                tab = page.locator(f"[data-test-id='{ctid}']").first
                if tab.count() == 0:
                    continue
                self._click_robust(tab); self.rt.think(0.4, 0.8)
            except Exception:
                continue
            did += self._mmt_answer_visible_amenities(toks, profile_data)
        if did:
            self._say(f"  ✓ MMT amenities — answered {did} across {len(cat_ids)} categories")
        return did > 0

    def _fill_mmt_occupancy(self, profile_data) -> bool:
        """MMT 'Sleeping Arrangement & Occupancy' — the occupancy counters are PRE-FILLED from
        the bed arrangement and only need one rule enforced: max children MUST be < max
        occupancy (and base ≤ max). The LLM kept stepping them into an invalid state, so fix
        them deterministically here (and the prompt now tells the LLM to leave them alone)."""
        page = self.rt.page
        try:
            if page.locator("[data-test-id='max-occupancy-counter']").count() == 0:
                return False
        except Exception:
            return False

        def cinput(tid):
            loc = page.locator(f"[data-test-id='{tid}'] [data-test-id='counter-input']").first
            return loc if loc.count() else None

        def cval(tid):
            loc = cinput(tid)
            try:
                return int(loc.input_value()) if loc is not None else None
            except Exception:
                return None

        O = cval("max-occupancy-counter") or 0
        A = cval("max-adult-counter") or 0
        if O <= 0:
            return False
        did = 0
        # max children must be strictly < max occupancy → the leftover after adults, clamped
        target_c = max(0, min(O - 1, O - A))
        mc = cinput("max-child-counter")
        if mc is not None and (cval("max-child-counter") or 0) != target_c:
            if self._set_counter(mc, target_c):
                did += 1
                self._say(f"  · MMT max children → {target_c} (must be < max occupancy {O})")
        # base children must be ≤ max children
        bc = cinput("base-child-counter")
        if bc is not None and (cval("base-child-counter") or 0) > target_c:
            if self._set_counter(bc, target_c):
                did += 1
        if did:
            self._say("  ✓ MMT occupancy — kept valid (max children < max occupancy)")
        return did > 0

    def _fill_mmt_rooms_overview(self, profile_data) -> bool:
        """MMT 'Create Room' OVERVIEW (lists CREATED rooms + a 'Create New Room' button). MMT
        builds rooms ONE at a time — if fewer rooms exist than the JSON has, click 'Create New
        Room' to start the next one (and point the LLM at that room via self._mmt_room_idx).
        Returns True (navigated) when it starts another room."""
        import re
        page = self.rt.page
        try:
            cnr = page.get_by_text("Create New Room", exact=False).count()
        except Exception:
            cnr = 0
        if cnr == 0:
            return False                                   # not the overview (no 'Create New Room')
        # count created rooms — one 'Edit Room' link per created room (robust); fall back to text
        created = 0
        try:
            created = page.get_by_text("Edit Room", exact=False).count()
        except Exception:
            created = 0
        if created == 0:
            try:
                m = re.search(r"CREATED\s*\((\d+)\)", page.inner_text("body") or "", re.I)
                created = int(m.group(1)) if m else 0
            except Exception:
                created = 0
        target = len(profile_data.get("room_types", []) or [])
        self._say(f"  · MMT rooms overview — {created} created / {target} wanted")
        if target == 0 or created >= target:
            self._mmt_room_idx = max(0, target - 1)
            return False                                   # all rooms made → let Continue advance
        self._mmt_room_idx = created                       # next room to create = index `created`
        nxt = (profile_data["room_types"][created] or {}).get("name", "")
        btn = self._locate("text", "Create New Room")
        if btn is None:
            self._say("  – MMT: 'Create New Room' button not found")
            return False
        try:
            self._click_robust(btn)
            self._say(f"  · MMT → Create New Room {created + 1}/{target} ({nxt})")
            self.rt.think(0.9, 1.5)
            return True
        except Exception as e:
            self._say(f"  – MMT 'Create New Room' click failed: {type(e).__name__}")
            return False

    def _click_agoda_card(self, target: str) -> bool:
        """Click the property-type CARD whose title matches `target`. Cards are clickable
        <div>s whose text starts with the title then a description ('Hotel Multi-unit …').
        Score every clickable candidate with the precise matcher: an exact title (tier 0)
        beats a longer prefix/container (tier 2), and on a tie the SHORTER text wins — so
        'Hotel' lands on the Hotel card, never 'Capsule Hotel' nor the whole grid."""
        page = self.rt.page
        js = r"""() => {
          const vis=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0;};
          const txt=n=>((n.innerText||n.textContent||'')).replace(/\s+/g,' ').trim();
          document.querySelectorAll('[data-ap-card]').forEach(e=>e.removeAttribute('data-ap-card'));
          let i=0; const out=[];
          document.querySelectorAll('div,button,li,a,span,p').forEach(n=>{
            if(!vis(n))return; const t=txt(n); if(t.length<2||t.length>200)return;
            let cur=''; try{cur=getComputedStyle(n).cursor;}catch(e){}
            if(cur!=='pointer' && !(n.getAttribute&&n.getAttribute('role')==='button'))return;
            n.setAttribute('data-ap-card', String(i)); out.push({idx:i, text:t}); i++;
          });
          return out;
        }"""
        try:
            cards = page.evaluate(js) or []
        except Exception:
            cards = []
        best = None                                   # ((tier, len), card)
        for c in cards:
            t = dropdown_tier(c["text"], target)
            if t < 99:
                key = (t, len(c["text"]))
                if best is None or key < best[0]:
                    best = (key, c)
        if best is None:
            return False
        try:
            self._click_robust(page.locator(f'[data-ap-card="{best[1]["idx"]}"]').first)
            return True
        except Exception:
            return False

    def _fill_agoda_property_type(self, profile_data) -> bool:
        """Agoda 'What type of property are you listing?' (category) and 'Which hotel-type
        property best fits your place?' (sub-type) are custom CARD pages — no form controls,
        so the LLM only guessed at them and they took 2-3 tries. Pick the right card
        deterministically from the property's type, first pass, no LLM."""
        page = self.rt.page
        try:
            head = (page.inner_text("h1,h2") or "").strip().lower()
        except Exception:
            head = ""
        is_cat = "what type of property are you listing" in head
        is_sub = "best fits your place" in head
        if not (is_cat or is_sub):
            return False
        ptype = (profile_data.get("property_type") or "hotel").lower().replace("-", "_").replace(" ", "_")
        if is_cat:
            target = "Home-type" if any(h in ptype for h in ("villa", "bungalow", "cottage")) else "Hotel-type"
        else:
            SUB = {"hotel": "Hotel", "motel": "Motel", "resort": "Resort", "inn": "Inn",
                   "lodge": "Lodge", "hostel": "Hostel", "guest_house": "Guest House",
                   "guesthouse": "Guest House", "serviced_apartment": "Serviced Apartment",
                   "apartment": "Serviced Apartment", "capsule_hotel": "Capsule Hotel",
                   "capsule": "Capsule Hotel", "bed_and_breakfast": "Bed & Breakfast",
                   "bnb": "Bed & Breakfast"}
            target = SUB.get(ptype, "Hotel")
        if self._click_agoda_card(target):
            self._say(f"  ✓ Agoda property type → {target}")
            self.rt.think(0.5, 0.9)
            return True
        self._say(f"  · Agoda property type: card ‘{target}’ not found")
        return False

    def _fill_agoda_location(self, profile_data) -> bool:
        """Agoda onboarding step 1 ('Location'). Standard data-testid inputs + button
        dropdowns for State/City + a Google search box and map pin. The generic LLM is
        told to skip ALL address fields (Booking's are a closed widget) — so Agoda needs
        its own handler. Search to drop the pin, then fill the structured fields."""
        page = self.rt.page
        try:
            is_loc = (page.locator("input[data-testid='street-address']").count() > 0
                      or page.locator("[data-element-name='location-search-field']").count() > 0)
        except Exception:
            is_loc = False
        if not is_loc:
            return False

        addr = profile_data.get("address", {}) or {}
        did = 0

        # A) search box → type address + pick first prediction (moves the map pin off the
        #    default and often auto-fills the structured fields)
        try:
            box = page.locator("input[placeholder='Search for your property location']").first
            if box.count() and box.is_visible() and not (box.input_value() or "").strip():
                query = ", ".join(p for p in (addr.get("line1"), addr.get("city"),
                                              addr.get("state")) if p)
                if query:
                    self._click_robust(box); box.fill("")
                    page.keyboard.type(query, delay=70)
                    self.rt.think(1.3, 1.9)
                    picked = False
                    for sel in ("ul[role='listbox'] li", "[role='listbox'] [role='option']",
                                "[data-element-name*='suggestion']", ".pac-item",
                                "[class*='SearchField'] li"):
                        o = page.locator(sel).first
                        if o.count() and o.is_visible():
                            self._click_robust(o); picked = True; break
                    if not picked:
                        page.keyboard.press("ArrowDown"); self.rt.think(0.3, 0.5)
                        page.keyboard.press("Enter")
                    self._say(f"  · Agoda location → searched ‘{query}’")
                    self.rt.think(1.0, 1.6); did += 1
        except Exception:
            pass

        # B) structured text inputs (fill any the search left blank)
        for tid, val in (("street-address", addr.get("line1")),
                         ("building", addr.get("line2")),
                         ("zip-postal-code", addr.get("postal_code"))):
            if not val:
                continue
            el = self._find_visible(page, f"input[data-testid='{tid}']")
            if el is None:
                continue
            try:
                if (el.input_value() or "").strip():
                    continue
            except Exception:
                pass
            if self._set_react_input(el, str(val)):
                did += 1

        # C) State/Province + City — these have NO data-testid (only the text inputs do);
        #    they're <button> dropdowns identified by their field LABEL. The search/map
        #    auto-fills them when it resolves, but when it doesn't (e.g. the pin lands in the
        #    wrong country) we MUST pick them here or the page blocks ("Please enter state").
        import re as _re

        def _geo_btn(lbl):
            for loc in (page.get_by_role("button", name=_re.compile(_re.escape(lbl), _re.I)),
                        page.locator(f"xpath=//*[normalize-space(text())={lbl!r}]/following::button[1]"),
                        page.locator(f'button:has-text("{lbl}")')):
                try:
                    if loc.count() and loc.first.is_visible():
                        return loc.first
                except Exception:
                    continue
            return None

        for label, want in (("State/Province", addr.get("state")), ("City", addr.get("city"))):
            if not want:
                continue
            btn = _geo_btn(label)
            if btn is None:
                self._say(f"  · Agoda {label}: dropdown not found on the page")
                continue
            try:
                cur = (btn.inner_text() or "").strip()
                if cur and cur.lower() != label.lower():
                    continue                          # already chosen (by the search/map)
            except Exception:
                pass
            if self._open_and_pick(btn, [want], strict=True):     # geography — never guess
                did += 1
                self._say(f"  · Agoda {label} → {want}")
                self.rt.think(0.8, 1.3)               # let City repopulate after State is set
            else:
                self._say(f"  · Agoda {label}: couldn't find ‘{want}’ in the dropdown")

        if did:
            self._say(f"  ✓ Agoda location — {addr.get('city','')}, {addr.get('state','')} "
                      f"({did} field(s))")
        return did > 0

    def _room_type_candidates(self, name) -> list:
        """Ordered fallback list for an OTA 'Room type' dropdown. The MIS room name (e.g.
        'Super Deluxe', 'Executive Suite') is often NOT a literal option in the OTA's fixed
        list, so try: the full name → the recognised type keyword inside it → universal
        defaults that almost every OTA has. The picker tries them in order, so the field is
        ALWAYS filled with a valid option and the (required) page can advance."""
        n = (name or "").strip()
        cands = [n] if n else []
        seen = {c.lower() for c in cands}
        low = n.lower()
        # longest/most-specific keywords first so 'Junior Suite' beats 'Suite'
        KEYWORDS = ["presidential suite", "junior suite", "presidential", "penthouse",
                    "executive", "family", "superior", "deluxe", "suite", "premier",
                    "premium", "standard", "studio", "twin", "triple", "quadruple",
                    "double", "single", "dormitory", "villa", "bungalow", "cottage", "apartment"]
        for kw in KEYWORDS:
            if kw in low and kw not in seen:
                cands.append(kw.title()); seen.add(kw)
        for d in ("Deluxe", "Standard", "Superior", "Double", "Standard Room", "Suite", "Room"):
            if d.lower() not in seen:
                cands.append(d); seen.add(d.lower())
        return cands

    def _fill_agoda_rooms(self, profile_data) -> bool:
        """Agoda 'Setup your rooms & rates' (Step 3). The room SIZE/RATE/occupancy are plain
        inputs/counters the LLM handles — but two things it can't: (1) lock rate management to
        'Set rate manually' (autopilot otherwise wanders into the channel-manager dropdown),
        and (2) the per-room 'Room type' button-dropdowns. Handle just those; the LLM does the
        rest, so this does NOT block it."""
        page = self.rt.page
        try:
            on_rooms = (page.locator("[data-testid='room-base-price']").count() > 0
                        or "rooms & rates" in (page.inner_text("h1,h2") or "").lower())
        except Exception:
            on_rooms = False
        if not on_rooms:
            return False
        rooms = profile_data.get("room_types", []) or []
        did = 0

        # 0) Room type per room — the exact MIS name (e.g. 'Super Deluxe') usually isn't in
        #    Agoda's fixed list, so map it to the closest available option. NON-strict on
        #    purpose: a room type MUST be chosen or the required page blocks forever. We feed
        #    an ordered candidate list (full name → type keyword → safe defaults) and pick the
        #    best precise match; the picker only ever lands on a real option.
        try:
            ri = 0
            for _ in range(len(rooms) + 3):           # bounded; one dropdown per room
                btn = page.get_by_role("button", name="Room type").first
                if btn.count() == 0 or not btn.is_visible():
                    break
                r = rooms[ri] if ri < len(rooms) else (rooms[-1] if rooms else {})
                cands = self._room_type_candidates(r.get("name"))
                if self._open_and_pick(btn, cands, strict=False):
                    did += 1
                    self._say(f"  · Agoda room {ri + 1} type ← ‘{r.get('name') or '?'}’")
                    ri += 1
                    self.rt.think(0.5, 0.9)
                else:
                    break                             # no option at all → stop (avoid a loop)
        except Exception:
            pass

        # 1) rate management → 'Set rate manually' (only if not already chosen)
        try:
            for lab in page.locator("label").all():
                if "set rate manually" in (lab.inner_text() or "").lower():
                    rb = lab.locator("input[type='radio']").first
                    already = rb.count() > 0 and rb.is_checked()
                    if not already:
                        self._click_robust(lab); did += 1; self.rt.think(0.3, 0.6)
                        self._say("  · Agoda → Set rate manually")
                    break
        except Exception:
            pass

        # 2) Minimum room rate per room → the JSON base_rate (the page pre-fills a wrong value)
        try:
            rate_inputs = page.locator("[data-testid='room-base-price']")
            for i in range(rate_inputs.count()):
                r = rooms[i] if i < len(rooms) else (rooms[0] if rooms else {})
                br = r.get("base_rate")
                if not br:
                    continue
                want = ("%g" % float(br))
                el = rate_inputs.nth(i)
                if (el.input_value() or "").strip() == want:
                    continue                          # already correct
                if self._set_react_input(el, want):
                    did += 1; self._say(f"  · Agoda room {i + 1} min rate → {want}")
        except Exception:
            pass

        # 3) Total-occupancy-limit + Bathrooms counters (the LLM can't tell the +/- apart)
        did += self._fill_agoda_room_counters(rooms)

        # 4) Breakfast Yes/No (required to advance) → from the profile
        bf = ((profile_data.get("facilities", {}) or {}).get("breakfast", {}) or {})
        did += 1 if self._fill_agoda_breakfast(bool(bf.get("available"))) else 0

        if did:
            self._say(f"  ✓ Agoda rooms — occupancy/rate/bathrooms/breakfast set ({did})")
        return did > 0

    def _fill_agoda_room_counters(self, rooms) -> int:
        """Set each room's 'Total occupancy limit' (→ max occupancy) and 'Bathrooms' (→ ≥1).
        The +/- buttons only carry aria-label 'increase'/'decrease', so we identify each
        counter by the short field-label text next to it. Re-checks the current value and
        only steps if it's wrong — never proceeds with the default 1."""
        page = self.rt.page
        tag_js = r"""() => {
          const norm=s=>(s||'').replace(/\s+/g,' ').trim();
          document.querySelectorAll('[data-ap-cnt],[data-ap-cval]').forEach(e=>{e.removeAttribute('data-ap-cnt');e.removeAttribute('data-ap-cval');});
          const out=[]; let idx=0;
          document.querySelectorAll('button[aria-label="decrease"]').forEach(dec=>{
            let cont=dec.parentElement, inc=null;
            for(let i=0;i<5 && cont;i++){ inc=cont.querySelector('button[aria-label="increase"]'); if(inc) break; cont=cont.parentElement; }
            if(!inc||!cont) return;
            let valEl=null,val=null;
            cont.querySelectorAll('*').forEach(n=>{ if(valEl) return; if(n.children.length===0){ const t=norm(n.textContent); if(/^\d+$/.test(t)){ valEl=n; val=parseInt(t,10);} } });
            let label='', probe=cont;
            for(let i=0;i<5 && probe;i++){ const t=norm(probe.textContent);
              if(t.length<70 && /(total )?occupancy|occupancy limit|max(imum)?\s*(guests?|occupancy|adults?)|how many guests|guests? per|sleeps/i.test(t)){label='occupancy';break;}
              if(t.length<70 && /bathroom|washroom/i.test(t)){label='bathrooms';break;} probe=probe.parentElement; }
            dec.setAttribute('data-ap-cnt','dec-'+idx);
            inc.setAttribute('data-ap-cnt','inc-'+idx);
            if(valEl) valEl.setAttribute('data-ap-cval','val-'+idx);
            out.push({idx, label, val}); idx++;
          });
          return out;
        }"""
        try:
            counters = page.evaluate(tag_js) or []
        except Exception:
            return 0
        occ = [c for c in counters if c["label"] == "occupancy"]
        bath = [c for c in counters if c["label"] == "bathrooms"]
        if not occ and counters:                      # labels didn't resolve → even=occ, odd=bath
            occ = [c for i, c in enumerate(counters) if i % 2 == 0]
            bath = [c for i, c in enumerate(counters) if i % 2 == 1]
        did = 0
        for ri, c in enumerate(occ):
            r = rooms[ri] if ri < len(rooms) else (rooms[0] if rooms else {})
            target = (r.get("max_occupancy")
                      or ((r.get("max_adults") or 0) + (r.get("max_children") or 0))
                      or r.get("base_occupancy") or 2)
            if self._step_counter(c["idx"], c.get("val") or 0, int(target)):
                did += 1; self._say(f"  · Agoda room {ri + 1} occupancy → {target}")
        for c in bath:
            if (c.get("val") or 0) < 1 and self._step_counter(c["idx"], c.get("val") or 0, 1):
                did += 1; self._say("  · Agoda bathrooms → 1")
        return did

    def _step_counter(self, idx, current, target) -> bool:
        """Click a tagged +/- counter from current→target (data-ap-cnt='inc-/dec-<idx>')."""
        page = self.rt.page
        delta = int(target) - int(current)
        if delta == 0:
            return False
        sel = f'[data-ap-cnt="{"inc" if delta > 0 else "dec"}-{idx}"]'
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                return False
            for _ in range(abs(delta)):
                if not btn.is_enabled():
                    break
                self._click_robust(btn); self.rt.think(0.12, 0.25)
            return True
        except Exception:
            return False

    def _fill_agoda_breakfast(self, available: bool) -> bool:
        """Answer Agoda's 'Do you provide breakfast?' Yes/No (required to advance)."""
        page = self.rt.page
        want = "yes" if available else "no"
        pick_js = r"""(want) => {
          const norm=s=>(s||'').replace(/\s+/g,' ').trim();
          document.querySelectorAll('[data-ap-bf]').forEach(e=>e.removeAttribute('data-ap-bf'));
          // smallest block that asks the breakfast question AND holds the Yes/No labels
          let scope=null, best=1e9;
          document.querySelectorAll('div,section,fieldset').forEach(d=>{ const t=norm(d.textContent);
            if(!/do you provide breakfast/i.test(t)) return;
            const hasYN=[...d.querySelectorAll('label')].some(l=>{const lt=norm(l.textContent).toLowerCase(); return lt==='yes'||lt==='no';});
            if(hasYN && t.length<best){ best=t.length; scope=d; } });
          if(!scope) return false;
          const lab=[...scope.querySelectorAll('label')].find(l=>norm(l.textContent).toLowerCase()===want);
          // already chosen? a checked radio inside → skip
          if(lab){ const rb=lab.querySelector('input[type=radio]'); if(rb && rb.checked) return 'done';
                   lab.setAttribute('data-ap-bf','1'); return true; }
          return false;
        }"""
        try:
            res = page.evaluate(pick_js, want)
            if res == "done":
                return False
            if res is True:
                self._click_robust(page.locator('[data-ap-bf="1"]').first)
                self.rt.think(0.2, 0.4)
                self._say(f"  · Agoda breakfast → {want.title()}")
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _time_candidates(t: str) -> list:
        """A 24h time like '13:00' → candidate display strings to match Agoda's time picker,
        which may show 12h ('1:00 PM') or 24h ('13:00')."""
        try:
            hh, mm = t.split(":"); h = int(hh)
        except Exception:
            return [t]
        ap = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return [f"{h:02d}:{mm}", t, f"{h12}:{mm}{ap}", f"{h12}:{mm} {ap}"]

    def _fill_agoda_times(self, profile_data) -> bool:
        """Agoda check-in/check-out time button-dropdowns. The LLM struggles with the 24h↔12h
        format, so pick them deterministically from policy."""
        page = self.rt.page
        try:
            if page.locator("[data-testid='check-in-check-out-start-time'],"
                            "[data-testid='check-out-time']").count() == 0:
                return False
        except Exception:
            return False
        pol = profile_data.get("policy", {}) or {}
        targets = (("check-in-check-out-start-time", pol.get("checkin_from", "14:00")),
                   ("check-in-check-out-end-time", pol.get("checkin_until", "23:00")),
                   ("check-out-time", pol.get("checkout_until", "11:00")))
        did = 0
        for tid, t in targets:
            if not t:
                continue
            try:
                btn = page.locator(f"[data-testid='{tid}'] button").first
                if btn.count() == 0:
                    continue
                cur = (btn.inner_text() or "")
                if any(ch.isdigit() for ch in cur):
                    continue                          # already shows a time
                if self._open_and_pick(btn, self._time_candidates(t)):
                    self._say(f"  · Agoda {tid} → {t}")
                    did += 1; self.rt.think(0.3, 0.7)
            except Exception:
                pass
        if did:
            self._say(f"  ✓ Agoda check-in/out times ({did})")
        return did > 0

    def _fill_agoda_legal(self, profile_data) -> bool:
        """Agoda 'Account details / Company legal details / Ultimate beneficial owner' — lots
        of button-dropdowns (Country, Nationality, State/Province, City) the LLM can't pick.
        Fill them by their aria-label. Text fields (company name/address/zip, first/last name)
        are left to the LLM. Date-of-birth is a separate picker (handled if present)."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        if not ("beneficial owner" in body or "legal details" in body
                or "account details" in body or "nationality" in body):
            return False

        addr = profile_data.get("address", {}) or {}
        country = "India" if (addr.get("country", "").upper() in ("IN", "INDIA")) else addr.get("country")
        state, city = addr.get("state"), addr.get("city")
        # (aria-label, candidate values) — every matching button gets filled
        plan = [("Country", [country]), ("Country/Region", [country]),
                ("Nationality", ["Indian", country]),
                ("State or Province", [state]), ("State/Province", [state]),
                ("State", [state]), ("City", [city])]
        did = 0
        for label, cands in plan:
            cands = [c for c in cands if c]
            if not cands:
                continue
            try:
                btns = page.locator(f"button[aria-label='{label}']")
                for i in range(btns.count()):
                    b = btns.nth(i)
                    cur = (b.inner_text() or "").strip()
                    if cur and cur.lower() != label.lower():
                        continue                      # already chosen
                    if self._open_and_pick(b, cands, strict=True):   # geography — never guess
                        did += 1
                        self._say(f"  · Agoda {label} → {cands[0]}")
                        self.rt.think(0.3, 0.7)
            except Exception:
                pass

        # date of birth — Agoda shows it as a dropdown/picker. Try the dd-mm-yyyy text first;
        # if it's a real picker we'll need its markup (logged so the operator can paste it).
        dob = self._norm_date(profile_data.get("compliance", {}).get("owner_dob", ""))  # yyyy-mm-dd
        if dob:
            try:
                dobf = self._find_visible(page, "input[aria-label='Date of birth'], "
                                                "[data-testid='date-of-birth'] input")
                if dobf is not None and self._set_react_input(dobf, dob):
                    did += 1; self._say(f"  · Agoda Date of birth → {dob}")
            except Exception:
                pass
        if did:
            self._say(f"  ✓ Agoda legal/account — {did} dropdown(s)/field(s)")
        return did > 0

    def _fill_agoda_pricing(self, profile_data) -> bool:
        """Agoda 'Pricing' step. It has NO price input — the real blocker is
        'Please select a payout method' (the Next button stays disabled until one is
        chosen). The page only exposes <label> radios, so the LLM sees 'nothing to
        fill' and the walker loops forever.

        We pick the payout METHOD only — default 'Bank transfer' (matches the profile's
        payout/bank fields). The actual bank ACCOUNT NUMBER is NEVER entered here: it's a
        human-only gate, and Agoda itself defers it ('add your new account once your
        listing is complete'). The optional 'First 3 Bookings' promotion is left to the
        operator. Returns True only on the turn it makes the selection (so Next advances)."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        if "select a payout method" not in body and "how will you receive your earnings" not in body:
            return False

        methods = ("bank-transfer", "upc-e-pass", "upc")
        # already chosen? don't block — let the walker click Next
        for m in methods:
            try:
                inp = page.locator(f"[data-testid='{m}'] input").first
                if inp.count() and inp.is_checked():
                    return False
            except Exception:
                pass
        # pick one — Bank transfer first (NEFT is standard for India; details added later)
        for m in methods:
            try:
                lab = page.locator(f"[data-testid='{m}']").first
                if lab.count() and lab.is_visible():
                    self._click_robust(lab)
                    self.rt.think(0.3, 0.6)
                    self._say(f"  · Agoda payout method → {m.replace('-', ' ')} "
                              f"(bank account details are a later human gate, not auto-filled)")
                    return True
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------ #
    # Expedia (Expedia Partner Central) deterministic page handlers.
    # Same philosophy as the MakeMyTrip / Agoda handlers: the generic LLM
    # walker drives the ordinary fields; these handle ONLY the widgets the
    # scraper/LLM can't see — the address autocomplete, the custom
    # Country/State/City + room/bed dropdowns, and the time pickers. Every
    # handler is body-text gated and returns False when its step isn't on
    # screen, so the LLM walker carries any page they don't recognise. The
    # selectors are resilient (label/placeholder/role + DOM-agnostic
    # _open_and_pick/_select_robust) so they degrade safely; tighten the
    # exact data-* ids after the first live run from data/training/expedia/.
    # ------------------------------------------------------------------ #
    def _fill_expedia_classification(self, profile_data) -> bool:
        """Expedia 'What would you like to list?' landing wizard (apps…/en_US/list). Two
        choice CARDS rendered as <div role='presentation'> — the scraper/LLM can't see them
        as clickable, so autopilot instead clicks the 'List your property' anchor (which only
        scrolls to #ulx-hero) and loops forever. Pick the right card by id from property_type:
        Lodging (#classification_lodging) for a hotel/motel/B&B, else Private residence."""
        page = self.rt.page
        # The landing wizard is an SPA that keeps the classification cards in the DOM (hidden)
        # on later steps — so gate on VISIBILITY, not mere presence, or we'd click a hidden
        # card every pass and churn the form back to its default. Also restrict to the landing
        # URL (…/list, not …/list/<step>).
        try:
            low = (page.url or "").lower().rstrip("/")
            on_landing = low.endswith("/list")
            lod = page.locator("#classification_lodging").first
            pr = page.locator("#classification_privateResidence").first
            visible = ((lod.count() and lod.is_visible()) or (pr.count() and pr.is_visible()))
        except Exception:
            on_landing, visible = False, False
        if not (on_landing and visible):
            return False
        ptype = str(profile_data.get("property_type", "hotel")).lower()
        # Expedia splits the world into Lodging vs Private residence. Whole-property rentals
        # (apartment/villa/home/homestay) are 'Private residence'; everything hotel-like is Lodging.
        residence = ptype in ("apartment", "villa", "holiday_home", "homestay")
        target = "#classification_privateResidence" if residence else "#classification_lodging"
        try:
            el = page.locator(target).first
            if el.count() == 0:                       # fallback to Lodging if the id shifted
                el = page.locator("#classification_lodging").first
            if el.count():
                self._click_robust(el)
                self._say(f"  · Expedia → ‘{'Private residence' if residence else 'Lodging'}’ "
                          f"(classification card)")
                self.rt.think(1.1, 1.7)
                return True
        except Exception:
            pass
        return False

    def _skip_expedia_booking_import(self, profile_data) -> bool:
        """Expedia optional 'Use your Booking.com URL to list your property faster' step. We do
        NOT import from Booking — clear any URL the autopilot/LLM may have typed and click
        'Next' (NOT 'Add', which would pull in the Booking listing). Navigates."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        if "booking.com" not in body or not any(
                m in body for m in ("list your property faster", "bring over property details",
                                    "link your booking.com")):
            return False
        # 1) clear any Booking.com URL the bot typed — we are NOT importing.
        try:
            for sel in ("input[placeholder*='booking.com' i]", "input[value*='booking.com' i]",
                        "input[type='url']", "input[type='text']"):
                el = self._find_visible(page, sel)
                if el is not None:
                    try:
                        if (el.input_value() or "").strip():
                            el.fill("")
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        # 2) click 'Next' to SKIP (not 'Add').
        try:
            btn = page.get_by_role("button", name="Next", exact=True).first
            if btn.count() == 0:
                btn = page.locator("button:has-text('Next')").first
            if btn.count() and btn.is_visible():
                self._click_robust(btn)
                self._say("  · Expedia → skipped Booking.com import (Next, no URL)")
                self.rt.think(1.0, 1.6)
                return True
        except Exception:
            pass
        return False

    def _fill_expedia_typeahead(self, profile_data) -> bool:
        """Expedia Step-1 location TYPEAHEAD (…/list/location, #locationTypeAhead) — a single
        'Enter property name or address...' box with Google-style autocomplete. Type the
        property + address and pick the first suggestion (Expedia's verified happy path). If
        nothing matches — common for dummy data — the walker's Next click drops to the
        manual-address fallback (handled by _fill_expedia_manual_location). Types once."""
        page = self.rt.page
        try:
            box = page.locator("#locationTypeAhead").first
            if box.count() == 0:
                return False
            if (box.input_value() or "").strip():
                return False                          # already typed — let Next advance it
        except Exception:
            return False

        addr = profile_data.get("address", {}) or {}
        parts = [p for p in (profile_data.get("display_name"), addr.get("line1"),
                             addr.get("city"), addr.get("state")) if p]
        if str(addr.get("country", "")).upper() in ("IN", "INDIA"):
            parts.append("India")
        query = ", ".join(parts)
        if not query:
            return False
        try:
            self._click_robust(box)
            box.fill("")
            page.keyboard.type(query, delay=55)
            self.rt.think(1.4, 2.1)                   # let the autocomplete render
            picked = False
            for sel in ("[role='listbox'] [role='option']", "ul[role='listbox'] li",
                        "[data-wdio*='suggestion']", "[data-wdio*='result']",
                        "[class*='typeahead'] li", "[class*='suggestion']", ".pac-item"):
                o = page.locator(sel).first
                if o.count() and o.is_visible():
                    self._click_robust(o)
                    picked = True
                    break
            if not picked:                            # nudge the highlighted suggestion, if any
                page.keyboard.press("ArrowDown"); self.rt.think(0.3, 0.5)
                page.keyboard.press("Enter")
            self._say(f"  · Expedia location typeahead → ‘{query}’"
                      + (" (picked suggestion)" if picked else " (no match — manual fallback)"))
            self.rt.think(0.8, 1.3)
            return True
        except Exception:
            return False

    def _fill_expedia_manual_location(self, profile_data) -> bool:
        """Expedia 'manual address' step (…/list/manual-location, exact ids #country/#address1/
        #stateProvince/#postalCode). ORDER MATTERS: Country must be set FIRST — it drives ZIP
        validation (a 6-digit Indian PIN reads as 'invalid' while the country defaults to US)
        AND repopulates the State dropdown with that country's regions. So set Country →
        Street/Apt/City → State → ZIP, idempotently across passes (Next stays disabled until
        every required field is valid)."""
        page = self.rt.page
        try:
            if page.locator("#country, #address1, #manualAddressNextBtn").count() == 0:
                return False
        except Exception:
            return False

        addr = profile_data.get("address", {}) or {}
        cc = str(addr.get("country", "")).upper()
        iso = "IN" if cc in ("IN", "INDIA") else (addr.get("country") or "")
        country_label = "India" if cc in ("IN", "INDIA") else (addr.get("country") or "")
        did = 0

        # 1) COUNTRY first — drives ZIP validation + repopulates the State dropdown.
        try:
            csel = page.locator("#country").first
            if csel.count():
                cur = (csel.input_value() or "").strip()
                if iso and cur != iso:
                    # React-controlled <select> — set via native setter so it STICKS and the
                    # State dropdown repopulates (select_option alone reverts to the US default)
                    if self._set_react_select(csel, f"{iso}|{country_label}"):
                        did += 1
                        self._say(f"  · Expedia country → {country_label}")
                        self.rt.think(1.1, 1.7)        # let State repopulate + ZIP re-validate
        except Exception:
            pass

        # 2) street / apt / city text inputs
        for _id, val in (("#address1", addr.get("line1")), ("#address2", addr.get("line2")),
                         ("#city", addr.get("city"))):
            if not val:
                continue
            try:
                el = page.locator(_id).first
                if el.count() and (el.input_value() or "").strip() != str(val):
                    if self._set_react_input(el, str(val)):
                        did += 1
            except Exception:
                pass

        # 3) STATE — only after country is set (the options are that country's regions now).
        state = addr.get("state")
        if state:
            try:
                ssel = page.locator("#stateProvince").first
                if ssel.count() and not (ssel.input_value() or "").strip():
                    if self._set_react_select(ssel, str(state)):
                        did += 1
                        self._say(f"  · Expedia state → {state}")
            except Exception:
                pass

        # 4) ZIP last — (re)fill so it validates under the now-correct country.
        pin = addr.get("postal_code")
        if pin:
            try:
                z = page.locator("#postalCode").first
                if z.count():
                    cls = (z.get_attribute("class") or "")
                    cur = (z.input_value() or "").strip()
                    if cur != str(pin) or "invalid" in cls:
                        if self._set_react_input(z, str(pin)):
                            did += 1
            except Exception:
                pass

        if did:
            self._say(f"  ✓ Expedia manual address — {addr.get('city','')}, "
                      f"{addr.get('state','')} ({did} field(s))")
        return did > 0

    def _fill_expedia_location(self, profile_data) -> bool:
        """Expedia 'Where's your property located?' / address step. The address is every
        OTA's LLM blind spot: a Google-Places-style autocomplete search box plus structured
        Address/City/Postal fields and Country/State/City dropdowns. Search to drop the pin,
        fill any structured field the search left blank, then pick the dropdowns. Blocks the
        LLM for this page (like the Agoda location handler)."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        addr_markers = ("property address", "where's your property", "where is your property",
                        "property location", "tell us where", "street address", "your address")
        try:
            has_addr_input = page.locator(
                "input[name*='address' i], input[id*='address' i], "
                "input[placeholder*='address' i], input[aria-label*='address' i]").count() > 0
        except Exception:
            has_addr_input = False
        if not (any(m in body for m in addr_markers) or has_addr_input):
            return False

        addr = profile_data.get("address", {}) or {}
        country = ("India" if str(addr.get("country", "")).upper() in ("IN", "INDIA")
                   else addr.get("country"))
        did = 0

        # A) autocomplete search box → type the address + pick the first prediction (drops the
        #    map pin and usually auto-fills the structured fields below).
        try:
            box = None
            for sel in ("input[placeholder*='search' i]", "input[aria-label*='search' i]",
                        "input[placeholder*='address' i]", "input[aria-label*='address' i]"):
                cand = self._find_visible(page, sel)
                if cand is not None:
                    box = cand; break
            if box is not None and not (box.input_value() or "").strip():
                query = ", ".join(p for p in (addr.get("line1"), addr.get("city"),
                                              addr.get("state"), country) if p)
                if query:
                    self._click_robust(box); box.fill("")
                    page.keyboard.type(query, delay=70)
                    self.rt.think(1.3, 1.9)
                    picked = False
                    for osel in ("ul[role='listbox'] li", "[role='listbox'] [role='option']",
                                 "[role='option']", ".pac-item", "li[role='option']"):
                        o = page.locator(osel).first
                        if o.count() and o.is_visible():
                            self._click_robust(o); picked = True; break
                    if not picked:
                        page.keyboard.press("ArrowDown"); self.rt.think(0.3, 0.5)
                        page.keyboard.press("Enter")
                    self._say(f"  · Expedia location → searched ‘{query}’")
                    self.rt.think(1.0, 1.6); did += 1
        except Exception:
            pass

        # B) structured text inputs (fill any the search left blank)
        field_plan = [
            (("input[name*='address1' i]", "input[name*='line1' i]", "input[id*='address1' i]",
              "input[aria-label*='address line 1' i]", "input[placeholder*='address line 1' i]"),
             addr.get("line1")),
            (("input[name*='address2' i]", "input[name*='line2' i]", "input[id*='address2' i]",
              "input[aria-label*='address line 2' i]"), addr.get("line2")),
            (("input[name*='city' i]", "input[id*='city' i]", "input[aria-label*='city' i]",
              "input[placeholder*='city' i]"), addr.get("city")),
            (("input[name*='zip' i]", "input[name*='postal' i]", "input[id*='postal' i]",
              "input[aria-label*='postal' i]", "input[placeholder*='postal' i]",
              "input[placeholder*='zip' i]"), addr.get("postal_code")),
        ]
        for sels, val in field_plan:
            if not val:
                continue
            el = None
            for sel in sels:
                el = self._find_visible(page, sel)
                if el is not None:
                    break
            if el is None:
                continue
            try:
                if (el.input_value() or "").strip():
                    continue                          # search already filled it
            except Exception:
                pass
            if self._set_react_input(el, str(val)):
                did += 1

        # C) Country / State / City dropdowns — native <select> OR custom; _select_robust
        #    handles both. Skip any that already shows a chosen value.
        dd_plan = [
            (("select[name*='country' i]", "select[id*='country' i]",
              "button[aria-label*='country' i]", "[role='button'][aria-label*='country' i]"),
             country),
            (("select[name*='state' i]", "select[name*='province' i]",
              "button[aria-label*='state' i]", "button[aria-label*='province' i]"),
             addr.get("state")),
            (("select[name*='city' i]", "button[aria-label*='city' i]"), addr.get("city")),
        ]
        for sels, want in dd_plan:
            if not want:
                continue
            loc = None
            for sel in sels:
                try:
                    cand = page.locator(sel).first
                    if cand.count() and cand.is_visible():
                        loc = cand; break
                except Exception:
                    continue
            if loc is None or self._dropdown_already_set(loc, want):
                continue
            try:
                if self._select_robust(loc, str(want)):
                    did += 1
                    self._say(f"  · Expedia location dropdown → {want}")
                    self.rt.think(0.3, 0.7)
            except Exception:
                pass

        if did:
            self._say(f"  ✓ Expedia location — {addr.get('city','')}, {addr.get('state','')} "
                      f"({did} field(s))")
        return did > 0

    def _fill_expedia_rooms(self, profile_data) -> bool:
        """Expedia 'Set up your rooms' — the room-type / bed-type custom dropdowns the LLM
        can't pick. Size / base rate / occupancy are plain inputs+counters the LLM fills, so
        this does NOT block it. Conservative: only touches a dropdown still on its placeholder
        (matches the FIRST room — Expedia's per-room sub-flow is handled by the generic unit
        walker, which feeds the LLM one room at a time)."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        if not any(m in body for m in ("room type", "bed type", "set up your room",
                                       "sleeping arrangement", "room details", "add a room")):
            return False
        rooms = profile_data.get("room_types", []) or []
        first = rooms[0] if rooms else {}
        beds = first.get("beds") or []
        bed_type = beds[0].get("bed_type") if beds else None
        plan = [
            (("button[aria-label*='room type' i]", "[role='button'][aria-label*='room type' i]",
              "select[name*='roomtype' i]", "select[aria-label*='room type' i]"),
             first.get("name")),
            (("button[aria-label*='bed type' i]", "select[name*='bedtype' i]",
              "select[aria-label*='bed type' i]"), bed_type),
        ]
        did = 0
        for sels, want in plan:
            if not want:
                continue
            loc = None
            for sel in sels:
                try:
                    cand = page.locator(sel).first
                    if cand.count() and cand.is_visible():
                        loc = cand; break
                except Exception:
                    continue
            if loc is None or self._dropdown_already_set(loc, want):
                continue
            cands = [str(want)]
            if isinstance(want, str):
                if "_" in want:
                    cands.append(want.replace("_", " "))   # enum 'sofa_bed' → 'sofa bed'
                if " " in want:
                    cands.append(want.split()[0])          # 'Deluxe Double' → 'Deluxe'
            if self._select_robust(loc, "|".join(cands)):
                did += 1
                self._say(f"  · Expedia room dropdown → {want}")
                self.rt.think(0.3, 0.7)
        if did:
            self._say(f"  ✓ Expedia rooms — {did} dropdown(s)")
        return did > 0

    def _fill_expedia_times(self, profile_data) -> bool:
        """Expedia check-in / check-out time pickers (native <select> or custom dropdown).
        The LLM struggles with the 24h↔12h format, so pick them deterministically from
        policy. Does NOT block the LLM."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        if not any(m in body for m in ("check-in", "check in", "check-out", "check out")):
            return False
        pol = profile_data.get("policy", {}) or {}
        targets = (("check-in", pol.get("checkin_from", "14:00")),
                   ("check-out", pol.get("checkout_until", "11:00")))
        did = 0
        for label, t in targets:
            if not t:
                continue
            loc = None
            for sel in (f"select[aria-label*='{label}' i]",
                        f"select[name*='{label.replace('-', '')}' i]",
                        f"select[id*='{label.replace('-', '')}' i]",
                        f"button[aria-label*='{label} time' i]",
                        f"[role='button'][aria-label*='{label} time' i]"):
                try:
                    cand = page.locator(sel).first
                    if cand.count() and cand.is_visible():
                        loc = cand; break
                except Exception:
                    continue
            if loc is None or self._dropdown_already_set(loc, None, require_time=True):
                continue
            if self._select_robust(loc, "|".join(self._time_candidates(t))):
                did += 1
                self._say(f"  · Expedia {label} time → {t}")
                self.rt.think(0.3, 0.7)
        if did:
            self._say(f"  ✓ Expedia check-in/out times ({did})")
        return did > 0

    def _fill_expedia_policies(self, profile_data) -> bool:
        """Expedia onboarding 'Policies and settings' (…/onboarding/policiesAndSettings). Its
        required custom widgets defeat the LLM, and the LLM actively HARMS the tax section
        (typing 0 into unchecked tax % inputs → 'invalid', which blocks Next). Own this page:
        pick a payment method, set Property time zone + Billing currency, and CLEAR any invalid
        tax % the LLM left (taxes stay optional/unchecked). Blocks the LLM for this page."""
        page = self.rt.page
        try:
            is_pol = ("policiesandsettings" in (page.url or "").lower()
                      or page.locator("#timeZoneId, select[name='billingCurrency']").count() > 0)
        except Exception:
            is_pol = False
        if not is_pol:
            return False

        pol = profile_data.get("policy", {}) or {}
        pays = [str(x).lower() for x in (pol.get("payment_methods") or [])]
        cc = str((profile_data.get("address", {}) or {}).get("country", "")).upper()
        did = 0

        # 1) Payment methods — at least one switch must be ON ('Please select an option').
        want_cards = (not pays) or any(k in pays for k in
                                       ("visa", "mastercard", "amex", "card", "credit", "debit"))
        want_cash = "cash" in pays
        for txt, want in (("Credit / debit cards", want_cards), ("Cash", want_cash)):
            if not want:
                continue
            try:
                lab = page.locator("label.fds-switch", has_text=txt).first
                chk = lab.locator("input[type='checkbox']").first
                if chk.count() and not chk.is_checked():
                    self._click_robust(lab.locator(".fds-switch-control").first)
                    self._say(f"  · Expedia payment → {txt}")
                    did += 1
            except Exception:
                pass

        # 1b) Deposits — the chip defaults to 'No'; only switch to 'Yes' if the profile says so.
        if pol.get("deposit_required"):
            try:
                yes = page.locator(".depositSectionChips .fds-chip-item, #deposit .fds-chip-item",
                                   has_text="Yes").first
                if yes.count():
                    self._click_robust(yes)
                    self._say("  · Expedia deposit → Yes")
                    did += 1
            except Exception:
                pass

        # 2) Property time zone (required) — prefer the explicit `timezone`, else derive from country.
        try:
            tz = page.locator("#timeZoneId").first
            if tz.count() and (tz.input_value() or "0") in ("", "0"):
                tzwant = (profile_data.get("timezone") or "").strip()
                cands = []
                if tzwant:                            # IANA like 'Asia/Kolkata' → city + raw
                    cands += [tzwant.split("/")[-1].replace("_", " "), tzwant]
                if cc in ("IN", "INDIA"):
                    cands += ["Chennai, Kolkata, Mumbai, New Delhi", "Kolkata", "GMT+05:30"]
                if cands and self._set_react_select(tz, "|".join(cands)):
                    self._say(f"  · Expedia time zone → {tzwant or 'IST (GMT+05:30)'}")
                    did += 1
        except Exception:
            pass

        # 3) Billing currency (required) → explicit billing_currency, else the property currency.
        try:
            bc = page.locator("select[name='billingCurrency']").first
            cur = profile_data.get("billing_currency") or profile_data.get("currency") or "INR"
            if bc.count() and not (bc.input_value() or "").strip():
                if self._set_react_select(bc, cur):
                    self._say(f"  · Expedia billing currency → {cur}")
                    did += 1
        except Exception:
            pass

        # 4) Clear the LLM's invalid tax zeros — taxes are OPTIONAL; an unchecked tax with a
        #    0 in its % box reads as 'invalid' and blocks Next. Empty them so they're ignored.
        try:
            inv = page.locator("label.fds-field.invalid input")
            for i in range(inv.count()):
                el = inv.nth(i)
                try:
                    if (el.input_value() or "").strip():
                        self._set_react_input(el, "")
                        did += 1
                except Exception:
                    pass
        except Exception:
            pass

        if did:
            self._say(f"  ✓ Expedia policies & settings ({did} field(s))")
        return did > 0

    # ----------------------------------------------------------------------- #
    # AIRBNB — 'become a host' single-listing wizard
    #
    # Airbnb's create flow is one-question-per-screen (structure → privacy-type →
    # location → floor-plan → amenities → photos → title → description → price), each
    # with a persistent Back/Next footer. Selecting a card / driving a stepper / picking
    # an amenity tile is invisible to the scraper+LLM, so each handler below OWNS that
    # widget and leaves the ordinary fields to the generic walker. Every handler is
    # body-text + URL gated and returns False (deferring to the LLM walker) when its
    # step isn't on screen — Airbnb is heavily bot-protected, so the selectors are
    # resilient first-pass and get tightened from one live run (read data/logs/airbnb.log
    # + data/training/airbnb/ and lock the exact data-* ids), exactly like MMT/Agoda.
    # ----------------------------------------------------------------------- #
    def _airbnb_card_selected(self) -> bool:
        """True if some card/radio on the current step already shows a selection — so a
        re-fire of a card handler doesn't TOGGLE the choice back off (clicking a selected
        Airbnb card deselects it). Lets the handler return 'handled' and let Next advance."""
        try:
            return self.rt.page.locator(
                "[aria-checked='true'], [aria-pressed='true'], input:checked").count() > 0
        except Exception:
            return False

    def _airbnb_pick_card(self, values) -> bool:
        """Click the first card whose label matches one of `values`. Tries an accessible
        button/text match first (stable across Airbnb's class churn), then the DOM-agnostic
        card scanner (_click_agoda_card) as a live fallback."""
        page = self.rt.page
        for v in values:
            if not v:
                continue
            for getter in (lambda v=v: page.get_by_role("button", name=v),
                           lambda v=v: page.get_by_text(v, exact=False)):
                try:
                    loc = getter().first
                    if loc.count() and loc.is_visible():
                        self._click_robust(loc)
                        return True
                except Exception:
                    pass
        for v in values:                               # live DOM-agnostic fallback
            try:
                if v and self._click_agoda_card(str(v)):
                    return True
            except Exception:
                pass
        return False

    def _fill_airbnb_structure(self, profile_data) -> bool:
        """Airbnb 'Which of these best describes your place?' — a grid of property-type
        cards (House, Apartment, Hotel, Guest house, Bed & breakfast, Hostel, …). The
        scraper sees a wall of identical buttons; pick the card from property_type. The
        card doesn't auto-advance (Next does), so this BLOCKS the LLM and lets Continue
        advance."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
            url = (page.url or "").lower()
        except Exception:
            return False
        if not ("which of these best describes" in body or "/structure" in url):
            return False
        if self._airbnb_card_selected():
            return True                                # already chosen — let Next advance
        ptype = str(profile_data.get("property_type", "hotel")).lower()
        AIRBNB_STRUCTURE = {
            "hotel": ["Hotel", "Boutique hotel"],
            "resort": ["Resort", "Hotel"],
            "apartment": ["Apartment", "Rental unit", "Serviced apartment"],
            "aparthotel": ["Serviced apartment", "Aparthotel", "Apartment"],
            "guesthouse": ["Guest house", "Guesthouse", "Bed & breakfast"],
            "homestay": ["Guest house", "Home", "House"],
            "bnb": ["Bed & breakfast", "Guest house"],
            "hostel": ["Hostel", "Guest house"],
            "villa": ["Villa", "House", "Home"],
            "holiday_home": ["Home", "House", "Villa"],
        }
        cands = AIRBNB_STRUCTURE.get(ptype, ["House", "Hotel"])
        if self._airbnb_pick_card(cands):
            self._say(f"  ✓ Airbnb structure → {cands[0]}")
            self.rt.think(0.5, 0.9)
            return True
        self._say(f"  · Airbnb structure: no card matched {cands}")
        return False

    def _fill_airbnb_privacy_type(self, profile_data) -> bool:
        """Airbnb 'What type of place will guests have?' — An entire place / A room / A
        shared room. A hotel/villa/apartment is an entire place; a hostel is a shared room;
        a B&B/homestay/guesthouse rents a private room. Blocks the LLM."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
            url = (page.url or "").lower()
        except Exception:
            return False
        if not ("what type of place will guests have" in body
                or "/privacy-type" in url or "/room-type" in url):
            return False
        if self._airbnb_card_selected():
            return True
        ptype = str(profile_data.get("property_type", "hotel")).lower()
        if ptype == "hostel":
            label = "A shared room"
        elif ptype in ("bnb", "homestay", "guesthouse"):
            label = "A room"
        else:
            label = "An entire place"
        if self._airbnb_pick_card([label]):
            self._say(f"  ✓ Airbnb privacy type → {label}")
            self.rt.think(0.5, 0.9)
            return True
        return False

    def _fill_airbnb_location(self, profile_data) -> bool:
        """Airbnb 'Where's your place located?' — a map search box with an 'Enter address
        manually' reveal. The address is every OTA's LLM blind spot: pick Country/region
        FIRST (drives the ZIP format), then the structured Street/Apt/City/State/ZIP fields
        (on Airbnb these are plain inputs, not dropdowns). Blocks the LLM for this page."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
            url = (page.url or "").lower()
        except Exception:
            return False
        markers = ("where's your place located", "where is your place located",
                   "confirm your address", "enter your address", "your address")
        try:
            has_addr_input = page.locator(
                "input[name*='address' i], input[id*='address' i], "
                "input[aria-label*='street' i], input[placeholder*='address' i]").count() > 0
        except Exception:
            has_addr_input = False
        if not (any(m in body for m in markers) or "/location" in url or has_addr_input):
            return False

        # reveal the manual-entry form if Airbnb is showing only the map search box
        try:
            for sel in ("button:has-text('Enter address manually')",
                        "a:has-text('Enter address manually')",
                        "[role='button']:has-text('Enter address manually')"):
                link = page.locator(sel).first
                if link.count() and link.is_visible():
                    self._click_robust(link); self.rt.think(0.5, 0.9); break
        except Exception:
            pass

        addr = profile_data.get("address", {}) or {}
        cc = "IN" if str(addr.get("country", "")).upper() in ("IN", "INDIA") else str(addr.get("country") or "")
        country = "India" if cc == "IN" else (addr.get("country") or "")
        did = 0

        # A) Country / region FIRST (native <select> or custom) — drives the ZIP format.
        for sel in ("select[name*='country' i]", "select[id*='country' i]",
                    "[data-testid='country-selector'] select",
                    "button[aria-label*='country' i]", "button[aria-label*='region' i]"):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    if country and not self._dropdown_already_set(loc, country):
                        if self._select_robust(loc, f"{cc}|{country}"):
                            did += 1
                            self._say(f"  · Airbnb country → {country}")
                            self.rt.think(0.8, 1.3)
                    break
            except Exception:
                continue

        # B) structured text fields (Street / Apt / City / State / ZIP)
        field_plan = [
            (("input[name*='addressLine1' i]", "input[name*='street' i]", "input[id*='street' i]",
              "input[aria-label*='street' i]", "input[placeholder*='street' i]"), addr.get("line1")),
            (("input[name*='addressLine2' i]", "input[name*='apt' i]", "input[aria-label*='apt' i]",
              "input[aria-label*='suite' i]"), addr.get("line2")),
            (("input[name*='city' i]", "input[id*='city' i]", "input[aria-label*='city' i]"),
             addr.get("city")),
            (("input[name*='state' i]", "input[name*='province' i]", "input[aria-label*='state' i]",
              "input[aria-label*='province' i]", "input[aria-label*='territory' i]"), addr.get("state")),
            (("input[name*='zip' i]", "input[name*='postal' i]", "input[id*='zip' i]",
              "input[aria-label*='zip' i]", "input[aria-label*='postal' i]"), addr.get("postal_code")),
        ]
        for sels, val in field_plan:
            if not val:
                continue
            el = None
            for sel in sels:
                el = self._find_visible(page, sel)
                if el is not None:
                    break
            if el is None:
                continue
            try:
                if (el.input_value() or "").strip():
                    continue                           # search/manual already filled it
            except Exception:
                pass
            if self._set_react_input(el, str(val)):
                did += 1
        if did:
            self._say(f"  ✓ Airbnb location — {addr.get('city','')}, {addr.get('state','')} "
                      f"({did} field(s))")
        return did > 0

    def _airbnb_floor_counts(self, profile_data) -> dict:
        """Derive Airbnb's floor-plan counters (Guests / Bedrooms / Beds / Bathrooms) from
        the profile. The whole property lists as ONE 'entire place', so guests = total
        capacity, bedrooms = total physical rooms, beds = total bed count, bathrooms = the
        private-bath room count. Clamped to Airbnb's create-flow caps (16 guests / 50 else)."""
        rooms = profile_data.get("room_types", []) or []
        guests = beds = bedrooms = baths = 0
        for r in rooms:
            cnt = int(r.get("count", 1) or 1)
            bedrooms += cnt
            cap = int(r.get("max_adults", 2) or 2) + int(r.get("max_children", 0) or 0)
            guests += cap * cnt
            rbeds = sum(int(b.get("count", 1) or 1) for b in (r.get("beds") or [])) or 1
            beds += rbeds * cnt
            if str(r.get("bathroom", "private")).lower() == "private":
                baths += cnt
        bedrooms = int(profile_data.get("total_room_count") or bedrooms or 1)
        guests = guests or 2
        beds = beds or bedrooms
        baths = baths or 1
        return {"Guests": min(guests, 16), "Bedrooms": min(bedrooms, 50),
                "Beds": min(beds, 50), "Bathrooms": min(baths, 50)}

    def _airbnb_set_stepper(self, field: str, target) -> bool:
        """Drive an Airbnb +/- stepper (data-testid='stepper-floorPlan<Field>-increase/
        decrease-button', value in '-value') to `target`. Re-reads the value each pass and
        clicks toward the target, stopping when it's reached, capped, or unreadable."""
        page = self.rt.page
        try:
            target = int(target)
        except Exception:
            return False
        if target < 0:
            return False
        inc = page.locator(f"[data-testid='stepper-floorPlan{field}-increase-button']").first
        dec = page.locator(f"[data-testid='stepper-floorPlan{field}-decrease-button']").first
        val = page.locator(f"[data-testid='stepper-floorPlan{field}-value']").first
        try:
            if inc.count() == 0 and dec.count() == 0:
                return False
        except Exception:
            return False

        def _read():
            try:
                if val.count():
                    txt = (val.inner_text() or "").strip()
                    digits = "".join(ch for ch in txt if ch.isdigit())
                    if digits:
                        return int(digits)
            except Exception:
                pass
            return None

        clicked = 0
        for _ in range(40):
            cur = _read()
            if cur is None:                            # unreadable — blind-drive from 1 (Airbnb default)
                for _ in range(max(0, target - 1)):
                    try:
                        if inc.count():
                            self._click_robust(inc); clicked += 1; self.rt.think(0.15, 0.3)
                    except Exception:
                        break
                break
            if cur == target:
                break
            btn = inc if cur < target else dec
            try:
                if btn.count() == 0:
                    break
            except Exception:
                break
            self._click_robust(btn); clicked += 1; self.rt.think(0.15, 0.3)
            nxt = _read()
            if nxt is not None and nxt == cur:         # the click didn't move it (capped/disabled)
                break
        if clicked:
            self._say(f"  · Airbnb {field.lower()} → {target}")
        return clicked > 0

    def _fill_airbnb_floor_plan(self, profile_data) -> bool:
        """Airbnb 'Share some basics about your place' — the Guests/Bedrooms/Beds/Bathrooms
        steppers. Blocks the LLM (the whole page is these counters)."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
            url = (page.url or "").lower()
        except Exception:
            return False
        try:
            has_stepper = page.locator("[data-testid^='stepper-floorPlan']").count() > 0
        except Exception:
            has_stepper = False
        if not ("share some basics about your place" in body or "/floor-plan" in url or has_stepper):
            return False
        counts = self._airbnb_floor_counts(profile_data)
        did = 0
        for field in ("Guests", "Bedrooms", "Beds", "Bathrooms"):
            if self._airbnb_set_stepper(field, counts[field]):
                did += 1
        if did:
            self._say(f"  ✓ Airbnb floor plan — {counts} ({did} stepper(s))")
        return did > 0

    def _airbnb_amenity_labels(self, profile_data) -> list:
        """Map the profile's facilities → Airbnb amenity-tile labels."""
        f = profile_data.get("facilities", {}) or {}
        out: list = []

        def add(x):
            if x and x not in out:
                out.append(x)

        if (f.get("internet", {}) or {}).get("wifi", True):
            add("Wifi")
        park = f.get("parking", {}) or {}
        if park.get("available"):
            add("Free parking on premises" if str(park.get("type")) == "free"
                else "Paid parking on premises")
        if f.get("swimming_pool"):
            add("Pool")
        if f.get("air_conditioning"):
            add("Air conditioning")
        if f.get("fitness_center"):
            add("Exercise equipment")
        if f.get("ev_charging"):
            add("EV charger")
        if f.get("elevator"):
            add("Elevator")
        if f.get("laundry"):
            add("Washer")
        if (f.get("breakfast", {}) or {}).get("available"):
            add("Breakfast")
        ras = set()
        for r in profile_data.get("room_types", []) or []:
            for a in (r.get("room_amenities") or []):
                ras.add(str(a).lower())
        if "tv" in ras:
            add("TV")
        if "ac" in ras:
            add("Air conditioning")
        if any(k in ras for k in ("kitchen", "kitchenette")):
            add("Kitchen")
        return out

    def _fill_airbnb_amenities(self, profile_data) -> bool:
        """Airbnb 'Tell guests what your place has to offer' (+ the 'standout amenities'
        screen) — toggle each amenity tile the profile declares. Idempotent: skips a tile
        already pressed. Blocks the LLM."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
            url = (page.url or "").lower()
        except Exception:
            return False
        if not ("what your place has to offer" in body or "standout amenities" in body
                or "tell guests what your place" in body
                or "/amenities" in url or "/stand-out" in url):
            return False
        did = 0
        for lab in self._airbnb_amenity_labels(profile_data):
            for getter in (lambda lab=lab: page.get_by_role("button", name=lab),
                           lambda lab=lab: page.get_by_text(lab, exact=False)):
                try:
                    loc = getter().first
                    if not (loc.count() and loc.is_visible()):
                        continue
                    pressed = (loc.get_attribute("aria-pressed")
                               or loc.get_attribute("aria-checked") or "").lower()
                    if pressed == "true":
                        break                          # already on
                    self._click_robust(loc); did += 1
                    self._say(f"  · Airbnb amenity → {lab}")
                    break
                except Exception:
                    continue
        if did:
            self._say(f"  ✓ Airbnb amenities — {did} selected")
        return did > 0

    def _fill_airbnb_title(self, profile_data) -> bool:
        """Airbnb 'Now, let's give your place a title' — fill the title field with the
        property name (Airbnb caps it at 50 chars). Blocks the LLM."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
            url = (page.url or "").lower()
        except Exception:
            return False
        if not ("give your place a title" in body or "create your title" in body
                or "/title" in url):
            return False
        title = (profile_data.get("display_name") or "").strip()[:50]
        if not title:
            return False
        for sel in ("textarea[name*='title' i]", "textarea#title", "textarea",
                    "input[name*='title' i]", "input[aria-label*='title' i]"):
            el = self._find_visible(page, sel)
            if el is None:
                continue
            try:
                if (el.input_value() or "").strip():
                    return True                        # already has a title
            except Exception:
                pass
            if self._set_react_input(el, title):
                self._say(f"  ✓ Airbnb title → “{title}”")
                return True
        return False

    def _fill_airbnb_description(self, profile_data) -> bool:
        """Airbnb 'Create your description' — fill the description textarea (Airbnb caps it
        at 500 chars). Falls back to the first room's description. Blocks the LLM."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
            url = (page.url or "").lower()
        except Exception:
            return False
        if not ("create your description" in body or "how would you describe" in body
                or "/description" in url):
            return False
        desc = (profile_data.get("description") or "").strip()
        if not desc:
            for r in profile_data.get("room_types", []) or []:
                if (r.get("description") or "").strip():
                    desc = r["description"].strip(); break
        if not desc:
            return False
        desc = desc[:500]
        for sel in ("textarea[name*='description' i]", "textarea#description", "textarea",
                    "input[aria-label*='description' i]"):
            el = self._find_visible(page, sel)
            if el is None:
                continue
            try:
                if (el.input_value() or "").strip():
                    return True
            except Exception:
                pass
            if self._set_react_input(el, desc):
                self._say(f"  ✓ Airbnb description ({len(desc)} chars)")
                return True
        return False

    def _fill_airbnb_price(self, profile_data) -> bool:
        """Airbnb 'Now, set your price' — fill the nightly base price with the cheapest
        room's base_rate (the entry price). Blocks the LLM."""
        page = self.rt.page
        try:
            body = (page.inner_text("body") or "").lower()
            url = (page.url or "").lower()
        except Exception:
            return False
        if not ("set your price" in body or "set a weekday price" in body
                or "set your base price" in body or "/price" in url):
            return False
        rates = []
        for r in profile_data.get("room_types", []) or []:
            try:
                if r.get("base_rate"):
                    rates.append(int(float(r["base_rate"])))
            except Exception:
                pass
        if not rates:
            return False
        price = min(rates)
        for sel in ("input#price", "[data-testid='price-input'] input", "input[name*='price' i]",
                    "input[inputmode='numeric']", "input[aria-label*='price' i]"):
            el = self._find_visible(page, sel)
            if el is None:
                continue
            try:
                cur = (el.input_value() or "").strip().lstrip("₹$").replace(",", "")
                if cur and cur not in ("0", ""):
                    return True
            except Exception:
                pass
            if self._set_react_input(el, str(price)):
                self._say(f"  ✓ Airbnb base price → {price}")
                return True
        return False

    def _dropdown_already_set(self, loc, want, require_time: bool = False) -> bool:
        """True if a dropdown already shows a real (non-placeholder) selection, so we don't
        re-open / re-pick it every pass. Works for a native <select> (check its value) and a
        custom button (check its visible text). `require_time` treats 'already set' as 'shows
        a digit' (a time has been chosen)."""
        try:
            is_select = loc.evaluate("e => e.tagName.toLowerCase()") == "select"
        except Exception:
            is_select = False
        try:
            if is_select:
                cur = (loc.input_value() or "").strip()
            else:
                cur = (loc.inner_text() or "").strip()
        except Exception:
            return False
        if not cur:
            return False
        low = cur.lower()
        if require_time:
            return any(ch.isdigit() for ch in cur)
        if low in ("select", "choose", "select...", "please select") or low.startswith("select "):
            return False
        # native <select>: input_value() is the chosen option's VALUE (a code like 'IN' that
        # needn't textually match the label 'India') — any non-placeholder value means set.
        if is_select:
            return True
        # custom button: any non-placeholder visible text means a choice was made.
        return "select" not in low and "choose" not in low

    def _current_unit_id(self):
        """Booking gives each room a unique unit_id in the URL — use it to count units
        reliably (so manual + auto 'Add unit' clicks don't double-count)."""
        try:
            from urllib.parse import urlparse, parse_qs
            return parse_qs(urlparse(self.rt.page.url).query).get("unit_id", [None])[0]
        except Exception:
            return None

    def _is_unit_flow(self) -> bool:
        """Are we on a per-room page? These vary per room (name/rate/beds differ), so they
        must NEVER be cached — a cached map would fill every room with room 1's data.
        Booking marks them in the URL; MMT shows a 'Create Room' header."""
        try:
            url = (self.rt.page.url or "").lower()
        except Exception:
            url = ""
        if ("core_subflow_room_setup" in url or "unit_id=" in url or "/unit-" in url
                or "/price.html" in url or "/price-overview" in url or "/unit." in url):
            return True
        if self.ota == "makemytrip":                  # MMT 'Create Room' accordion (per room)
            try:
                if self.rt.page.locator(
                        "[data-test-id=room-creation-header], #room-creation-header").count() > 0:
                    return True
            except Exception:
                pass
        return False

    def _count_existing_units(self) -> int:
        """Count rooms ALREADY on the overview (each unit card shows 'Rooms of this type').
        So a re-run on a session that already has units doesn't add extra ones."""
        try:
            body = (self.rt.page.inner_text("body") or "").lower()
            return body.count("rooms of this type")
        except Exception:
            return 0

    def _try_progress_button(self) -> bool:
        """When there's no plain Continue, click a 'next action' CTA so the walk flows
        into the sub-flows: Units first (capped at the number of room types), then
        Photos (only if we have photo files). NEVER clicks the payments / Final-steps
        gate — that's the operator's step."""
        order = []
        # offer 'Add unit' only while FEWER rooms exist than we have room types. Count by
        # BOTH unit_ids seen this run AND units already on the overview (so re-runs / manual
        # clicks never over-add).
        units_started = max(len(getattr(self, "_unit_order", [])), self._count_existing_units())
        if units_started < getattr(self, "_unit_target", 0):
            nxt = ""
            if 0 <= units_started < len(getattr(self, "_room_list", [])):
                nxt = self._room_list[units_started].get("name", "")
            order += ["Add unit", "Add your first unit", "Add a unit", "Add another unit",
                      "Add room", "Add your first room", "Add a room"]
        else:
            nxt = ""
        if getattr(self, "_photo_paths", None):
            order += ["Add photos", "Add your photos", "Upload photos", "Add your first photos"]
        # once rooms + photos are done, enter the Final step (invoicing/GST). The gate
        # below still HARD-STOPS on real bank/card details or a publish/terms button.
        order += ["Add final details", "Add your final details", "Complete final steps"]
        # generic "start a listing/onboarding" CTAs — covers MMT/Goibibo 'List New Property'
        # etc. (harmless on Booking, which has no such button)
        order += ["List New Property", "List new property", "List a property",
                  "List your property", "Add property", "Add a property", "Add new property",
                  "Create listing", "Start listing", "List Property",
                  "Get started", "Let’s get started", "Let's get started", "Continue setup"]
        clicked = getattr(self, "_clicked_ctas", set())
        for lab in order:
            is_unit = ("unit" in lab.lower() or "room" in lab.lower())
            if not is_unit and lab in clicked:        # already clicked this CTA — don't loop on it
                continue
            loc = self._locate("text", lab)
            if loc is not None:
                try:
                    self._click_robust(loc)
                    if is_unit:
                        self._say(f"  · clicked ‘{lab}’ → adding room {units_started + 1}/"
                                  f"{self._unit_target}{(' — ' + nxt) if nxt else ''}")
                    else:
                        clicked.add(lab)
                        self._say(f"  · clicked ‘{lab}’ → continuing setup")
                    return True
                except Exception:
                    pass
        return False

    def _act(self, loc, alias, val, action, *, cached: bool) -> bool:
        tag = " (cached)" if cached else ""
        try:
            if action == "click":
                self._click_robust(loc); self._say(f"  ✓ clicked {alias}{tag}"); return True
            if action == "select" and val:
                if self._select_robust(loc, val):
                    self._say(f"  ✓ {alias} = {val}{tag}"); return True
                self._say(f"  – {alias}: couldn't set '{val}'{tag}"); return False
            if action == "fill" and val:
                if self._is_counter(loc):              # +/- stepper, not a text box → click +/-
                    if self._set_counter(loc, val):
                        self._say(f"  ✓ {alias} = {val} (stepper){tag}"); return True
                    self._say(f"  – {alias}: stepper not settable yet (disabled?){tag}"); return False
                try:
                    loc.fill(str(val), timeout=5000)
                except Exception:
                    if not self._select_robust(loc, val):   # maybe it's a dropdown, not a text box
                        raise
                self._say(f"  ✓ {alias} = {str(val)[:40]}{tag}"); return True
            if action == "check":
                self._check_robust(loc); self._say(f"  ✓ checked {alias}{tag}"); return True
            if action == "uncheck":
                try:
                    loc.uncheck(timeout=3500)
                except Exception:
                    if loc.is_checked():
                        self._click_robust(loc)
                self._say(f"  ✓ unchecked {alias}{tag}"); return True
        except Exception as e:
            self._say(f"  – {alias}: {type(e).__name__}{tag}")
        return False

    def _apply_actions(self, actions, *, cached) -> tuple:
        """Apply LLM-decided actions ([{selector, action, value}]). Returns (did, navigated)."""
        page = self.rt.page
        did = 0; navigated = False
        for a in actions or []:
            sel = a.get("selector"); action = a.get("action"); val = a.get("value", "")
            if not sel or not action:
                continue
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    # the live data-ap-id can be stripped by a React re-render mid-page;
                    # fall back to the element's stable id/name/test-id selector
                    st = a.get("stable")
                    if st:
                        loc = page.locator(st).first
                    if loc.count() == 0:
                        continue
            except Exception:
                continue
            before = self._page_sig()
            alias = (a.get("label") or sel.split(">")[-1].strip())[:30]
            if self._act(loc, alias, val, action, cached=cached):
                did += 1
            if action in ("click", "check", "uncheck"):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=4000)
                except Exception:
                    pass
                if self._page_sig() != before:
                    navigated = True; break
        return did, navigated

    def _resolve_cached(self, sel_map, alias):
        sel = sel_map.get(alias)
        if not sel:
            return None, None
        try:
            loc = self.rt.page.locator(sel).first
            if loc.count() == 0:
                return None, None
            return loc, sel
        except Exception:
            return None, None

    def _resolve_learn(self, els, alias):
        loc = getattr(els, alias, None)
        if loc is None:
            return None, None
        return loc, self._cacheable_selector(loc)

    def _apply(self, p, resolve, *, cached):
        """Run the plan. resolve(alias)->(locator|None, selector|None). Stops early
        and returns navigated=True if a click changed the page."""
        page = self.rt.page
        did = 0; sel_map = {}; navigated = False
        for alias, val, action in self._plan(p):
            loc, sel = resolve(alias)
            if loc is None:
                continue
            if sel:
                sel_map[alias] = sel
            before = page.url
            if self._act(loc, alias, val, action, cached=cached):
                did += 1
            if action == "click":
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                if page.url != before:
                    navigated = True
                    break
        return did, sel_map, navigated

    # ---- deterministic Booking rules (stable selectors + labels) ---------
    def _locate(self, kind, value):
        page = self.rt.page
        try:
            if kind == "css":
                loc = page.locator(value)
            elif kind == "label":
                loc = page.get_by_label(value, exact=False)
                if loc.count() == 0:
                    loc = page.get_by_placeholder(value)
                if loc.count() == 0:
                    loc = page.get_by_text(value, exact=False)
            elif kind == "text":
                loc = page.get_by_text(value, exact=True)
                if loc.count() == 0:
                    loc = page.get_by_text(value, exact=False)   # forgiving contains match
            else:
                return None
            loc = loc.first
            if loc.count() == 0:
                return None
            try:
                if not loc.is_visible():
                    return None
            except Exception:
                pass
            return loc
        except Exception:
            return None

    def _frames(self):
        """Main page + all iframes — Booking's address widget lives in an iframe,
        which is why a main-page input search returns 0."""
        page = self.rt.page
        ctxs = [page]
        try:
            for f in page.frames:
                if f is not page.main_frame:
                    ctxs.append(f)
        except Exception:
            pass
        return ctxs

    def _fill_labeled_address(self, fields: dict) -> int:
        """Fill the structured 'Address form' fields by label, across all frames."""
        label_map = {
            # NOTE: no bare "address" here — it would match the "Find Your Address"
            # autocomplete search box and dump the street into it without geocoding.
            "line1": ["street and house", "street name and house", "street", "house number",
                      "building", "address line 1", "address line one"],
            "city": ["city", "town"],
            "state": ["state", "region", "province"],
            "postal": ["postal code", "postcode", "zip", "pin code", "post code"],
            "country": ["country/region", "country"],
        }
        filled = 0
        for key, labels in label_map.items():
            val = fields.get(key)
            if not val:
                continue
            done = False
            for ctx in self._frames():
                for lab in labels:
                    try:
                        l = ctx.get_by_label(lab, exact=False).first
                        if l.count() == 0:
                            l = ctx.get_by_placeholder(lab).first
                        if l.count() == 0 or not l.is_visible():
                            continue
                        tag = ""
                        try:
                            tag = l.evaluate("e => e.tagName.toLowerCase()")
                        except Exception:
                            pass
                        if tag == "select":           # Country/Region dropdown
                            ok = False
                            for how in ("label", "value"):
                                try:
                                    l.select_option(**{how: str(val)})
                                    ok = True; break
                                except Exception:
                                    pass
                            if not ok:
                                continue
                            self._say(f"  ✓ address.{key} = {val} (select)")
                            filled += 1; done = True; break
                        if l.is_editable():
                            l.fill(str(val))
                            try:                      # fire events so Booking geocodes the map
                                l.dispatch_event("input")
                                l.dispatch_event("change")
                            except Exception:
                                pass
                            self._say(f"  ✓ address.{key} = {val}")
                            filled += 1; done = True; break
                    except Exception:
                        pass
                if done:
                    break
        return filled

    def _find_address_input(self, quiet: bool = False):
        """Find the address autocomplete box. Booking labels it 'Find Your Address' or
        'Address' (newer A/B: 'Where is your property?'); the element id is a volatile
        React id like ':r7:' so we NEVER match by id — only by label/placeholder/role.
        Search main page + iframes, prefer an address-labelled input, never the language
        select or a readonly field."""
        import re
        want = re.compile(r"address|find your|where is your property|location|street|adres", re.I)

        def _is_input(loc) -> bool:
            """Guard: only ever return a real <input>. A <select> (the language
            dropdown) ALSO has role=combobox — typing + Enter into it silently
            changes the site language. Never let that happen."""
            try:
                return (loc.evaluate("e => e.tagName.toLowerCase()") == "input")
            except Exception:
                return False

        first_text = None
        for ctx in self._frames():
            where = "iframe" if ctx is not self.rt.page else "main"
            # 1) label / placeholder / role — most reliable, layout-independent.
            #    'address' placeholders are language-specific, so the input[role=combobox]
            #    CSS fallback (input-scoped, never <select>) is what usually catches it.
            for desc, build in (
                ("label 'Find Your Address'", lambda: ctx.get_by_label("Find Your Address", exact=False)),
                ("placeholder 'address'",     lambda: ctx.get_by_placeholder("address")),
                ("placeholder 'Find Your'",   lambda: ctx.get_by_placeholder("Find Your Address")),
                ("input[role=combobox]",      lambda: ctx.locator("input[role='combobox']")),
            ):
                try:
                    l = build().first
                    if l.count() > 0 and l.is_visible() and l.is_editable() and _is_input(l):
                        self._say(f"  · address input found ({where}) via {desc}")
                        return l
                except Exception:
                    pass
            # 2) scan text/search inputs, pick the one whose placeholder/aria says address
            for sel in ("input[type='search']", "input[role='combobox']",
                        "input[type='text']:not([readonly])"):
                try:
                    loc = ctx.locator(sel)
                    for i in range(min(loc.count(), 12)):
                        el = loc.nth(i)
                        try:
                            if not (el.is_visible() and el.is_editable()):
                                continue
                            blob = ((el.get_attribute("placeholder") or "") + " " +
                                    (el.get_attribute("aria-label") or ""))
                            if want.search(blob):
                                self._say(f"  · address input found ({where}) via {sel} (placeholder/aria matched)")
                                return el
                            if first_text is None:
                                first_text = el
                        except Exception:
                            pass
                except Exception:
                    pass
        if first_text is not None:
            if not quiet:
                self._say("  · address input: no address-labelled field found — using first visible text input")
            return first_text
        if not quiet:
            self._say("  · address input: NONE found (page may not have rendered, or it's a non-address page)")
        return None

    def _suggestions(self, ctx):
        """Visible autocomplete options in this frame (main page or iframe)."""
        for ssel in ("[role='option']", "li[role='option']", "ul[role='listbox'] li",
                     "[data-testid*='suggestion']", "[class*='autocomplete'] li",
                     "[class*='suggestion']"):
            try:
                loc = ctx.locator(ssel)
                n = loc.count()
                if not n:
                    continue
                opts = []
                for i in range(min(n, 8)):
                    o = loc.nth(i)
                    try:
                        if o.is_visible() and (o.inner_text() or "").strip():
                            opts.append(o)
                    except Exception:
                        pass
                if opts:
                    return opts
            except Exception:
                pass
        return []

    def _pick_suggestion(self, prefer: str = "") -> bool:
        """Wait for the autocomplete dropdown, then click the suggestion that best
        matches `prefer` (our city), else the first one. Booking drops the map pin
        automatically once a real suggestion is chosen — no manual map click needed."""
        want = (prefer or "").strip().lower()
        waited = 0.0
        while waited < 6.0:
            for ctx in self._frames():
                opts = self._suggestions(ctx)
                if not opts:
                    continue
                chosen = None
                if want:
                    for o in opts:
                        try:
                            if want in (o.inner_text() or "").lower():
                                chosen = o; break
                        except Exception:
                            pass
                chosen = chosen or opts[0]
                try:
                    txt = (chosen.inner_text() or "").strip().replace("\n", " ")[:64]
                    chosen.click()
                    self._say(f"  ✓ address — picked suggestion: {txt}")
                    return True
                except Exception:
                    pass
            self.rt.think(0.5, 0.7)
            waited += 0.6
        return False

    def _force_english(self):
        """Booking sometimes serves a non-English language; our text-based rules
        (stars, Yes/No, amenities, channel-manager) only match English. Set the
        #lang <select> to English once at the start. Best-effort — harmless if absent."""
        for ctx in self._frames():
            try:
                sel = ctx.locator("#lang").first
                if sel.count() == 0:
                    continue
                try:
                    cur = (sel.input_value() or "").lower()
                except Exception:
                    cur = ""
                if cur in ("en", "xu"):
                    return
                for val in ("xu", "en"):
                    try:
                        sel.select_option(val)
                        self._say(f"  · forced site language to English ({val})")
                        try:
                            self.rt.page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        self.rt.think(0.8, 1.4)
                        return
                    except Exception:
                        pass
            except Exception:
                pass

    def _wait_sig_change(self, before: str, timeout: float = 6.0) -> bool:
        """Poll up to `timeout`s for the page signature to change (SPA content swap)."""
        waited = 0.0
        while waited < timeout:
            try:
                self.rt.page.wait_for_load_state("domcontentloaded", timeout=1500)
            except Exception:
                pass
            if self._page_sig() != before:
                return True
            self.rt.think(0.4, 0.7)
            waited += 0.6
        return False

    def _diag_stuck(self):
        """When Continue didn't advance, say WHY: is the primary button disabled, and
        what required/empty fields remain? Turns a dead stop into an actionable line."""
        page = self.rt.page
        try:
            disabled = page.locator('[data-testid="FormButtonPrimary-disabled"]').count() > 0
            if disabled:
                self._say("      · Continue is DISABLED — a required field on this page isn't filled yet")
        except Exception:
            pass
        # any 'pin / address' warning visible? (the usual real blocker on the address page)
        try:
            if not self._address_accepted():
                self._say("      · the map PIN / address isn't accepted yet (Booking shows a pin warning)")
        except Exception:
            pass
        # list visible empty text inputs — but skip OPTIONAL ones (e.g. apartment/floor)
        try:
            empties = page.evaluate(r"""() => {
              const out=[];
              document.querySelectorAll('input[type=text], input:not([type]), textarea').forEach(el=>{
                const r=el.getBoundingClientRect();
                if (r.width>0 && r.height>0 && !el.value) {
                  const lab=(el.getAttribute('aria-label')||el.getAttribute('placeholder')||'').toLowerCase();
                  if (lab.includes('optional') || lab.includes('apartment') || lab.includes('floor')) return;
                  out.push((el.getAttribute('aria-label')||el.getAttribute('placeholder')||('#'+el.id)).slice(0,40));
                }
              });
              return out.slice(0,8);
            }""")
            if empties:
                self._say("      · empty required field(s) here: " + ", ".join(empties))
        except Exception:
            pass

    def _diag_inputs(self):
        """Dump every visible text-ish control (input/textarea/contenteditable/combobox/
        textbox) across main + iframes, so we can see what the Quick-search box actually
        IS when the finder misses it. Diagnostic only — drives the next selector fix."""
        js = r"""() => {
          const out=[]; let shadowHosts=0;
          function walk(root){
            let nodes=[];
            try { nodes = root.querySelectorAll(
              'input,textarea,[contenteditable=""],[contenteditable="true"],'+
              '[role="combobox"],[role="textbox"],[role="searchbox"]'); } catch(e){}
            nodes.forEach(el=>{ const r=el.getBoundingClientRect();
              out.push({ tag: el.tagName.toLowerCase(), type: el.getAttribute('type')||'',
                role: el.getAttribute('role')||'', id:(el.id||'').slice(0,18),
                ph:(el.getAttribute('placeholder')||'').slice(0,26),
                al:(el.getAttribute('aria-label')||'').slice(0,26),
                ce: el.getAttribute('contenteditable')||'', vis:(r.width>0&&r.height>0),
                shadow: (root!==document) }); });
            let all=[]; try{ all=root.querySelectorAll('*'); }catch(e){}
            all.forEach(el=>{ if(el.shadowRoot){ shadowHosts++; walk(el.shadowRoot); } });
          }
          walk(document);
          return { items: out.slice(0,40), shadowHosts };
        }"""
        lines = []
        try:
            self._say(f"  · DIAG — {len(self._frames())} frame(s) on this page")
        except Exception:
            pass
        for ctx in self._frames():
            where = "iframe" if ctx is not self.rt.page else "main"
            try:
                furl = (ctx.url or "")
            except Exception:
                furl = "?"
            short = furl.replace("https://", "").replace("http://", "")[:60]
            try:
                res = ctx.evaluate(js)
                items = res.get("items", [])
                self._say(f"  · DIAG {where} [{short}]: {len(items)} control(s), "
                          f"{res.get('shadowHosts', 0)} shadow host(s)")
            except Exception as e:
                self._say(f"  · DIAG {where} [{short}]: evaluate failed ({type(e).__name__})")
                items = []
            for it in items:
                if not it.get("vis"):
                    continue
                lines.append(
                    f"{where}{'/shadow' if it.get('shadow') else ''}: {it['tag']} "
                    f"type={it['type']!r} role={it['role']!r} ce={it['ce']!r} "
                    f"id={it['id']!r} ph={it['ph']!r} al={it['al']!r}")
        if lines:
            self._say("  · DIAG — visible text-ish controls on this page:")
            for s in lines[:16]:
                self._say("      " + s)
        else:
            self._say("  · DIAG — no input/textarea/combobox/contenteditable is visible here")

    def _pick_suggestion_deep(self, prefer: str = "") -> bool:
        """Pierce shadow DOM to click the autocomplete suggestion (the dropdown is often
        a web component too). Prefers the option matching our city."""
        js = r"""(prefer) => {
          const out=[];
          function walk(root){
            let nodes=[];
            try { nodes = root.querySelectorAll(
              '[role=option],li[role=option],ul[role=listbox] li,[data-testid*=suggestion],'+
              '[class*=autocomplete] li,[class*=suggestion],[class*=Suggestion],ul li'); } catch(e){}
            nodes.forEach(n=>{ const r=n.getBoundingClientRect(); const t=(n.innerText||'').trim();
              if(r.width>0 && r.height>0 && t) out.push(n); });
            let all=[]; try{ all=root.querySelectorAll('*'); }catch(e){}
            all.forEach(el=>{ if(el.shadowRoot) walk(el.shadowRoot); });
          }
          walk(document);
          if(!out.length) return null;
          let pick=null;
          if(prefer){ pick=out.find(n=>(n.innerText||'').toLowerCase().includes(prefer.toLowerCase())); }
          return pick || out[0];
        }"""
        want = (prefer or "").strip()
        for _ in range(8):                            # wait for the dropdown to populate
            for ctx in self._frames():
                try:
                    h = ctx.evaluate_handle(js, want)
                    el = h.as_element()
                    if el is not None:
                        txt = (el.inner_text() or "").strip().replace("\n", " ")[:60]
                        el.click()
                        self._say(f"  ✓ address — picked suggestion (shadow): {txt}")
                        return True
                except Exception:
                    pass
            self.rt.think(0.4, 0.7)
        return False

    def _ensure_quick_search_tab(self) -> bool:
        """Activate the 'Quick search' tab (stable id #SEARCH-tab-trigger) so Booking
        shows the Google-Places box — picking a suggestion there auto-drops the map
        pin (no manual map click). Falls back to the visible tab text."""
        for ctx in self._frames():
            for desc, build in (
                ("#SEARCH-tab-trigger", lambda: ctx.locator("#SEARCH-tab-trigger")),
                ("text 'Quick search'", lambda: ctx.get_by_text("Quick search", exact=False)),
            ):
                try:
                    t = build().first
                    if t.count() > 0 and t.is_visible():
                        t.click()
                        self.rt.think(0.6, 1.0)
                        self._say(f"  · address: on ‘Quick search’ tab (via {desc})")
                        return True
                except Exception:
                    pass
        return False

    def _address_accepted(self) -> bool:
        """True once Booking no longer shows the 'set your pin / enter address' warning."""
        try:
            body = (self.rt.page.inner_text("body") or "").lower()
        except Exception:
            return True
        warns = ("set your property location", "place the pin", "placing the pin",
                 "pin on the exact", "enter an address", "enter your address",
                 "select an address from", "add your address",
                 "something wrong with the pin", "wrong with the pin on the map",
                 "move the pin", "pin location is incorrect", "couldn't find this address")
        return not any(w in body for w in warns)

    def _place_map_pin(self) -> bool:
        """Drop the location pin Booking requires before Continue. Clicks the map centre
        (after geocoding recentres it on the city). If the map won't accept it, the
        operator clicks the exact spot once."""
        page = self.rt.page
        self.rt.think(2.0, 3.0)            # let the map geocode/recentre on the city
        for msel in (".leaflet-container", "[class*='leaflet']", "div[aria-label*='Map' i]",
                     "[class*='MapContainer']", "[class*='map-container']",
                     "[id*='map']", "[class*='gm-style']", "[class*='map']"):
            try:
                m = page.locator(msel).first
                if m.count() > 0 and m.is_visible():
                    box = m.bounding_box()
                    if box and box["width"] > 250 and box["height"] > 200:
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        self.rt.think(0.8, 1.4)
                        self._say("  · placed a pin at the map centre (nudge it in the window if it's off)")
                        return True
            except Exception:
                pass
        self._say("  ⏸ map pin — click the exact spot on the map in the window, then Continue/Fill")
        return False

    def _wait_address_widget(self, timeout: float = 8.0) -> bool:
        """Booking's address widget renders LAZILY — the tab triggers / inputs appear a
        second or two after the page. Wait for them so we don't search an empty page
        (the exact bug: we looked, found nothing, and fell back too early)."""
        waited = 0.0
        while waited < timeout:
            for ctx in self._frames():
                try:
                    if ctx.locator("#SEARCH-tab-trigger, #MANUAL-tab-trigger").first.count() > 0:
                        return True
                    inp = ctx.locator(
                        "input[type='text'], input[type='search'], input[role='combobox']").first
                    if inp.count() > 0 and inp.is_visible():
                        return True
                except Exception:
                    pass
            self.rt.think(0.5, 0.8)
            waited += 0.7
        return False

    def _goto_address_form_tab(self):
        for ctx in self._frames():
            try:
                tab = ctx.locator("#MANUAL-tab-trigger").first
                if tab.count() == 0:
                    tab = ctx.get_by_text("Address form", exact=False).first
                if tab.count() > 0 and tab.is_visible():
                    tab.click()
                    self.rt.think(1.0, 1.6)
                    self._say("  · address: on ‘Address form’ tab")
                    return
            except Exception:
                pass

    def _human_pin_handoff(self) -> bool:
        """The map pin is a genuinely interactive widget; if auto-placement fails, pause
        and let the operator click it ONCE, then resume. Reuses the dashboard's Done
        button. This is the single manual touch per property — only when auto fails."""
        self.state = "awaiting_captcha"
        self._captcha.clear()
        self._say("⏸ MAP PIN — Booking needs the location pin set. In the browser window, click "
                  "(or drag) the pin onto the spot, then click Done. One tap and it continues.")
        self._captcha.wait(); self._captcha.clear()
        self.state = "filling"
        ok = self._address_accepted()
        self._say("  ✓ pin placed — continuing" if ok else "  · continuing (pin warning may persist)")
        return True

    def _clear_input(self, loc):
        """Hard-clear a field (Booking may have a saved value) with real key events so
        React state actually resets, not just the DOM value."""
        try:
            loc.click()
        except Exception:
            pass
        for attempt in (lambda: loc.fill(""),
                        lambda: (loc.press("Control+a"), loc.press("Delete"))):
            try:
                attempt()
            except Exception:
                pass
        try:
            return (loc.input_value() or "").strip() == ""
        except Exception:
            return False

    def _finish_pin(self) -> bool:
        """After an address is entered, make sure the map pin is satisfied: wait for
        geocode (auto-pin), else centre-click, else one operator click. Returns True."""
        self.rt.think(1.8, 2.8)                       # let Booking geocode + auto-place the pin
        if self._address_accepted():
            self._say("  ✓ address — pin set by Booking")
            return True
        self._place_map_pin()
        self.rt.think(1.5, 2.5)
        if self._address_accepted():
            self._say("  ✓ address — pin accepted")
            return True
        return self._human_pin_handoff()              # last resort: one operator click

    def _find_address_input_deep(self, quiet: bool = False):
        """Pierce OPEN shadow DOM (web components) to reach the address box when normal
        selectors AND raw querySelectorAll see nothing. Returns a Playwright
        ElementHandle (supports click/fill/type/press) or None. Runs per frame."""
        js = r"""() => {
          const out=[];
          function walk(root){
            let nodes=[];
            try { nodes = root.querySelectorAll(
              'input,textarea,[contenteditable=""],[contenteditable="true"],'+
              '[role="combobox"],[role="textbox"],[role="searchbox"]'); } catch(e){}
            nodes.forEach(n=>{ const r=n.getBoundingClientRect();
              if(r.width>0 && r.height>0) out.push(n); });
            let all=[];
            try { all = root.querySelectorAll('*'); } catch(e){}
            all.forEach(el=>{ if(el.shadowRoot) walk(el.shadowRoot); });
          }
          walk(document);
          if(!out.length) return null;
          const score=el=>{ const b=((el.getAttribute('placeholder')||'')+' '+
            (el.getAttribute('aria-label')||'')).toLowerCase();
            return /address|adres|find your|where is|location|street|search/.test(b)?1:0; };
          out.sort((a,b)=>score(b)-score(a));
          return out[0];
        }"""
        for ctx in self._frames():
            try:
                handle = ctx.evaluate_handle(js)
                el = handle.as_element()
                if el is not None:
                    if not quiet:
                        where = "iframe" if ctx is not self.rt.page else "main"
                        self._say(f"  · address box found via shadow-DOM scan ({where})")
                    return el
            except Exception:
                pass
        return None

    def _gmp_committed(self) -> bool:
        """Did a place actually get selected in the <gmp-place-autocomplete>? Check the
        component's value and the place-details panel (which un-hides on selection).
        Do NOT use 'no warning' as the signal — that's true even when the box is empty."""
        for ctx in self._frames():
            try:
                ok = ctx.evaluate(r"""() => {
                  const a = document.querySelector('gmp-place-autocomplete');
                  if (a && ((a.getAttribute('value')||'').trim().length > 2)) return true;
                  const d = document.querySelector('[data-testid="place-details-container"]');
                  if (d && getComputedStyle(d).display !== 'none') return true;
                  return false;
                }""")
                if ok:
                    return True
            except Exception:
                pass
        return False

    def _fill_gmp_autocomplete(self, candidates, prefer_city) -> bool:
        """Address box = Google <gmp-place-autocomplete>: the <input> AND the suggestion
        dropdown both live in its CLOSED shadow root, unreachable from the DOM. The host
        element is in the light DOM, so: click it (focus the inner input), keyboard-type,
        wait for Places to populate, then ↓ (highlight first suggestion) + Enter (select).
        Verify a place was actually selected — never click DOM 'suggestions' (those are
        the page's tabs/menus, not Places results)."""
        page = self.rt.page
        el = None
        for ctx in self._frames():
            try:
                cand = ctx.locator("gmp-place-autocomplete").first
                if cand.count() == 0:
                    cand = ctx.locator("[data-testid='autocomplete-container']").first
                if cand.count() > 0 and cand.is_visible():
                    el = cand; break
            except Exception:
                pass
        if el is None:
            return False
        for q in candidates:
            try:
                el.click()                                    # focus the inner (shadow) input
                self.rt.think(0.3, 0.6)
                try:
                    page.keyboard.press("Control+a"); page.keyboard.press("Delete")
                except Exception:
                    pass
                page.keyboard.type(str(q), delay=70)
                self._say(f"  · Quick search (Google UI Kit): typed '{str(q)[:46]}'")
                self.rt.think(2.2, 3.2)                       # Places debounces — wait for results
                page.keyboard.press("ArrowDown"); self.rt.think(0.7, 1.1)   # highlight first result
                page.keyboard.press("Enter"); self.rt.think(1.6, 2.4)       # select it
                if self._gmp_committed():
                    self._say(f"  ✓ address — selected a place for ‘{prefer_city or q}’")
                    return True
                self._say(f"  – no place selected for '{str(q)[:30]}' — trying a broader query")
            except Exception as e:
                self._say(f"  – gmp try '{str(q)[:24]}': {type(e).__name__}")
        return False

    def _type_address_by_anchor(self, text: str, prefer_city: str) -> bool:
        """Coordinate fallback for a visible-but-unreachable address box: find a light-DOM
        anchor near it (the field label), click just below the label (into the input), and
        type with the keyboard — then pick the suggestion. No element handle needed."""
        page = self.rt.page
        anchors = ("Find Your Address", "Start typing the address", "Address",
                   "Where is your property")
        for a in anchors:
            for ctx in self._frames():
                try:
                    el = ctx.get_by_text(a, exact=False).first
                    if el.count() == 0 or not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box:
                        continue
                    # click ~30px below the label's bottom — that's the input field
                    x = box["x"] + min(box["width"] / 2, 180)
                    y = box["y"] + box["height"] + 30
                    page.mouse.click(x, y)
                    self.rt.think(0.3, 0.6)
                    try:
                        page.keyboard.press("Control+a"); page.keyboard.press("Delete")
                    except Exception:
                        pass
                    page.keyboard.type(text, delay=55)
                    self._say(f"  · address: typed at the box under ‘{a}’ (coordinate fallback)")
                    self.rt.think(1.4, 2.2)
                    if self._pick_suggestion(prefer_city) or self._pick_suggestion_deep(prefer_city):
                        self._say(f"  ✓ address — picked the ‘{prefer_city}’ suggestion")
                        return True
                    try:
                        page.keyboard.press("ArrowDown"); self.rt.think(0.4, 0.7)
                        page.keyboard.press("Enter"); self.rt.think(1.0, 1.6)
                        if self._address_accepted():
                            self._say("  ✓ address — committed via keyboard (coordinate)")
                            return True
                    except Exception:
                        pass
                except Exception:
                    pass
        return False

    def _quick_search_box(self):
        """Find the Quick-search address input AFTER forcing the Quick search tab.
        Patient (the box renders lazily inside an iframe), STRICT (never a <select>),
        and re-scans ALL frames + shadow DOM each round because the address iframe can
        finish loading a few seconds in."""
        # let any iframes finish loading — the address box lives in one of them
        try:
            for f in self.rt.page.frames:
                if f is not self.rt.page.main_frame:
                    try:
                        f.wait_for_load_state("domcontentloaded", timeout=3000)
                    except Exception:
                        pass
        except Exception:
            pass
        for _ in range(18):                           # ~16s, re-fetching frames each round
            loc = self._find_address_input(quiet=True)
            if loc is not None:
                return loc
            deep = self._find_address_input_deep(quiet=True)
            if deep is not None:
                return deep
            self.rt.think(0.6, 0.9)
        loud = self._find_address_input()             # loud attempts for the log
        return loud if loud is not None else self._find_address_input_deep()

    def _fill_address(self, addr_data) -> bool:
        """QUICK SEARCH ONLY (operator's instruction). Force the 'Quick search' tab,
        type the full address into its Google-Places box, pick the city-matching
        suggestion — Booking then auto-fills country/city/zip AND sets the map pin.
        No Address-form fallback: if the box can't be found, hand off for one click."""
        page = self.rt.page
        fields = addr_data.get("fields", {}) if isinstance(addr_data, dict) else {}
        candidates = addr_data.get("candidates", []) if isinstance(addr_data, dict) else (
            addr_data if isinstance(addr_data, list) else [addr_data])
        prefer_city = str(fields.get("city", "")).strip()

        # 0) widget loads lazily — wait, then FORCE the Quick search tab
        self._wait_address_widget(timeout=10.0)
        if self._ensure_quick_search_tab():
            self._say("  · address: forced ‘Quick search’ tab")
        else:
            self._say("  · address: no tab toggle here — using the search box directly")
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        self.rt.think(1.0, 1.8)

        # PRIMARY for this variant: the Google <gmp-place-autocomplete> web component.
        if self._fill_gmp_autocomplete(candidates, prefer_city):
            return self._finish_pin()

        loc = self._quick_search_box()
        if loc is None:
            # The box is visible but unreachable by selectors/JS (not in any frame's DOM).
            # COORDINATE FALLBACK: click where it physically sits (under its label) and
            # type with the keyboard — works even when the element can't be located.
            self._diag_inputs()
            for cand in candidates:
                if self._type_address_by_anchor(str(cand), prefer_city):
                    return self._finish_pin()
            # last resort — one operator nudge, then retry the box
            self.state = "awaiting_captcha"; self._captcha.clear()
            self._say("⏸ Click into the address box in the browser window and type one letter so it "
                      "activates, then click Done — I’ll take over.")
            self._captcha.wait(); self._captcha.clear(); self.state = "filling"
            self._ensure_quick_search_tab()
            loc = self._quick_search_box()
            if loc is None:
                for cand in candidates:               # try coordinates again post-nudge
                    if self._type_address_by_anchor(str(cand), prefer_city):
                        return self._finish_pin()
        if loc is None:
            self._say("  ⏸ couldn’t reach the Quick search box — type the address there and pick a "
                      "suggestion, then click Fill.")
            return False

        # type into Quick search + pick the suggestion (sticking to this box only)
        for cand in candidates:
            try:
                self._clear_input(loc)                # wipe any saved (e.g. Saudi) value
                try:
                    loc.press_sequentially(str(cand), delay=55)
                except Exception:
                    loc.type(str(cand), delay=55)
                self._say(f"  · Quick search: typed '{str(cand)[:46]}'")
                if self._pick_suggestion(prefer_city) or self._pick_suggestion_deep(prefer_city):
                    self._say(f"  ✓ Quick search — picked the '{prefer_city or cand}' suggestion")
                    return self._finish_pin()
                # keyboard fallback: ↓ + Enter commits the top suggestion
                try:
                    loc.press("ArrowDown"); self.rt.think(0.4, 0.7)
                    loc.press("Enter"); self.rt.think(1.0, 1.6)
                    val = (loc.input_value() or "").strip()
                    if val and prefer_city and prefer_city.lower() in val.lower():
                        self._say(f"  ✓ Quick search — committed via keyboard: {val[:40]}")
                        return self._finish_pin()
                except Exception:
                    pass
                self._say(f"  – no '{prefer_city}' suggestion for '{str(cand)[:30]}' — broader query")
            except Exception as e:
                self._say(f"  – Quick search try '{str(cand)[:26]}': {type(e).__name__}")

        self._say("  ⏸ no suggestion matched in Quick search — pick one in the window, then Fill.")
        return False

    def _apply_booking_rules(self, rules) -> tuple:
        page = self.rt.page
        did = 0; navigated = False
        for kind, value, action, val in rules:
            if action == "address":
                # Fire ONLY on the real address page. The bare word "address" appears on
                # other pages too (e.g. the overview's "...name, address, facilities..."),
                # so require the actual widget: the Quick-search/Address-form tab triggers,
                # or the unmistakable heading text.
                try:
                    body = (self.rt.page.inner_text("body") or "").lower()
                except Exception:
                    body = ""
                has_widget = False
                try:
                    for ctx in self._frames():
                        if ctx.locator("#SEARCH-tab-trigger, #MANUAL-tab-trigger").first.count() > 0:
                            has_widget = True; break
                except Exception:
                    pass
                is_address_page = (has_widget
                                   or "where is your property" in body
                                   or "find your address" in body)
                if not is_address_page:
                    continue
                if self._fill_address(val):
                    did += 1
                continue
            loc = self._locate(kind, value)
            if loc is None:
                continue
            before = self._page_sig()
            if self._act(loc, str(value)[:32], val, action, cached=False):
                did += 1
            if action in ("click", "check"):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=2500)
                except Exception:
                    pass
                if self._page_sig() != before:
                    navigated = True; break
        return did, navigated

    # ---- auto-advancing fill (one click → whole wizard) ------------------
    def _do_fill(self, profile_data: dict):
        from accounts_pilot.models.property_profile import PropertyProfile
        if settings.agentql_api_key and not os.environ.get("AGENTQL_API_KEY"):
            os.environ["AGENTQL_API_KEY"] = settings.agentql_api_key
        try:
            p = PropertyProfile.model_validate(profile_data)
        except Exception as e:
            self._say(f"Invalid property JSON: {e}"); self.state = "connected"; return

        use_llm = bool(settings.azure_openai_endpoint and settings.azure_openai_key
                       and settings.azure_openai_deployment)
        if not use_llm:
            self._say("⚠ No LLM key configured — only the 4 stable rules will run. "
                      "Set AZURE_OPENAI_* in .env for full autonomous fill.")
        # AUTOPILOT: fill EVERY field, inventing dummy data for gaps so no page stalls.
        # Enable per-run via "autopilot": true (or "dummy": true) in the property JSON,
        # or on the session (self.autopilot).
        autopilot = bool(profile_data.get("autopilot") or profile_data.get("dummy")
                         or getattr(self, "autopilot", False))
        if autopilot:
            self._say("🛫 Autopilot ON — filling every field with real data where it maps, "
                      "dummy data for gaps; advancing through all pages.")
        self.state = "filling"
        self._stop.clear()                            # fresh run — drop any prior Kill
        self._captcha.clear(); self._otp.clear()
        try:
            self._force_english()                     # so the page text matches our mapping
            from accounts_pilot.web import booking_rules, llm_fill
            self._autopilot_active = autopilot        # gates map persistence (no dummy poisoning)
            # learned maps are looked up two-tier per page: cache → file → LLM (_lookup_map)
            last_key = None; same_page_seen = 0
            last_fp = None                            # page fingerprint — detects REAL change
            sig_streak_key = None; sig_streak = 0     # absolute passes on one page (even w/ progress)
            pending = None                            # (mkey, entries) — saved ONLY after advance
            # how many "Add unit"-style CTA clicks we allow (one per room type + buffer),
            # so we flow into the unit sub-wizard without looping forever
            progress_clicks = 0
            self._room_list = profile_data.get("room_types", []) or []
            progress_cap = len(self._room_list) * 8 + 6   # each room is a multi-page sub-flow
            self._unit_order = []                          # distinct unit_ids seen (ordered)
            self._unit_target = len(self._room_list)
            self._mmt_room_idx = 0                          # which room MMT is currently creating
            # photos come from the MIS as S3 URLs (no local files) — download them to a
            # temp cache so the upload queue is real; without this, photo-gated steps stall.
            self._photo_paths = self._resolve_photo_paths(getattr(p, "photos", []) or [])
            self._all_photo_paths = [pp for pp in self._photo_paths if os.path.exists(pp)]
            self._photo_preflight()                    # warn early about photos OTAs will reject
            self._ensure_filechooser()                 # auto-feed any upload dialog (no OS popup)
            self._clicked_ctas = set()                # don't spam a generic CTA that opened a tab
            self._bank_gate_passed = False            # pause at the bank/payout page only once
            self._verify_gate_passed = False          # pause once for MMT email/mobile OTP verify
            self._last_progress = False               # did the previous pass fill/expand anything?
            RETRY_PASSES = 5                          # auto-retry a slow/rendering page this many
                                                     # times before giving up (≈ pressing Fill again)
            for i in range(120):                      # whole wizard + 3 room sub-flows + photos + retries
                if self._stop.is_set():               # operator pressed Kill
                    self._say("⏹ Stopped by operator."); break
                if hasattr(self.rt, "active_page"):
                    self.rt.active_page()             # follow any new tab the last click opened
                # let the SPA fully render before reading/acting
                try:
                    self.rt.page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                self.rt.think(0.6, 1.1)
                url = self.rt.page.url
                sig = self._page_sig()
                self._say(f"— Page {i+1}: {url}")

                self._last_progress = False

                # REAL-progress guard: a pass counts as progress ONLY if the page state actually
                # changed (a new field filled, a dropdown picked, a section expanded). Re-filling
                # the same values does NOT count — that false 'progress' is what caused the page
                # to loop. We detect change with a fingerprint of all inputs/dropdowns/errors.
                fp = self._page_fingerprint()
                made_progress_prev = (sig == last_key and fp != last_fp)
                if sig == last_key and not made_progress_prev:
                    same_page_seen += 1
                else:
                    same_page_seen = 0; last_key = sig
                last_fp = fp
                # absolute cap: even a legitimately-changing page never needs more than this many
                # passes — a hard stop so nothing can loop indefinitely.
                if sig == sig_streak_key:
                    sig_streak += 1
                else:
                    sig_streak = 0; sig_streak_key = sig
                if sig_streak > 14:
                    self._diag_stuck()
                    self._say("This page kept churning but never advanced — stopping (hard cap)."); break
                # a control may simply not be rendered yet — wait longer each pass and re-try
                # (this is what pressing Fill again does, done automatically).
                if 0 < same_page_seen <= RETRY_PASSES:
                    self._say(f"  · page didn't advance — retry {same_page_seen}/{RETRY_PASSES} "
                              f"(letting it finish rendering)")
                    self.rt.think(1.0 + same_page_seen * 0.8, 1.7 + same_page_seen * 1.0)
                if same_page_seen > RETRY_PASSES:
                    self._diag_stuck()
                    self._say("Stuck on this page (it won't advance after several retries). "
                              "Send the screenshot/log and I'll map it."); break

                # 0) The service fills the whole flow from the JSON (it's the operator's
                #    autonomous onboarding tool). The only thing it won't auto-type is a
                #    literal bank/card ACCOUNT NUMBER field — there it pauses for the
                #    operator (a 10-sec manual entry). Everything else, incl. GST/invoicing
                #    and completion, is filled.
                try:
                    body = (self.rt.page.inner_text("body") or "").lower()
                except Exception:
                    body = ""
                bank_kw = ("bank account number", "iban", "swift code", "bic code", "sort code",
                           "routing number", "how would you like to get paid", "add your bank",
                           "add a bank account", "your bank account details",
                           "card number", "credit card number", "debit card number")
                if any(k in body for k in bank_kw) and not self._bank_gate_passed:
                    self.state = "awaiting_captcha"; self._captcha.clear()
                    self._say("⏸ Reached the BANK/PAYOUT account fields — enter the account number "
                              "in the window (or skip), then click Done and I'll carry on.")
                    self._captcha.wait(); self._captcha.clear(); self.state = "filling"
                    self._bank_gate_passed = True     # don't re-pause on the same page next loop
                    continue
                # 0b) CAPTCHA mid-flow — hand off, then resume
                if self.rt.detect_challenge() == "captcha":
                    self.state = "awaiting_captcha"
                    self._say("A CAPTCHA appeared — solve it in the browser window, then click Done.")
                    self._captcha.wait(); self._captcha.clear()
                    self.state = "filling"
                    continue

                # 0b1) MMT EMAIL/MOBILE OTP VERIFICATION — a human-only gate. Autopilot fills the
                #      email/phone but cannot complete the OTP; pause ONCE for the operator.
                if (self.ota == "makemytrip" and not self._verify_gate_passed
                        and ("needs to be verified" in body or "verify your email" in body
                             or "verify your mobile" in body)):
                    self.state = "awaiting_captcha"; self._captcha.clear()
                    self._say("⏸ MMT needs EMAIL + MOBILE verified (OTP). Click each ‘Verify’, "
                              "enter the codes, then click Done and I’ll continue.")
                    self._captcha.wait(); self._captcha.clear(); self.state = "filling"
                    self._verify_gate_passed = True
                    continue

                navigated = False

                is_booking = (self.ota == "booking_com")

                # 0a1) LOGIN / AUTH page — NEVER fill it (that's the operator's password +
                #      OTP). Pause until they log in and open the property form.
                low = url.lower()
                if not is_booking and (
                        "login" in low or "signin" in low or "sign-in" in low or
                        "enter your password" in body or "username or email" in body or
                        ("password" in body and ("sign in" in body or "log in" in body))):
                    self.state = "awaiting_captcha"; self._captcha.clear()
                    self._say("⏸ This is the LOGIN page — I won't touch it. Log in (password + OTP) "
                              "in the browser window, open the ‘List/Add a property’ form, then click Done.")
                    self._captcha.wait(); self._captcha.clear(); self.state = "filling"
                    continue

                # 0a2) TRAINING CAPTURE — for non-Booking OTAs (e.g. MakeMyTrip) we haven't
                #      mapped yet, record every page's structure to data/training/<ota>/ so
                #      it can be mapped once from a single run.
                if not is_booking:
                    try:
                        self._dump_training(i + 1, url)
                    except Exception as e:
                        self._say(f"  · (training capture skipped: {type(e).__name__})")

                # 0a3) MMT 'Which property type would you like to list?' — custom card UI
                #      the LLM/scraper can't see. Pick category + sub-type from JSON, List.
                mmt_basic = False
                if self.ota == "makemytrip" and self._fill_mmt_property_type(profile_data):
                    navigated = True
                # 0a3b) MMT 'Create Room' overview — add the next room until all 3 exist.
                if self.ota == "makemytrip" and not navigated and self._fill_mmt_rooms_overview(profile_data):
                    navigated = True
                # 0a4) MMT 'Basic Info' step — custom dropdowns + radio the scraper misses.
                if self.ota == "makemytrip" and not navigated:
                    mmt_basic = self._fill_mmt_basic_info(profile_data, p)
                # 0a5) MMT 'Location' step — Google-Places autocomplete (MUI), pick 1st match.
                if self.ota == "makemytrip" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_mmt_location(profile_data)
                # 0a5b) MMT 'Property Amenities' — click through every left category tab.
                if self.ota == "makemytrip" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_mmt_amenities(profile_data)
                # 0a5c) MMT room occupancy — keep counters valid (max children < max occupancy).
                #       Does NOT block the LLM (it still fills bed type / room amenities).
                if self.ota == "makemytrip" and not navigated and not mmt_basic:
                    self._fill_mmt_occupancy(profile_data)
                # 0a5d) AGODA property-type CARD pages (category + sub-type) — deterministic,
                #       first pass, no LLM (these took 2-3 tries via the LLM/empty-cache path).
                if self.ota == "agoda" and not navigated and not mmt_basic:
                    if self._fill_agoda_property_type(profile_data):
                        mmt_basic = True              # handled → skip LLM; Continue advances
                # 0a6) AGODA 'Location' step — search + structured address + state/city dropdowns.
                if self.ota == "agoda" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_agoda_location(profile_data)
                # 0a7) AGODA 'Rooms & rates' step — force 'Set rate manually' + room-type
                #      dropdowns. Does NOT block the LLM (it fills size/rate/occupancy/breakfast).
                if self.ota == "agoda" and not navigated and not mmt_basic:
                    self._fill_agoda_rooms(profile_data)
                # 0a8) AGODA check-in/out time dropdowns (24h↔12h format).
                if self.ota == "agoda" and not navigated and not mmt_basic:
                    self._fill_agoda_times(profile_data)
                # 0a9) AGODA legal / account / beneficial-owner dropdowns (country/nationality/
                #      state/city/DOB). Does NOT block the LLM (it fills the text fields).
                if self.ota == "agoda" and not navigated and not mmt_basic:
                    self._fill_agoda_legal(profile_data)
                # 0a9b) AGODA 'Pricing' step — picks a payout METHOD (the page's real blocker);
                #       bank account number stays a human gate. Blocks LLM the turn it selects.
                if self.ota == "agoda" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_agoda_pricing(profile_data)

                # 0a9e) EXPEDIA landing wizard 'What would you like to list?' — click the
                #       Lodging / Private-residence card (role=presentation divs the LLM can't
                #       see; otherwise autopilot loops on the scroll-only CTA). Navigates.
                if self.ota == "expedia" and self._fill_expedia_classification(profile_data):
                    navigated = True
                # 0a9e2) EXPEDIA optional 'import from Booking.com URL' — SKIP it (clear any URL,
                #        click Next, never 'Add'). Navigates.
                if self.ota == "expedia" and not navigated and self._skip_expedia_booking_import(profile_data):
                    navigated = True
                # 0a9f0) EXPEDIA Step-1 location TYPEAHEAD — type address into #locationTypeAhead,
                #        pick a suggestion; Next then advances (to manual fallback for dummy data).
                if self.ota == "expedia" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_expedia_typeahead(profile_data)
                # 0a9f) EXPEDIA 'manual address' step — exact-id handler, Country FIRST (drives
                #       ZIP validation + repopulates State). Runs before the generic location
                #       handler so the country-first ordering wins on this page.
                if self.ota == "expedia" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_expedia_manual_location(profile_data)
                # 0a10) EXPEDIA 'Location' step — address autocomplete + structured fields +
                #       Country/State/City dropdowns (the universal LLM blind spot). Blocks LLM.
                if self.ota == "expedia" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_expedia_location(profile_data)
                # 0a11) EXPEDIA 'Rooms' — room-type / bed-type dropdowns (LLM does size/rate).
                if self.ota == "expedia" and not navigated and not mmt_basic:
                    self._fill_expedia_rooms(profile_data)
                # 0a11b) EXPEDIA 'Policies and settings' — payment method + time zone + billing
                #        currency (required), and CLEAR the LLM's invalid tax zeros. Blocks LLM.
                if self.ota == "expedia" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_expedia_policies(profile_data)
                # 0a12) EXPEDIA check-in/out time pickers (24h↔12h format).
                if self.ota == "expedia" and not navigated and not mmt_basic:
                    self._fill_expedia_times(profile_data)

                # 0a13) AIRBNB 'become a host' wizard — single-listing flow. Each handler is
                #       body-text gated and degrades to the LLM walker on any unmapped page.
                #       Card/stepper/tile pages BLOCK the LLM (mmt_basic) and let Next advance.
                if self.ota == "airbnb" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_airbnb_structure(profile_data)
                if self.ota == "airbnb" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_airbnb_privacy_type(profile_data)
                if self.ota == "airbnb" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_airbnb_location(profile_data)
                if self.ota == "airbnb" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_airbnb_floor_plan(profile_data)
                if self.ota == "airbnb" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_airbnb_amenities(profile_data)
                if self.ota == "airbnb" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_airbnb_title(profile_data)
                if self.ota == "airbnb" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_airbnb_description(profile_data)
                if self.ota == "airbnb" and not navigated and not mmt_basic:
                    mmt_basic = self._fill_airbnb_price(profile_data)

                # 0d) KYP / 'Partner verification' — Booking-specific deterministic fill.
                kyp_page = is_booking and ("know-your-partner" in url.lower()
                                           or "partner verification" in body)
                if kyp_page:
                    self._fill_kyp(profile_data)

                # 0c) PHOTOS — upload the property's images.
                if self._photo_paths:
                    if self.ota == "agoda":
                        # Agoda = one dropzone per section (property + each room); feed each
                        # a small batch so the ≥3 property gate clears and rooms get photos.
                        on_photos = "/photos" in (self.rt.page.url or "")
                        if on_photos and self._fill_agoda_photos():
                            self._photo_paths = []    # fed every dropzone once
                    elif self._photos_file_input() is not None:
                        self._upload_photos_chunked()  # batches of 20 for MMT, all-at-once else
                        self._photo_paths = []        # upload once

                # 1) STABLE RULES (Booking-only) — property-type card, single/multi,
                #    channel-manager, the Google address widget. Other OTAs rely on the
                #    generic LLM walker (+ their own handlers, added as we learn them).
                did_r = 0
                if is_booking:
                    did_r, navigated = self._apply_booking_rules(booking_rules.rules_for(p))
                    if did_r:
                        self._say("  ✓ address filled via dedicated handler (Quick search + map pin)")

                # 2) LEARNED MAP first (no LLM), else LLM once then STORE the map.
                #    EXCEPTION: unit/room sub-flow pages vary per room (Standard vs Deluxe
                #    vs Family Suite), so we never cache them — and we feed the LLM ONLY the
                #    current room so each unit gets its own name/beds/occupancy/price.
                mkey = self._map_key(p)
                in_unit = self._is_unit_flow()
                llm_profile = profile_data
                if in_unit:
                    if self.ota == "makemytrip":
                        idx = getattr(self, "_mmt_room_idx", 0)   # tracked from the rooms overview
                    else:
                        uid = self._current_unit_id()
                        if uid and uid not in self._unit_order:
                            self._unit_order.append(uid)  # a new room just started
                        idx = (self._unit_order.index(uid) if uid in self._unit_order
                               else max(0, len(self._unit_order) - 1))
                    if 0 <= idx < len(self._room_list):
                        cur = self._room_list[idx]
                        llm_profile = dict(profile_data)
                        llm_profile["room_types"] = [cur]   # the LLM sees only THIS room
                        self._say(f"  · unit {idx + 1}/{self._unit_target}: "
                                  f"{cur.get('name', 'room')} (rate {cur.get('base_rate', '')})")
                # run the LLM when the page is NEW, or when the last pass made progress (so a
                # multi-section accordion gets filled section-by-section across passes).
                # run the LLM on a new page, when the last pass progressed, OR on a retry pass
                # (same_page_seen>0) — a retry re-scrapes, catching controls that hadn't rendered.
                # After the payout/bank gate there's nothing left to auto-fill, so don't re-map.
                if not navigated and not kyp_page and not mmt_basic and not self._bank_gate_passed \
                        and (same_page_seen == 0 or made_progress_prev
                             or same_page_seen <= RETRY_PASSES):
                    stored = self._lookup_map(mkey) if (not in_unit and same_page_seen == 0) else None
                    if stored:                        # only replay a NON-EMPTY learned map
                        # replay what we learned before — plain Playwright, no LLM call.
                        did_c, navigated = self._apply_stored(stored)
                        self._say(f"  ✓ replayed learned map — {did_c} field(s), no LLM "
                                  f"({len(stored)} stored)")
                        self._last_progress = did_c > 0 or navigated
                    # An EMPTY learned map ([]) means this page can't be cached — card-select
                    # pages like the property-type chooser store 0 fields. Don't waste the pass
                    # replaying nothing; run the LLM NOW (same pass) so it advances first try.
                    elif use_llm:
                        try:
                            fields = llm_fill.scrape_fields(self.rt.page)
                            actions = llm_fill.map_actions(fields, llm_profile, autopilot=autopilot)
                            if actions:
                                self._say(f"  · LLM read the page → mapping {len(actions)} field(s) from your JSON")
                                did_l, navigated = self._apply_actions(actions, cached=False)
                                self._say(f"  ✓ {did_l} field(s) via LLM")
                                self._last_progress = did_l > 0 or navigated
                            else:
                                self._say("  · LLM: nothing to fill here (info page or all stable-rule controls)")
                            if not in_unit:
                                # LEARN non-unit pages: stable (label/automation-id) locators
                                entries = []
                                for a in (actions or []):
                                    # the live data-ap-id selector isn't stable across reloads;
                                    # cache from the element's stable id/name/test-id/DOM path
                                    d = self._stable_descriptor(a.get("stable") or a.get("selector", ""))
                                    if d:
                                        entries.append({"by": d[0], "locator": d[1],
                                                        "action": a.get("action"),
                                                        "value": a.get("value", "")})
                                # persist only AFTER the page advances (no cache poisoning)
                                pending = (mkey, entries)
                        except Exception as e:
                            self._say(f"  · LLM error: {type(e).__name__}: {e}")

                if navigated:
                    self._commit_pending(pending)
                    pending = None
                    self._say("  → a selection advanced the page"); self.rt.think(); continue

                # 3) ADVANCE — a plain Continue, OR a 'next action' CTA (the post-wizard
                #    overview's 'Add unit'/'Add photos' etc.) so the walk flows into the
                #    Units sub-wizard and keeps learning.
                before = self._page_sig()
                moved = self.rt.try_advance()
                cta = False
                if not moved and progress_clicks < progress_cap:
                    if self._try_progress_button():       # 'Add unit' / 'Add photos' etc.
                        progress_clicks += 1; moved = True; cta = True
                if not moved:
                    if self._bank_gate_passed:
                        # the payout/bank gate is the end of the flow — nothing left to fill
                        self._say("Reached the payout/bank gate — nothing left to auto-fill; "
                                  "stopping here (the operator completes payout)."); break
                    # No Continue/next button yet — it may still be disabled or rendering.
                    # Don't give up: loop and retry (the same-page retry guard above keeps
                    # waiting + re-scraping, and finally stops after RETRY_PASSES).
                    self._say("  · no ‘Continue’ enabled yet — retrying after the page settles")
                    self.rt.think(0.8, 1.4)
                    continue
                if self._wait_sig_change(before, timeout=10.0):
                    self._commit_pending(pending)         # page advanced → the map WORKS, save it
                    pending = None
                    continue                          # advanced — next page
                if cta:
                    # a CTA may open a sub-flow/modal without changing the heading+url sig;
                    # loop again to handle whatever appeared (the same_page_seen guard stops
                    # us if nothing really changed).
                    self._say("  · CTA clicked — re-reading the page")
                    continue

                # Continue was clicked but the page didn't advance — a required field is still
                # empty (or a verify-gate). Loop back and retry: re-scrape and (in autopilot)
                # fill the gaps. The same-page retry guard at the top of the loop gives up and
                # shows the diagnostic after RETRY_PASSES tries.
                self._say("  · clicked but the page didn’t advance — retrying (filling any gaps)")
                self.rt.think(0.8, 1.4)
                continue

            self._say("Fill run finished. ✓")
        except Exception as e:
            self._say(f"Fill error: {type(e).__name__}: {e}")
        finally:
            self.state = "connected"


# ---- per-OTA session registry: each OTA runs its own browser + thread, independently ----
_SESSIONS: "dict[str, LiveSession]" = {}


def get_session(ota: str = "booking_com") -> "LiveSession":
    s = _SESSIONS.get(ota)
    if s is None:
        s = LiveSession(ota)
        _SESSIONS[ota] = s
    return s


# back-compat alias — the original single Booking.com session
LIVE = get_session("booking_com")
