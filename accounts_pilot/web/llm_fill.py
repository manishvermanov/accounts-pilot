"""LLM page-filler — drives ANY wizard page on ANY OTA, with NO per-page code.

For each page: scrape EVERY interactive element — not just standard inputs, but custom
cards, custom dropdowns, radios, checkboxes, switches and clickable tiles — each tagged
with a reliable `data-ap-id` locator plus its visible text, label and the heading it
sits under (its 'group'). Send that + the property JSON to the LLM, which returns a list
of {id, action, value} decisions. Apply them with native Playwright.

This is the 'brain': the model decides what every control means and how to map the
property data onto it. The richer the scrape, the less hand-coding any OTA needs — the
goal is zero per-page handlers (only genuinely opaque widgets, e.g. a Google-Maps box
with a closed shadow root, stay in dedicated code).

Cacheable: each element also carries a `stable` selector (id/name/test-id/DOM-path) used
only to remember a page's mapping for key-free replay next time.
"""
from __future__ import annotations

import json
from typing import Optional

from accounts_pilot.config import settings

# JS that tags + lists EVERY interactive element with a reliable locator, its visible
# text, label, and the heading it lives under. Custom cards/dropdowns/radios included.
_SCRAPE_JS = r"""
() => {
  document.querySelectorAll('[data-ap-id]').forEach(e=>e.removeAttribute('data-ap-id'));
  const vis=el=>{try{const r=el.getBoundingClientRect();const s=getComputedStyle(el);
    return r.width>1&&r.height>1&&s.visibility!=='hidden'&&s.display!=='none'&&s.opacity!=='0';}catch(e){return false;}};
  const txt=el=>((el.innerText||el.textContent||'').replace(/\s+/g,' ').trim());
  const ptr=el=>{try{return getComputedStyle(el).cursor==='pointer';}catch(e){return false;}};
  function stable(el){
    if(el.id) return '#'+CSS.escape(el.id);
    const nm=el.getAttribute('name'); if(nm) return el.tagName.toLowerCase()+'[name="'+nm+'"]';
    const td=el.getAttribute('data-testid'); if(td) return '[data-testid="'+td+'"]';
    const ti=el.getAttribute('data-test-id'); if(ti) return '[data-test-id="'+ti+'"]';
    function seg(e){let i=1,s=e.previousElementSibling;while(s){if(s.tagName===e.tagName)i++;s=s.previousElementSibling;}return e.tagName.toLowerCase()+':nth-of-type('+i+')';}
    let parts=[],e=el;while(e&&e.nodeType===1&&e.tagName!=='HTML'){parts.unshift(seg(e));e=e.parentElement;}
    return parts.join(' > ');
  }
  function label(el){
    if(el.getAttribute&&el.getAttribute('aria-label'))return el.getAttribute('aria-label');
    if(el.placeholder)return el.placeholder;
    if(el.id){const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');if(l&&txt(l))return txt(l);}
    const lp=el.closest&&el.closest('label');if(lp&&txt(lp))return txt(lp).slice(0,120);
    let p=el.parentElement,hops=0;
    while(p&&hops<4){const h=p.querySelector&&p.querySelector('label,legend,h1,h2,h3,h4');if(h&&txt(h))return txt(h).slice(0,120);p=p.parentElement;hops++;}
    return '';
  }
  function group(el){      // the row/section label this control belongs to (so Yes/No radios
                           // get their amenity/question name, not just 'Yes'/'No')
    const TITLE='h1,h2,h3,h4,h5,h6,legend,[data-test-id*="text"],[data-test-id*="title"],'+
      '[data-test-id*="name"],[data-test-id*="question"],[data-test-id*="label"]';
    let p=el.parentElement,hops=0;
    while(p&&hops<6){
      let h=p.querySelector&&p.querySelector(TITLE);   // a titled label within this ancestor
      if(h&&h!==el&&!h.contains(el)&&txt(h))return txt(h).slice(0,90);
      p=p.parentElement;hops++;
    }
    return '';
  }
  const cand=new Set();
  document.querySelectorAll('input,textarea,select,button,[role=button],[role=radio],'+
    '[role=checkbox],[role=switch],[role=combobox],[role=option],[role=menuitemradio],'+
    '[role=tab],[role=spinbutton]').forEach(e=>cand.add(e));
  // clickable cards/tiles: keep the OUTERMOST cursor:pointer element of each cluster
  document.querySelectorAll('div,li,span,label,a').forEach(e=>{
    if(ptr(e)&&!(e.parentElement&&ptr(e.parentElement)))cand.add(e);});
  const out=[];let id=0;
  cand.forEach(el=>{
    if(!vis(el))return; if(el.type==='hidden')return;
    if(el.closest&&el.closest('header,nav,footer'))return;     // skip chrome (nav/header/footer)
    const tag=el.tagName.toLowerCase();const role=el.getAttribute('role')||'';
    const t=txt(el).slice(0,90);const lab=label(el).slice(0,100);
    if(!t&&!lab&&tag!=='input'&&tag!=='textarea'&&tag!=='select')return;  // nothing to identify it by
    el.setAttribute('data-ap-id',String(id));
    const item={id,selector:'[data-ap-id="'+id+'"]',stable:stable(el),tag,
      type:el.type||role||tag,label:lab,text:t,group:group(el)};
    if(tag==='input'||tag==='textarea'){item.value=el.value||'';
      if(el.type==='checkbox'||el.type==='radio')item.checked=!!el.checked;}
    else if(tag==='select'){item.options=[...el.options].map(o=>(o.value+' | '+o.text)).slice(0,60);item.value=el.value;}
    else if(role==='checkbox'||role==='radio'||role==='switch'){const a=el.getAttribute('aria-checked');if(a!=null)item.checked=a==='true';}
    if(role==='combobox'||/^(select|choose)\b/i.test(t))item.dropdown=true;
    out.push(item);id++;
  });
  return out.slice(0,180);
}
"""

