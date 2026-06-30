"""Turn a raw MIS hotel record into a validated PropertyProfile dict.

The MIS row (the DigiStay "personal data collection" query result) carries some
columns as *stringified* JSON (property_amenities, room_types, property_images,
property_address, …). `normalize_to_profile` detects that shape and converts it.

If it's handed something that is ALREADY a PropertyProfile (e.g. a pre-converted
file in the folder fallback), it passes it through untouched (only filling in
rate_plans if missing). This is the single "make the JSON right" step.
"""
from __future__ import annotations

import json
import re
import types
import typing
from copy import deepcopy
from enum import Enum
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from accounts_pilot.models.property_profile import PropertyProfile, RoomType


# --------------------------------------------------------------------------- #
# full-schema skeleton — every field present with its empty default, so the
# table editor shows ALL OTA fields (the operator fills the ones the MIS lacks).
# --------------------------------------------------------------------------- #
def _inner_type(ann):
    origin = typing.get_origin(ann)
    union = (typing.Union, getattr(types, "UnionType", None))
    if origin in union:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return args[0] if args else ann
    return ann


def _skeleton(model) -> dict:
    out: dict = {}
    for name, f in model.model_fields.items():
        ann = _inner_type(f.annotation)
        origin = typing.get_origin(ann)
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out[name] = _skeleton(ann)
        elif origin in (list, tuple) or ann is list:
            out[name] = []
        else:
            if f.default is not PydanticUndefined:
                d = f.default
            elif f.default_factory is not None:
                try:
                    d = f.default_factory()
                except Exception:
                    d = None
            else:
                d = None                       # required-without-default → empty for the operator
            if isinstance(d, Enum):
                d = d.value
            if isinstance(d, (list, dict)):
                d = deepcopy(d)
            out[name] = d
    return out


_SKELETON = _skeleton(PropertyProfile)
_ROOM_SKELETON = _skeleton(RoomType)


def _deep_merge(base: dict, over: dict) -> dict:
    """Overlay `over` onto `base` in place; real values win, defaults stay for gaps."""
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _loads(v: Any) -> Any:
    """A MIS column may arrive as a JSON string or already parsed."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _t24(s: Any) -> str:
    """'11:00 AM' -> '11:00'.  Also accepts a datetime.time (Postgres returns one):
    str(time(12,0)) == '12:00:00' -> '12:00'."""
    s = str(s or "").strip()
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)?", s, re.I)
    if not m:
        return s or "14:00"
    h = int(m.group(1)); mn = m.group(2); ap = (m.group(3) or "").upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mn}"


def _sqft_to_sqm(s: str | None) -> float | None:
    m = re.search(r"([\d.]+)", s or "")
    if not m:
        return None
    v = float(m.group(1))
    low = (s or "").lower()
    if "sq. m" in low or "sqm" in low or "sq.m" in low:
        return round(v, 1)
    return round(v * 0.092903, 1)            # sq ft -> sq m


_BEDMAP = {"single": "single", "twin": "twin", "double": "double", "queen": "queen",
           "king": "king", "sofa": "sofa_bed", "bunk": "bunk"}


def _bedtype(s: str) -> str:
    s = (s or "").lower()
    for k, v in _BEDMAP.items():
        if k in s:
            return v
    return "double"


def is_raw_mis_record(rec: dict) -> bool:
    """A raw MIS row has the export's hallmark columns; a PropertyProfile does not."""
    return any(k in rec for k in ("property_amenities", "property_address", "property_images")) \
        or (isinstance(rec.get("room_types"), str))


# --------------------------------------------------------------------------- #
# rate plans — EP always, CP when breakfast is sold
# --------------------------------------------------------------------------- #
def _ensure_rate_plans(profile: dict) -> None:
    bf = ((profile.get("facilities") or {}).get("breakfast") or {})
    bf_price = bf.get("price_per_person")
    bf_avail = bool(bf.get("available"))
    for rt in profile.get("room_types") or []:
        if rt.get("rate_plans"):
            continue
        base = rt.get("base_rate")
        if not base:
            continue
        plans = [{"code": "EP", "name": "Room Only", "price": base}]
        if bf_avail:
            # CP = room + breakfast; supplement = per-person price (×1) when known, else +400
            add = bf_price if isinstance(bf_price, (int, float)) and bf_price > 0 else 400
            plans.append({"code": "CP", "name": "Breakfast", "price": base + add})
        rt["rate_plans"] = plans


