# Booking.com — Full Onboarding Field Inventory

Every field Booking.com's "List your property" wizard asks for, mapped to the Property
Profile model and tagged AUTO (engine fills) or GATE (human/credential/verification).

> **Provenance & honesty:** this inventory is compiled from Booking.com's onboarding
> taxonomy/segments, not yet from a live DOM walk. Field labels and exact enum values are
> **reconciled during the selector-capture pass** (PLAN.md v1, task 1). Treat the
> Booking.com column as "what it asks for"; treat selectors as still-to-capture.

Legend: **AUTO** = filled from the profile · **GATE** = human step · **SYS** = Booking-side
· *(derived)* = computed, not a stored field.

---

## Segment 0 — Scope & category

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Are you listing one property or a group? | AUTO | `listing_scope` | `single_property` \| `property_group`. Group = parent + N child properties (v-next). |
| Property type/category | AUTO | `property_type` | hotel, apartment, aparthotel, guesthouse, homestay, B&B, hostel, resort, villa, holiday_home |
| How many properties? (if group) | AUTO | *(group, v-next)* | Only when scope = group |

---

## Segment 1 — Property details

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Property name | AUTO | `display_name` | |
| **Star rating** | AUTO | `star_rating` | 0/None = unrated, else 1–5 |
| **Setting / location type** | AUTO | `location_type` | city_centre, suburb, beach, lakeside, countryside, mountain, near_airport, near_station |
| Description | AUTO | `description` | Booking may re-write; we provide context |
| Currency | AUTO | `currency` | property default |
| Opening date / is it open yet | AUTO | `opening_date`, `is_currently_open` | |
| Total number of rooms/units | AUTO | *(derived)* `total_rooms` | Σ room_types[].count |

---

## Segment 2 — Location

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Country/region | AUTO | `address.country` | default `IN` |
| Street address | AUTO | `address.line1`, `line2` | |
| City | AUTO | `address.city` | |
| State/region | AUTO | `address.state` | |
| Postal code | AUTO | `address.postal_code` | |
| **Map pin (exact location)** | AUTO | `address.latitude`, `longitude` | If present, drop pin directly; else geocode the address |

---

## Segment 3 — Rooms & layout (per room type)

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Room name/category | AUTO | `room_types[].name` | |
| Number of this room | AUTO | `room_types[].count` | |
| Max occupancy (adults/children) | AUTO | `max_adults`, `max_children` | |
| Bed types & quantities | AUTO | `beds[]` (`bed_type`,`count`) | single, double, queen, king, twin, bunk, sofa_bed |
| Bathroom (private/shared) | AUTO | `room_types[].bathroom` | |
| Room size | AUTO | `room_types[].size_sqm` | |
| Smoking room? | AUTO | `room_types[].smoking` | |
| Extra bed available? | AUTO | `room_types[].extra_bed_available` | |
| Room amenities | AUTO | `room_types[].room_amenities[]` | ac, tv, safe, minibar, kettle, balcony, bathtub… |

---

## Segment 4 — Pricing

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Price per night (per room type) | AUTO | `room_types[].base_rate` | |
| Currency | AUTO | `room_types[].currency` | |

---

## Segment 5 — Facilities & amenities

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Parking (free/paid, on-site) | AUTO | `facilities.parking` | available, type, on_site, reservation_needed |
| Breakfast (type, included, price) | AUTO | `facilities.breakfast` | none/continental/buffet/a_la_carte/asian/veg/full_english |
| Internet / WiFi (free, coverage) | AUTO | `facilities.internet` | coverage: none/rooms/public_areas/all_areas |
| Swimming pool | AUTO | `facilities.swimming_pool` | |
| Spa | AUTO | `facilities.spa` | |
| Fitness centre | AUTO | `facilities.fitness_center` | |
| Restaurant | AUTO | `facilities.restaurant` | |
| Bar | AUTO | `facilities.bar` | |
| Room service | AUTO | `facilities.room_service` | |
| Airport shuttle | AUTO | `facilities.airport_shuttle` | |
| Laundry | AUTO | `facilities.laundry` | |
| Business centre | AUTO | `facilities.business_center` | |
| EV charging | AUTO | `facilities.ev_charging` | |
| Elevator/lift | AUTO | `facilities.elevator` | |
| Family rooms | AUTO | `facilities.family_rooms` | |
| Languages spoken by staff | AUTO | `facilities.languages_spoken[]` | |
| Accessibility features | AUTO | `facilities.accessibility[]` | |
| Other / long-tail | AUTO | `facilities.other[]`, `amenities[]` | unmapped extras |

