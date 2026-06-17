# MIS integration — search a hotel instead of pasting JSON

The dashboard's front door is now **search the MIS → pick a hotel → it converts
the record to a profile behind the scenes → fill the OTAs**. The operator never
pastes or sees the raw JSON (a collapsed *View JSON* toggle is there for debugging).

## Flow

1. `GET /api/mis/search?q=<name>` → small list `[{id, name, city, state}]`.
2. Operator clicks a hotel.
3. `GET /api/mis/hotel/{id}` → fetches the full MIS record, runs
   `normalize_to_profile` (the "make the JSON right" step), validates it against
   `PropertyProfile`, returns `{summary, profile}`.
4. The UI shows the **summary card** and keeps the **profile** in memory; the OTA
   *Fill* buttons use it exactly as before.

## Configure the live MIS (REST)

Set these in `.env` (gitignored — never commit, never paste secrets in chat):

```
MIS_BASE_URL=https://mis.digistay.ai/api      # set this to go live; blank = offline folder
MIS_AUTH_HEADER=Authorization
MIS_AUTH_VALUE=Bearer <token>
MIS_SEARCH_PATH=/hotels
MIS_SEARCH_PARAM=search
MIS_FETCH_PATH=/hotels/{id}
MIS_TIMEOUT_S=20
```

Resulting calls:
- **search** → `GET {MIS_BASE_URL}{MIS_SEARCH_PATH}?{MIS_SEARCH_PARAM}=<name>`
  Response may be a bare array, or `{results: []}` / `{data: []}`.
  Each row needs `property_id` (or `id`) and `property_name` (or `display_name`/`name`);
  `city`/`state` are read from `address` or the stringified `property_address`.
- **fetch** → `GET {MIS_BASE_URL}{MIS_FETCH_PATH}` with `{id}` substituted.
  Response may be the row, an array-of-one, or `{data: {...}}`.

The fetched row is the **DigiStay "personal data collection" query result** shape
(columns like `property_amenities`, `room_types`, `property_images`,
`property_address` arriving as stringified JSON). `accounts_pilot/mis/convert.py`
handles that shape; if a record is already a `PropertyProfile`, it passes through.

## Offline fallback (works today, no creds)

When `MIS_BASE_URL` is blank, `FolderMisProvider` indexes `MIS_FOLDER`
(default `examples/booking_engine/`) and searches the JSON files there — both raw
MIS exports and already-converted profiles are supported. This is how search works
right now with the hotels already on disk (e.g. *Hotel Manchester Royals LLP*).

## Code map

| Piece | File |
|-------|------|
| Convert raw MIS row → profile (+ EP/CP rate plans) | `accounts_pilot/mis/convert.py` |
| Search providers (REST live / folder fallback)     | `accounts_pilot/mis/provider.py` |
| Endpoints                                           | `accounts_pilot/web/app.py` (`/api/mis/*`) |
| Settings                                            | `accounts_pilot/config.py` (`mis_*`) |
