"""Deterministic Booking.com wizard rules — every value mapped from YOUR JSON.

Built from the real captured pages. Booking exposes stable selectors for key
controls (`#automation_id_*`, `[data-testid=*]`); its inputs use volatile React
ids, so those are located by LABEL/TEXT. Each rule: (kind, locator, action, value).
Nothing is assumed — the property type, scope, name, stars, address, amenities,
breakfast, parking and languages all come from the property JSON you provide.
"""
from __future__ import annotations

import re

# property category card → Booking's stable automation id (from category.html)
PROPERTY_TYPE_ID = {
    "hotel": 204, "resort": 204, "guesthouse": 216, "bnb": 208, "homestay": 222,
    "hostel": 203, "aparthotel": 219, "apartment": 219, "villa": 223, "holiday_home": 223,
}

# Booking amenity checkbox label → words that, if present in YOUR data, tick it
BOOKING_AMENITY_SYNONYMS = {
    "Restaurant": ["restaurant"],
    "Room service": ["room service"],
    "Bar": ["bar"],
    "24-hour front desk": ["24-hour front desk", "24x7 front desk", "front desk", "reception", "24x7", "24/7"],
    "Sauna": ["sauna"],
    "Fitness center": ["fitness", "gym"],
    "Garden": ["garden"],
    "Terrace": ["terrace"],
    "Airport shuttle": ["airport shuttle", "airport pickup", "airport transfer"],
    "Family rooms": ["family room", "family rooms"],
    "Spa": ["spa"],
    "Hot tub/Jacuzzi": ["jacuzzi", "hot tub"],
    "Free Wifi": ["free wifi", "wifi", "wi-fi"],
    "Air conditioning": ["air conditioning", "ac"],
    "Water park": ["water park"],
    "Electric vehicle charging station": ["ev charging", "electric vehicle"],
    "Swimming pool": ["swimming pool", "pool"],
    "Beach": ["beach"],
}


def _amenity_blob(p) -> str:
    """Every amenity signal from the JSON, normalised to one lowercased string."""
    f = p.facilities
    parts: list[str] = []
    if f.restaurant: parts.append("restaurant")
    if f.bar: parts.append("bar")
    if f.room_service: parts.append("room service")
    if f.fitness_center: parts.append("fitness center")
    if f.spa: parts.append("spa")
    if f.swimming_pool: parts.append("swimming pool")
    if f.airport_shuttle: parts.append("airport shuttle")
    if f.family_rooms: parts.append("family room")
    if f.ev_charging: parts.append("ev charging")
    if f.internet.wifi and f.internet.free: parts.append("free wifi")
    parts += list(f.other or [])
    parts += list(p.amenities or [])
    for r in p.room_types:
        parts += list(r.room_amenities or [])
    return " | ".join(str(x) for x in parts).lower()


def amenity_labels(p) -> list[str]:
    blob = _amenity_blob(p)
    out = [label for label, syns in BOOKING_AMENITY_SYNONYMS.items()
           if any(re.search(r"\b" + re.escape(s) + r"\b", blob) for s in syns)]
    if not p.policy.smoking_allowed:
        out.append("Non-smoking rooms")
    return out


def rules_for(p) -> list[tuple]:
    """(kind, locator, action, value). kind ∈ css | label | text | address.

    Deliberately ONE rule: the ADDRESS widget. That is the only control the LLM
    can't do — it needs Google-Places autocomplete + the map pin (dedicated code).
    EVERYTHING else (property category, single-vs-multiple, channel-manager, name,
    stars, amenities, yes/no, languages, policies) is owned by the LLM filler, which
    reads the ACTUAL page and maps from the JSON. Booking ships several layouts of
    the category/owner pages (e.g. `#automation_id_property_type_204` cards vs
    `#automation_id_choose_category`); hard-coded selectors only matched one of them
    and silently no-oped on the others. The LLM sees whatever is on screen, so it
    handles every variant.
    """
    a = p.address
    country = "India" if a.country.upper() == "IN" else a.country
    # Google Maps only matches REAL places, so offer most-specific → city fallback.
    addr_candidates = [
        f"{a.line1}, {a.city}, {a.state} {a.postal_code}",
        f"{a.city}, {a.state}, {country}",
        f"{a.city}, {a.state}",
        f"{a.city}, {country}",
        a.city,
    ]
    return [("address", "address", "address", {
        "candidates": addr_candidates,
        "fields": {
            "line1": a.line1, "city": a.city, "state": a.state,
            "postal": a.postal_code, "country": country,
        },
    })]