_SYSTEM = (
    "You drive ONE page of an online hotel-listing wizard (any OTA — Booking.com, "
    "MakeMyTrip/Goibibo, etc.). You are given EVERY interactive element on the current page "
    "and the property's full data as JSON. Each element has: id (use this to act on it), "
    "tag, type, label, text (its visible text), group (the heading/section it sits under), "
    "and for inputs a value/checked, for <select> the options. Custom widgets are included: "
    "clickable CARDS/tiles, custom DROPDOWNS (type 'combobox' or dropdown:true, shown with a "
    "placeholder like 'Select rating'), custom RADIOS and CHECKBOXES (role radio/checkbox/"
    "switch). Treat them the same as native controls.\n"
    "Your job: map AS MANY controls as you can to the property data so the page is COMPLETE "
    "and its Continue/Save button can enable. A half-filled page can't advance — be thorough.\n"
    "Return STRICT JSON: "
    '{"actions":[{"id":<number>,"action":"fill|click|select|check|uncheck","value":"<text/option/empty>"}]}.\n'
    "ACTION VOCABULARY:\n"
    "• fill — type text into an input/textarea (value = the text).\n"
    "• click — click a button, a CARD/tile, or a custom radio/option (e.g. the 'Hotel' card, "
    "a 'No' radio). Use the element's id.\n"
    "• select — set a dropdown (native <select> OR a custom one). value = the option's text/"
    "value. For native selects the options are 'value | label' — send just the value (e.g. "
    "'13:00', 'IN'). For a CUSTOM dropdown send the visible option text (e.g. '3', '2015', "
    "'Resort'); the executor opens it and clicks that option.\n"
    "• check / uncheck — a checkbox or switch. NEVER uncheck something already required.\n"
    "HOW TO MAP (by meaning, using group+label+text):\n"
    "• property CATEGORY/TYPE → click the card/option whose title matches property_type "
    "(property_type 'hotel' → click the 'Hotel' card; under a 'Type of Hotel' group pick the "
    "matching sub-type card too).\n"
    "• single vs multiple properties / management-company / 'part of a group or chain?' → "
    "choose SINGLE / 'No' (unless listing_scope is exactly 'property_group'). Clicking 'Yes' "
    "on a group/chain question opens a REQUIRED company-name field that BLOCKS the form — so "
    "never click 'Yes' there and never fill a company/group/chain name.\n"
    "• channel-manager question → choose 'No' / decline (we don't use one during onboarding).\n"
    "• name of property → display_name (NOT the contact person's name). description → "
    "description. email → contact.email. phone/mobile → contact.phone (if there is a separate "
    "country-code dropdown like '+91', send only the local digits, not the country code).\n"
    "• star rating → select/click the value matching star_rating (e.g. '3'). "
    "year built / 'accepting bookings since' → pick a plausible year (use the JSON if present, "
    "else a sensible recent year).\n"
    "• amenities / facilities — map from facilities/amenities/room_amenities (wifi, ac, "
    "restaurant, bar, room service, parking, pool, spa, gym, elevator, housekeeping, power "
    "backup, reception, cctv, etc.). TWO possible UIs, handle whichever you see:\n"
    "   – Yes/No radios (each amenity row has a 'Yes' and a 'No', common on MakeMyTrip): you "
    "MUST answer EVERY amenity shown — click 'Yes' for ones the property HAS, click 'No' for "
    "the rest. The amenity name is in the control's 'group'. Leaving any Yes/No blank keeps "
    "the page from advancing, so answer all of them (Yes for present, No for absent).\n"
    "   – Checkboxes (Booking style): check the ones present, leave the rest, NEVER uncheck.\n"
    "   Skip any optional sub-dropdown next to an amenity (e.g. 'free/paid', a charge type) "
    "unless you can map it from the data.\n"
    "• yes/no questions → answer FROM the data (breakfast → facilities.breakfast.available; "
    "parking → facilities.parking; pets/smoking → policy).\n"
    "• check-in/check-out TIME dropdowns (24-hour): Check-in From = policy.checkin_from; "
    "Check-in Until = policy.checkin_until (late, default '23:00') and MUST be later than "
    "Check-in From; Check-out From = policy.checkout_from (default '00:00'); Check-out Until = "
    "policy.checkout_until.\n"
    "• min check-in age → policy.min_checkin_age; cancellation/prepayment → policy.\n"
    "• invoicing/tax/GST → GST registered? = compliance.gst_registered (false→'No', true→'Yes'); "
    "GSTIN → compliance.gstin; PAN → compliance.pan; '4th character of PAN is P or H?' → take "
    "the 4th char of compliance.pan and answer Yes only if it is P or H; legal/company name → "
    "compliance.legal_entity_name; business type → compliance.business_type.\n"
    "• KYC / 'Know Your Partner' / owner type → choose 'I'm an individual running a business' "
    "(the INDIVIDUAL option), then fill first/last name → compliance.owner_first_name/"
    "owner_last_name (else split contact.full_name); DOB → compliance.owner_dob; address.* → "
    "address fields.\n"
    "• UNIT / ROOM pages → fill from room_types[0]: room type → select the option best "
    "matching the room name/type (e.g. 'Standard Room'); room view → select a fitting option "
    "(e.g. a valley/mountain/city view; if unsure pick any so the required field is set); "
    "number of rooms → count; size → size_sqm; nightly price/rate → base_rate; max guests → "
    "max_adults (+ max_children); room name → name; description → description; room amenities → "
    "room_amenities; bathroom (private/shared) → bathroom; beds → match each beds[] type+count.\n"
    "   – SLEEPING/OCCUPANCY: FIRST select the bed type (a dropdown, e.g. matching beds[0] "
    "type like queen/king/single), which enables the rest. Set 'Number of beds' → beds[].count "
    "(a +/- stepper, action 'fill' with the number). DO NOT touch the OCCUPANCY counters — "
    "'Base adults', 'Maximum adults', 'Base children', 'Maximum children', 'Maximum occupancy'. "
    "Those are PRE-FILLED automatically from the bed arrangement and have strict cross-field "
    "rules (max children must be < max occupancy); separate code keeps them valid. Changing "
    "them breaks the page. If a counter looks disabled this pass, set the bed type first.\n"
    "   – ROOM SIZE has a unit (Square Feet / Square Meter) AND an area box. ALWAYS pick the "
    "unit first (Square Meter for size_sqm) — the area box stays DISABLED until a unit is "
    "chosen — then enter the area. If the area box looks disabled/uneditable this pass, just "
    "set the unit; you'll be called again once it enables.\n"
    "• MULTI-SECTION / ACCORDION pages (a single page with collapsed steps shown as headers, "
    "e.g. a 'Create Room' page listing 'Room Details', 'Sleeping Arrangement & Occupancy', "
    "'Bathroom Details', 'Meal Plan…', 'Amenity Details'): fill the OPEN section, then CLICK "
    "the next collapsed section header to expand it. You'll be called again to fill the newly "
    "revealed fields — work through the sections one pass at a time until all are complete.\n"
    "• required consent/confirmation checkbox to complete the operator's OWN registration → "
    "you MAY check it.\n"
    "• pure info/intro pages with nothing to fill → return {\"actions\":[]} (caller clicks Continue).\n"
    "HARD RULES — never violate:\n"
    "• The ADDRESS / map / 'where is your property' search is handled by SEPARATE code — do NOT "
    "touch any address search box, street/house, city, state, postal/zip, country, the map, or "
    "address-form/quick-search tabs. Skip ALL of those.\n"
    "• NEVER click any navigation/flow button — Continue, Next, BACK, Previous, Save, "
    "'Save & exit', 'Save and continue', Submit, Skip, Cancel, Done, 'List property'. The "
    "caller handles moving between pages. Clicking Back or Save & exit ruins the flow.\n"
    "• NEVER touch a language selector, account/profile, cookie/consent banner, menu, help, "
    "back, or sign-in. NEVER change the page/site language.\n"
    "• NEVER type a bank-account / IBAN / card NUMBER (handled by separate code). Other "
    "final-step fields (tax IDs, business details, consent) are fine.\n"
    "• NEVER click photo/file upload controls ('Add photos', 'Upload', 'Browse', 'Choose "
    "file', drop zones) — uploads are done by separate code; clicking opens a blocking OS dialog.\n"
    "Completeness matters more than caution: include every action you are reasonably confident "
    "about, referencing each element by its id."
)