---

## Segment 6 — Photos

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Property photos (min ~5) | AUTO | `photos[]` (`path`/`url`) | |
| Photo captions | AUTO | `photos[].caption` | |
| Per-room photos | AUTO | `photos[].room_type` | links a photo to a room type |

---

## Segment 7 — Policies

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Check-in window | AUTO | `policy.checkin_from` | |
| Check-out window | AUTO | `policy.checkout_until` | |
| Cancellation policy | AUTO | `policy.cancellation_tier` + `free_cancellation_until_hours` | flexible/moderate/strict/non_refundable |
| Prepayment | AUTO | `policy.prepayment` | none/partial/full |
| Minimum check-in age | AUTO | `policy.min_checkin_age` | |
| Smoking allowed? | AUTO | `policy.smoking_allowed` | |
| Quiet hours | AUTO | `policy.quiet_hours_from/until` | |
| Pets allowed? + fee | AUTO | `policy.pets_allowed`, `pet_fee` | |
| Child policy (ages, free under, cots) | AUTO | `policy.child_policy` | children_allowed, min_age, free_stay_under_age, extra_bed_fee, crib_available, crib_fee |
| Payment methods accepted at property | AUTO | `policy.payment_methods[]` | cash, visa, mastercard, amex, upi… |
| House rules | AUTO | `policy.house_rules[]` | |
| Reception hours / 24h front desk | AUTO | `reception` | is_24h, hours_from/until |

---

## Segment 8 — Contact

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Contact person name | AUTO | `contact.full_name` | |
| Email | AUTO | `contact.email` | also the OTP target |
| Phone | AUTO | `contact.phone` | E.164 |

---

## Segment 9 — Tax / legal entity

| Booking.com asks | Type | Model field | Notes |
|---|---|---|---|
| Business or individual? | AUTO | `compliance.business_type` | |
| Legal entity name | AUTO | `compliance.legal_entity_name` | |
| Tax ID / GST | AUTO | `compliance.gstin` | 15-char (validated) |
| PAN | AUTO | `compliance.pan` | 10-char (validated) |
| Company registration number | AUTO | `compliance.business_registration_number` | CIN/registration |

---

## Segment 10 — Account, payout, contract (GATE — never auto-filled)

| Booking.com asks | Type | Where the data lives | Notes |
|---|---|---|---|
| Partner email + password | **GATE** | `gate_data.json` → `account` | Account creation — human only |
| Email/phone verification (OTP) | **GATE** | `gate_data.json` → `verification` | Auto-resolvable in v1.1 |
| Bank / payout details | **GATE** | `gate_data.json` → `payout` | Human only; never in the profile (ADR-005) |
| Partner agreement / commission | **GATE** | `gate_data.json` → `contract` | Human reads + accepts |
| CAPTCHA | **GATE** | — | Stealth-avoided; solver fallback (v1.1) |
| Submit → review → live | **SYS** | — | Booking-side |

---

## Coverage summary

- **AUTO fields modelled:** Segments 0–9 — scope, category, star rating, setting, full address+pin,
  rooms/beds/bathroom/occupancy/amenities, rates, full facilities taxonomy, photos, full policy block,
  contact, tax/legal entity. ✅
- **GATE data:** Segment 10 — held separately in `gate_data.json`, typed by a human. ✅
- **Not yet modelled (deliberate):** group/multi-property sub-structure (v-next), and any
  market-specific extras the live wizard reveals during selector-capture.

See the live mapping any time with:
```bash
python -m accounts_pilot.cli fields --profile examples/test_property_full.json --ota booking_com
```
