# Accounts Pilot — Data Model Reference

The canonical **Property Profile** is the single input to every onboarding. Defined in
`accounts_pilot/models/property_profile.py` (Pydantic v2). Expanded to cover Booking.com's
full field set — see [BOOKING-FIELDS.md](BOOKING-FIELDS.md) for the field-by-field OTA mapping.

> **Bank / payout details are intentionally absent** — human-only gate ([ADR-005](DECISIONS.md)).
> New fields are optional with defaults, so simpler profiles still validate.

---

## PropertyProfile (root)

| Field | Type | Req | Notes |
|---|---|---|---|
| `property_id` | str | ✅ | Internal id |
| `listing_scope` | enum | ◻ | `single_property` (default) · `property_group` |
| `property_type` | enum | ✅ | hotel · apartment · aparthotel · guesthouse · homestay · bnb · hostel · resort · villa · holiday_home |
| `display_name` | str | ✅ | Public listing name |
| `star_rating` | int 0–5 | ◻ | 0/None = unrated |
| `location_type` | enum | ◻ | city_centre · suburb · beach · lakeside · countryside · mountain · near_airport · near_station |
| `description` | str | ◻ | |
| `currency` | str | ◻ | default `INR` |
| `opening_date` | str | ◻ | ISO date |
| `is_currently_open` | bool | ◻ | default true |
| `address` | Address | ✅ | |
| `contact` | Contact | ✅ | |
| `compliance` | Compliance | ✅ | |
| `room_types` | RoomType[] | ✅ | ≥ 1 |
| `facilities` | Facilities | ◻ | structured amenities |
| `amenities` | str[] | ◻ | legacy/simple long-tail |
| `photos` | Photo[] | ◻ | |
| `policy` | Policy | ◻ | defaults applied |
| `reception` | Reception | ◻ | front-desk hours |

Derived: `total_rooms` = Σ `room_types[].count`.

## Address
`line1`✅ · `line2` · `city`✅ · `state`✅ · `country`(IN) · `postal_code`✅ · `latitude` · `longitude`
*(lat/long strongly recommended — drives the map pin without geocoding)*

## Contact
`full_name`✅ · `email`✅ (EmailStr, also OTP target) · `phone`✅ (E.164)

## Compliance
`legal_entity_name`✅ · `business_type` (business/individual) · `gstin` (15-char, validated) ·
`pan` (10-char, validated) · `business_registration_number`

## RoomType
`name`✅ · `count`✅ ≥1 · `max_adults`✅ · `max_children`(0) · `beds[]` (BedConfig) ·
`bathroom` (private/shared) · `size_sqm` · `base_rate`✅ >0 · `currency`(INR) · `smoking` ·
`extra_bed_available` · `room_amenities[]`
**BedConfig:** `bed_type` ∈ single·double·queen·king·twin·bunk·sofa_bed, `count`

## Facilities
- `parking` → available · type(none/free/paid) · on_site · reservation_needed
- `breakfast` → available · included_in_rate · type(none/continental/buffet/a_la_carte/asian/vegetarian/full_english) · price_per_person · currency
- `internet` → wifi · free · coverage(none/rooms/public_areas/all_areas)
- booleans: `swimming_pool` `spa` `fitness_center` `restaurant` `bar` `room_service` `airport_shuttle` `laundry` `business_center` `ev_charging` `elevator` `family_rooms`
- lists: `languages_spoken[]` · `accessibility[]` · `other[]`

## Photo
`path`◻* · `url`◻* · `caption` · `room_type` *(\* exactly one of path/url required)*

## Policy
`checkin_from`(14:00) · `checkout_until`(11:00) · `cancellation_tier`(flexible/moderate/strict/non_refundable) ·
`free_cancellation_until_hours` · `prepayment`(none/partial/full) · `min_checkin_age` ·
`smoking_allowed` · `quiet_hours_from/until` · `pets_allowed` · `pet_fee` · `payment_methods[]` ·
`house_rules[]` · `child_policy`
**ChildPolicy:** `children_allowed` · `min_age` · `free_stay_under_age` · `extra_bed_fee` · `crib_available` · `crib_fee`

## Reception
`is_24h`(true) · `hours_from` · `hours_until`

---

## Job model (`models/job.py`)

`job_id` (`<ota>__<property_id>`) · `property_id` · `ota` · `state` (JobState) ·
`current_step` · `waiting_on` (GateKind) · `history` (JobEvent[])

**JobState:** draft · filling · awaiting_account · awaiting_otp · awaiting_bank ·
awaiting_contract · awaiting_captcha · submitted · under_review · live · failed · needs_fix
**GateKind:** account · otp · bank · contract · captcha *(account/bank/contract are HUMAN_ONLY)*

---

## Examples & inspection

| File | What |
|---|---|
| [`examples/sample_property.json`](../examples/sample_property.json) | Minimal valid profile (24 rooms) |
| [`examples/test_property_full.json`](../examples/test_property_full.json) | **Fully-populated** Booking.com profile (30 rooms, every field) |
| [`examples/gate_data.example.json`](../examples/gate_data.example.json) | Human GATE-step data (account/bank/tax/contract) |

```bash
python -m accounts_pilot.cli validate examples/test_property_full.json
python -m accounts_pilot.cli fields   --profile examples/test_property_full.json --ota booking_com
```