# scraped controls whose label/text/stable-selector matches these are dropped before the LLM
_EXCLUDE = ("#lang", "language selector", "language", "account", "cookie", "consent",
            "sign in", "log in", "sign out", "help", "back to previous", "menu",
            "skip to", "currency selector",
            # navigation / flow buttons the caller handles — keep them away from the LLM
            # (clicking these moves between pages or exits the wizard):
            "save and continue", "save & continue", "next step", "save & exit",
            "save and exit", "save-and-exit", "property-back", "property-next",
            "list-your-property-back", "list-your-property-next",
            # photo/file upload — handled by code (set_input_files / file-chooser), never
            # by the LLM, because clicking these opens the native OS dialog and blocks:
            "upload", "add photo", "add photos", "choose file", "browse", "select file",
            "select photo", "drag", "photofileinput", "type=\"file\"", "type='file'")

# exact button labels that are navigation — never let the LLM click these (substring match
# would wrongly catch e.g. 'Power Backup' for 'back', so these are matched WHOLE-string)
_NAV_EXACT = {"back", "next", "previous", "continue", "save", "save & exit", "save and exit",
              "save & continue", "save and continue", "skip", "cancel", "exit", "submit",
              "done", "proceed", "finish"}


def _is_excluded(field: dict) -> bool:
    label = str(field.get("label", "")).strip().lower()
    text = str(field.get("text", "")).strip().lower()
    if label in _NAV_EXACT or text in _NAV_EXACT:     # whole-label nav buttons
        return True
    blob = (label + " " + text + " " + str(field.get("stable", "")).lower())
    return any(x in blob for x in _EXCLUDE)


