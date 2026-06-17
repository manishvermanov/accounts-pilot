"""Convert a DigiStay/tally property export (query_result_*.json) into the
PropertyProfile JSON our onboarding service consumes. Validates before writing.

Usage:  python scripts/convert_tally_export.py <export.json> <out.json>
"""
import json, re, sys

SRC = sys.argv[1] if len(sys.argv) > 1 else r"C:/Users/manis/Downloads/query_result_2026-06-11T12_52_07.975903222Z (1).json"
OUT = sys.argv[2] if len(sys.argv) > 2 else r"C:/Users/manis/Downloads/manchester-royals.json"

src = json.load(open(SRC, encoding="utf-8"))[0]
addr = json.loads(src["property_address"])
ams = [a["amenity_name"] for a in json.loads(src["property_amenities"])]
rts = json.loads(src["room_types"])
imgs = json.loads(src["property_images"])
amset = {a.lower() for a in ams}


def has(*kw):
    return any(any(k in a for a in amset) for k in kw)


def t24(s):                                  # "11:00 AM" -> "11:00"
    s = (s or "").strip()
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)?", s, re.I)
    if not m:
        return s
    h = int(m.group(1)); mn = m.group(2); ap = (m.group(3) or "").upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mn}"


gstin = (src.get("service_gst") or src.get("gst") or "").strip() or None
pan = gstin[2:12] if gstin and len(gstin) == 15 else None        # PAN = GSTIN chars 3..12

mp = re.search(r"\+?91[\s-]?(\d{5})[\s-]?(\d{5})", src.get("terms_and_conditions", ""))
phone = ("+91" + mp.group(1) + mp.group(2)) if mp else None


def sqft_to_sqm(s):
    m = re.search(r"([\d.]+)", s or "")
    if not m:
        return None
    v = float(m.group(1))
    if "sq. m" in (s or "").lower() or "sqm" in (s or "").lower():
        return round(v, 1)
    return round(v * 0.092903, 1)            # sq ft -> sq m


BEDMAP = {"single": "single", "twin": "twin", "double": "double", "queen": "queen",
          "king": "king", "sofa": "sofa_bed", "bunk": "bunk"}


def bedtype(s):
    s = s.lower()
    for k, v in BEDMAP.items():
        if k in s:
            return v
    return "double"


def room_amen(names):
    n = {x.lower() for x in names}
    rules = {"tv": ["tv", "television"], "minibar": ["fridge", "minibar", "refrigerator"],
             "kettle": ["kettle", "coffee", "tea maker"], "safe": ["safe"],
             "hairdryer": ["hairdryer", "hair dryer"], "ac": ["air condition"],
             "wifi": ["wifi", "wi-fi"], "desk": ["desk", "work area", "workspace"],
             "wardrobe": ["wardrobe", "clothes rack"], "balcony": ["balcony"],
             "telephone": ["telephone", "intercom"], "water_heater": ["geyser", "hot & cold", "hot and cold"],
             "toiletries": ["toiletr", "shampoo", "body wash", "dental"],
             "smart_lock": ["smart lock", "access card", "key access"],
             "towels": ["towels"], "seating": ["seating"]}
    return [tok for tok, kw in rules.items() if any(any(k in a for a in n) for k in kw)]


rooms = []
for r in rts:
    rooms.append({
        "name": r["room_type_name"],
        "count": 1,                          # NOT in export — placeholder, set real inventory
        "max_adults": r.get("max_occupancy") or r.get("base_occupancy") or 2,
        "max_children": r.get("max_child_occupancy", 0),
        "beds": [{"bed_type": bedtype(b), "count": r.get("bed_count", 1)}
                 for b in (r.get("bed_type") or ["Double bed"])],
        "bathroom": "private",
        "size_sqm": sqft_to_sqm(r.get("size")),
        "base_rate": r.get("base_price"),
        "currency": "INR",
        "extra_bed_available": bool(r.get("extra_adult_charge")),
        "room_amenities": room_amen([a["amenity_name"] for a in r.get("amenities", [])]),
    })

photos = [{"url": im["download_url"], "caption": im.get("tag")}
          for im in imgs if im.get("download_url")]
for r in rts:
    for im in r.get("images", []):
        if im.get("download_url"):
            photos.append({"url": im["download_url"], "caption": im.get("tag"),
                           "room_type": r["room_type_name"]})

out = {
    "property_id": src["property_id"],
    "listing_scope": "single_property",
    "property_type": (src.get("property_type") or "hotel").lower(),
    "display_name": src["property_name"],
    "star_rating": None,                     # not in export
    "location_type": "city_centre",
    "description": src.get("property_description") or None,
    "currency": src.get("currency") or "INR",
    "is_currently_open": True,
    "address": {
        "line1": addr.get("street"),
        "line2": addr.get("landmark") or addr.get("apartment_building_flat"),
        "city": addr.get("city"),
        "state": (addr.get("state") or "").title(),
        "country": "IN",
        "postal_code": addr.get("pincode"),
        "latitude": float(addr["latitude"]) if addr.get("latitude") else None,
        "longitude": float(addr["longitude"]) if addr.get("longitude") else None,
    },
    "contact": {
        "full_name": src.get("billing_name") or src["property_name"],
        "email": src.get("owner_email"),
        "phone": phone,
    },
    "compliance": {
        "legal_entity_name": src.get("registered_business_name") or src.get("billing_name") or src["property_name"],
        "business_type": "business",
        "gstin": gstin,
        "pan": pan,
    },
    "policy": {
        "checkin_from": t24(src.get("checkin_time_ist", "14:00")),
        "checkin_until": "23:00",
        "checkout_from": "00:00",
        "checkout_until": t24(src.get("checkout_time_ist", "11:00")),
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
    "amenities": ams,                        # full original amenity list (long-tail for the LLM)
    "photos": photos,
    "reception": {"is_24h": has("24x7 front desk", "24-hour", "reception")},
    "room_types": rooms,
}

from accounts_pilot.models.property_profile import PropertyProfile
PropertyProfile.model_validate(out)          # raises if anything is wrong
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("VALID OK  rooms:", len(rooms), " photos:", len(photos), " amenities:", len(ams))
print("wrote:", OUT)
print("gstin:", gstin, " pan:", pan, " phone:", phone)