# --------------------------------------------------------------------------- #
# raw MIS row -> profile
# --------------------------------------------------------------------------- #
def raw_to_profile(src: dict) -> dict:
    addr = _loads(src.get("property_address")) or {}
    ams = [a.get("amenity_name") for a in (_loads(src.get("property_amenities")) or []) if a.get("amenity_name")]
    rts = _loads(src.get("room_types")) or []
    imgs = _loads(src.get("property_images")) or []
    amset = {(a or "").lower() for a in ams}

    def has(*kw: str) -> bool:
        return any(any(k in a for a in amset) for k in kw)

    gstin = (src.get("service_gst") or src.get("gst") or "").strip() or None
    pan = gstin[2:12] if gstin and len(gstin) == 15 else None     # PAN = GSTIN chars 3..12

    mp = re.search(r"\+?91[\s-]?(\d{5})[\s-]?(\d{5})", src.get("terms_and_conditions", "") or "")
    phone = ("+91" + mp.group(1) + mp.group(2)) if mp else None

    rooms = []
    for r in rts:
        rooms.append({
            "name": r.get("room_type_name"),
            "count": r.get("count") or 1,            # NOT in export — placeholder inventory
            "max_adults": r.get("max_occupancy") or r.get("base_occupancy") or 2,
            "max_children": r.get("max_child_occupancy", 0) or 0,
            "base_occupancy": r.get("base_occupancy"),
            "base_child_occupancy": r.get("base_child_occupancy"),
            "max_occupancy": r.get("max_occupancy"),
            "extra_adult_charge": r.get("extra_adult_charge"),
            "extra_child_charge": r.get("extra_child_charge"),
            "description": (r.get("description") or "").strip() or None,
            "beds": [{"bed_type": _bedtype(b), "count": r.get("bed_count", 1)}
                     for b in (r.get("bed_type") or ["Double bed"])],
            "bathroom": "private",
            "size_sqm": _sqft_to_sqm(r.get("size")),
            "base_rate": r.get("base_price"),
            "currency": src.get("currency") or "INR",
            "extra_bed_available": bool(r.get("extra_adult_charge")),
            "room_amenities": [a.get("amenity_name") for a in (r.get("amenities") or []) if a.get("amenity_name")],
        })

    photos = [{"url": im["download_url"], "caption": im.get("tag")}
              for im in imgs if im.get("download_url")]
    for r in rts:
        for im in (r.get("images") or []):
            if im.get("download_url"):
                photos.append({"url": im["download_url"], "caption": im.get("tag"),
                               "room_type": r.get("room_type_name")})

    out = {
        "property_id": src.get("property_id"),
        "listing_scope": "single_property",
        "property_type": (src.get("property_type") or "hotel").lower(),
        "display_name": src.get("property_name"),
        "star_rating": None,
        "location_type": "city_centre",
        "description": src.get("property_description") or None,
        "currency": src.get("currency") or "INR",
        "is_currently_open": True,
        "address": {
            "line1": addr.get("street") or src.get("property_name"),
            "line2": addr.get("landmark") or addr.get("apartment_building_flat"),
            "city": addr.get("city"),
            "state": (addr.get("state") or "").title(),
            "country": "IN",
            "postal_code": addr.get("pincode"),
            "latitude": float(addr["latitude"]) if addr.get("latitude") else None,
            "longitude": float(addr["longitude"]) if addr.get("longitude") else None,
        },
        "contact": {
            "full_name": src.get("billing_name") or src.get("property_name"),
            # prefer a HOTEL-specific email; only fall back to the account owner's email
            # (owner_email is the DigiStay account holder — shared across that account's
            # properties, e.g. testmanish8070@gmail.com — not the hotel's own contact).
            "email": (src.get("property_email") or src.get("hotel_email")
                      or src.get("reservation_email") or src.get("contact_email")
                      or src.get("service_email") or src.get("email") or src.get("owner_email")),
            "phone": phone,
        },
        "compliance": {
            "legal_entity_name": src.get("registered_business_name") or src.get("billing_name") or src.get("property_name"),
            "business_type": "business",
            "gstin": gstin,
            "pan": pan,
            "gst_registered": bool(gstin),
            # owner_name in the MIS is an email, not a person — leave KYC names for the operator
        },
        "policy": {
            "checkin_from": _t24(src.get("checkin_time_ist", "14:00")),
            "checkin_until": "23:00",
            "checkout_from": "00:00",
            "checkout_until": _t24(src.get("checkout_time_ist", "11:00")),
            "cancellation_tier": "moderate",
            "child_policy": {"children_allowed": True, "free_stay_under_age": 6},
        },
        "facilities": {
            "parking": {"available": has("parking"), "type": "free", "on_site": True},
            "internet": {"wifi": has("wifi", "lan"), "free": True, "coverage": "all_areas"},
            "spa": has("spa", "massage", "ayurvedic"),
            "restaurant": has("restaurant"),
            "bar": has("bar"),
            "room_service": has("room service"),
            "airport_shuttle": has("airport"),
            "laundry": has("laundry", "laundromat"),
            "business_center": has("meeting room", "printer", "photocopy"),
            "ev_charging": has("ev charging"),
            "elevator": has("elevator", "lift"),
            "fitness_center": has("gym", "fitness"),
            "swimming_pool": has("swimming", "pool"),
            "languages_spoken": ["English", "Hindi"],
            "accessibility": [x for x, k in [("wheelchair_accessible", "wheelchair"), ("ramp", "ramp"),
                              ("accessible_bathroom", "grabrail"), ("accessible_elevator", "elevator with access")]
                              if has(k)],
        },
        "amenities": ams,
        "photos": photos,
        "reception": {"is_24h": has("24x7 front desk", "24-hour", "reception")},
        "room_types": rooms,
    }
    return out


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def normalize_to_profile(record: dict, *, validate: bool = True) -> dict:
    """Accept a raw MIS row OR an already-built profile; return a profile dict with
    EVERY schema field present (empty where unknown), so the table editor surfaces
    all OTA fields. rate_plans are filled in. Validates unless validate=False."""
    if isinstance(record, list):                 # MIS often returns an array of one
        record = record[0] if record else {}
    built = raw_to_profile(record) if is_raw_mis_record(record) else dict(record)

    profile = deepcopy(_SKELETON)                # full field set with empty defaults
    _deep_merge(profile, built)                  # real/edited values win
    # give each room the full room field set too
    profile["room_types"] = [_deep_merge(deepcopy(_ROOM_SKELETON), r)
                             for r in (profile.get("room_types") or [])]

    _ensure_rate_plans(profile)
    if validate:
        PropertyProfile.model_validate(profile)  # raises on any bad field
    return profile


def summarize(profile: dict) -> dict:
    """Compact card the UI shows after a hotel is picked (no raw dump)."""
    addr = profile.get("address") or {}
    rts = profile.get("room_types") or []
    return {
        "id": profile.get("property_id"),
        "name": profile.get("display_name"),
        "type": profile.get("property_type"),
        "stars": profile.get("star_rating"),
        "city": addr.get("city"),
        "state": addr.get("state"),
        "room_types": [{"name": r.get("name"), "count": r.get("count"),
                        "base_rate": r.get("base_rate"),
                        "rate_plans": r.get("rate_plans") or []} for r in rts],
        "total_rooms": sum((r.get("count") or 0) for r in rts),
        "photos": len(profile.get("photos") or []),
        "amenities": len(profile.get("amenities") or []),
        "currency": profile.get("currency") or "INR",
    }