def _azure_client():
    """Handle both Azure styles: the new `/openai/v1` base URL (OpenAI client) and
    the classic resource endpoint (AzureOpenAI client + api_version)."""
    endpoint = settings.azure_openai_endpoint.rstrip("/")
    if "/openai/v1" in endpoint:
        from openai import OpenAI
        return OpenAI(base_url=endpoint + "/", api_key=settings.azure_openai_key)
    from openai import AzureOpenAI
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=settings.azure_openai_key,
        api_version=settings.azure_openai_api_version,
    )


def scrape_fields(page) -> list[dict]:
    try:
        return page.evaluate(_SCRAPE_JS)
    except Exception:
        return []


# Appended to the system prompt in autopilot mode: fill EVERYTHING so the wizard never
# blocks on a required field the JSON doesn't cover — invent plausible dummy data for gaps.
_AUTOPILOT = (
    "\n\nAUTOPILOT MODE (sandbox completion run): drive this wizard to the end with NO blank "
    "required fields. Fill EVERY visible control on the page, not only those that map to the "
    "JSON. Prefer real JSON data; for anything the JSON doesn't cover, INVENT a reasonable "
    "dummy value consistent with the field's label/group so the page can advance:\n"
    "• required dropdowns (room view, meal plan, property sub-type, etc.) → pick the FIRST "
    "sensible offered option.\n"
    "• year fields (built / accepting bookings since) → a recent year like 2018.\n"
    "• prices / rates / rack-rate / extra-guest charges → use room base_rate, else a sane "
    "number (e.g. 2000).\n"
    "• meal plan → 'Room Only' / 'EP' / breakfast-only if offered, else the first option.\n"
    "• counts / occupancy / inventory / number-of-rooms → a small number (1–3).\n"
    "• free-text / descriptions / names → a short plausible sentence or the property/room name.\n"
    "• required radios (yes/no) → answer per the data, else the safer/common default ('No' for "
    "extra charges, 'Yes' for standard inclusions).\n"
    "• required consent / confirmation / terms checkboxes → check them.\n"
    "NEVER leave a required field empty — a half-filled page cannot advance. Still obey ALL "
    "the HARD RULES above (never touch address/map, never click Continue/Save/Next, never type "
    "a bank/card number, never click uploads, never change language)."
)


