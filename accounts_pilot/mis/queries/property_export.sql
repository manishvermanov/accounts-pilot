-- DigiStay "personal data collection" export for ONE property.
-- Identical to the operator's hand-run query, except the single input is now a
-- bind parameter (%(property_id)s) instead of a hardcoded UUID, so Accounts Pilot
-- can run it for any hotel. Returns exactly one row in the export shape that
-- accounts_pilot/mis/convert.py consumes. READ-ONLY (a single SELECT).
WITH
params AS (
    SELECT %(property_id)s::text AS property_id
),

property_info AS (
    SELECT
        p.id,
        p.name,
        trim(replace(replace(regexp_replace(p.description, '<[^>]*>', '', 'g'),
            '&nbsp;', ' '), '&amp;', '&')) AS description,
        trim(replace(replace(regexp_replace(p.terms_and_conditions, '<[^>]*>', '', 'g'),
            '&nbsp;', ' '), '&amp;', '&')) AS terms_and_conditions,
        p.type, p.currency, p.language, p.timezone,
        p.checkin_time, p.checkout_time,
        p.registered_business_name, p.gst, p.service_gst,
        p.hotel_license_number, p.billing_name
    FROM public.property p
    WHERE p.id = (SELECT property_id FROM params)
      AND p.is_deleted = FALSE AND p.is_active = TRUE
),

property_owner AS (
    SELECT u.name AS owner_name, u.email AS owner_email
    FROM public.user_access_scope uas
    JOIN public.user_role ur ON ur.user_access_scope_id = uas.id
        AND ur.is_active = TRUE AND ur.is_deleted = FALSE
    JOIN public.role r ON r.id = ur.role_id AND r.name = 'Property Owner'
        AND r.is_active = TRUE AND r.is_deleted = FALSE
    JOIN public."user" u ON u.id = uas.user_id AND u.is_deleted = FALSE
    WHERE uas.property_id = (SELECT property_id FROM params)
      AND uas.is_active = TRUE AND uas.is_deleted = FALSE
    ORDER BY uas.created_at ASC
    LIMIT 1
),

property_policies AS (
    SELECT JSON_AGG(JSON_BUILD_OBJECT(
        'policy_id', pol.id, 'name', pol.name,
        'key_points', pol.key_points, 'content', pol.content
    ) ORDER BY pol.name ASC) AS policies
    FROM public.policy pol
    WHERE pol.property_id = (SELECT property_id FROM params)
      AND pol.is_deleted = FALSE AND pol.is_active = TRUE
),

property_amenities AS (
    SELECT JSON_AGG(JSON_BUILD_OBJECT(
        'amenity_id', a.id, 'amenity_name', a.name,
        'is_highlighted', la.is_highlighted, 'assigned_at', la.assigned_at
    ) ORDER BY a.name ASC) AS amenities
    FROM public.linked_amenity la
    JOIN public.amenity a ON a.id = la.amenity_id
    WHERE la.property_id = (SELECT property_id FROM params)
      AND la.room_type_id IS NULL
      AND la.is_deleted = FALSE AND la.is_active = TRUE
),

property_images AS (
    SELECT JSON_AGG(JSON_BUILD_OBJECT(
        'category_id', ic.id, 'tag', ic.name, 'category_type', ic.type,
        'category_rank', ic.rank, 'file_id', f.id, 'file_name', f.name,
        'mime_type', f.type, 'content_type', f.content_type,
        'download_url', 'https://digistay-new.s3.ap-south-1.amazonaws.com/' || f.blob_name
    ) ORDER BY ic.rank ASC, f.created_at ASC) AS images
    FROM public.image_category ic
    JOIN public.file f ON f.image_category_id = ic.id
        AND f.is_deleted = FALSE AND f.is_active = TRUE
    WHERE ic.property_id = (SELECT property_id FROM params)
      AND ic.type = 'PROPERTY'
      AND ic.is_deleted = FALSE AND ic.is_active = TRUE
),