def map_actions(fields: list[dict], profile: dict, autopilot: bool = False) -> list[dict]:
    """Ask the LLM which controls to act on. Returns actions enriched with the live
    `selector` (data-ap-id, used to act) + a `stable` selector and `label` (used for
    caching/logging), looked up from the scraped fields by id. With autopilot=True the
    model fills EVERY field, inventing dummy data for gaps so the wizard never stalls."""
    if not (settings.azure_openai_endpoint and settings.azure_openai_key and settings.azure_openai_deployment):
        raise RuntimeError("Azure OpenAI not configured (endpoint/key/deployment).")
    client = _azure_client()
    safe_fields = [f for f in fields                          # never expose lang/account/nav/upload…
                   if not _is_excluded(f)
                   and str(f.get("type", "")).lower() != "file"]
    # what the model sees (drop the bulky live selector — it acts by id)
    view = [{k: v for k, v in f.items() if k != "selector"} for f in safe_fields]
    user = ("PROPERTY DATA:\n" + json.dumps(profile, ensure_ascii=False)
            + "\n\nPAGE CONTROLS:\n" + json.dumps(view, ensure_ascii=False))
    system = _SYSTEM + (_AUTOPILOT if autopilot else "")
    # GPT-5 / reasoning deployments reject temperature!=1 and max_tokens; keep it minimal.
    resp = client.chat.completions.create(
        model=settings.azure_openai_deployment,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    by_id = {}
    for f in fields:
        try:
            by_id[int(f["id"])] = f
        except (KeyError, ValueError, TypeError):
            pass
    out = []
    for a in data.get("actions", []) or []:
        if "id" in a and a.get("id") is not None:
            try:
                f = by_id.get(int(a["id"]))
            except (ValueError, TypeError):
                f = None
            if f is not None:
                a.setdefault("selector", f.get("selector"))
                a["stable"] = f.get("stable")
                a["label"] = f.get("label") or f.get("text")
        if a.get("selector") and a.get("action"):
            out.append(a)
    return out