property_address AS (
    SELECT JSON_BUILD_OBJECT(
        'address_id', ad.id, 'street', ad.street, 'landmark', ad.landmark,
        'apartment_building_flat', ad.apartment_building_flat,
        'city', ad.city, 'state', ad.state, 'country', ad.country,
        'pincode', ad.pincode, 'latitude', ad.latitude, 'longitude', ad.longitude,
        'address_type', ad.address_type
    ) AS address
    FROM public.address ad
    WHERE ad.entity_id = (SELECT property_id FROM params)
      AND ad.entity_type = 'PROPERTY'
      AND ad.is_deleted = FALSE AND ad.is_active = TRUE
    ORDER BY ad.created_at ASC
    LIMIT 1
),

room_amenities AS (
    SELECT la.room_type_id,
        JSON_AGG(JSON_BUILD_OBJECT(
            'amenity_id', a.id, 'amenity_name', a.name,
            'is_highlighted', la.is_highlighted, 'assigned_at', la.assigned_at
        ) ORDER BY a.name ASC) AS amenities
    FROM public.linked_amenity la
    JOIN public.amenity a ON a.id = la.amenity_id
    WHERE la.property_id = (SELECT property_id FROM params)
      AND la.room_type_id IS NOT NULL
      AND la.is_deleted = FALSE AND la.is_active = TRUE
    GROUP BY la.room_type_id
),

room_images AS (
    SELECT ic.room_type_id,
        JSON_AGG(JSON_BUILD_OBJECT(
            'category_id', ic.id, 'tag', ic.name, 'category_type', ic.type,
            'category_rank', ic.rank, 'file_id', f.id, 'file_name', f.name,
            'mime_type', f.type, 'content_type', f.content_type,
            'download_url', 'https://digistay-new.s3.ap-south-1.amazonaws.com/' || f.blob_name
        ) ORDER BY ic.rank ASC, f.created_at ASC) AS images
    FROM public.image_category ic
    JOIN public.room_type rtt ON rtt.id = ic.room_type_id
        AND rtt.property_id = (SELECT property_id FROM params)
    JOIN public.file f ON f.image_category_id = ic.id
        AND f.is_deleted = FALSE AND f.is_active = TRUE
    WHERE ic.type = 'ROOM'
      AND ic.is_deleted = FALSE AND ic.is_active = TRUE
    GROUP BY ic.room_type_id
),

room_types AS (
    SELECT COUNT(*) AS room_type_count,
        JSON_AGG(JSON_BUILD_OBJECT(
            'room_type_id', rt.id, 'room_type_name', rt.name,
            'description', rt.description, 'size', rt.size,
            'base_price', rt.base_price, 'bed_count', rt.bed_count,
            'bed_type', rt.bed_type,
            'base_occupancy', rt.base_occupancy, 'max_occupancy', rt.max_occupancy,
            'base_child_occupancy', rt.base_child_occupancy,
            'max_child_occupancy', rt.max_child_occupancy,
            'extra_adult_charge', rt.extra_adult_charge,
            'extra_child_charge', rt.extra_child_charge,
            'amenities', ra.amenities, 'images', ri.images
        ) ORDER BY rt.name ASC) AS room_types
    FROM public.room_type rt
    LEFT JOIN room_amenities ra ON ra.room_type_id = rt.id
    LEFT JOIN room_images    ri ON ri.room_type_id = rt.id
    WHERE rt.property_id = (SELECT property_id FROM params)
      AND rt.is_deleted = FALSE AND rt.is_active = TRUE
)

SELECT
    pi.id AS property_id, pi.name AS property_name,
    pi.description AS property_description, pi.type AS property_type,
    pown.owner_name, pown.owner_email,
    pi.currency, pi.language, pi.timezone,
    (pi.checkin_time  AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')::time AS checkin_time_ist,
    (pi.checkout_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')::time AS checkout_time_ist,
    pi.registered_business_name, pi.gst, pi.service_gst,
    pi.hotel_license_number, pi.billing_name, pi.terms_and_conditions,
    pad.address AS property_address,
    pp.policies,
    pa.amenities AS property_amenities,
    pimg.images AS property_images,
    rt.room_type_count, rt.room_types
FROM       property_info      pi
LEFT JOIN  property_owner     pown ON TRUE
LEFT JOIN  property_address   pad  ON TRUE
CROSS JOIN property_policies  pp
CROSS JOIN property_amenities pa
CROSS JOIN property_images    pimg
CROSS JOIN room_types         rt;
