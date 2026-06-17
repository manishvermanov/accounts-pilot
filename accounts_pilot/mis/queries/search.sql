-- Lightweight hotel search for the dashboard's search box. READ-ONLY.
-- Matches on property name (and city), returns just what the result list needs.
SELECT
    p.id   AS property_id,
    p.name AS property_name,
    ad.city,
    ad.state
FROM public.property p
LEFT JOIN public.address ad
    ON  ad.entity_id   = p.id
    AND ad.entity_type = 'PROPERTY'
    AND ad.is_deleted  = FALSE
    AND ad.is_active   = TRUE
WHERE p.is_deleted = FALSE
  AND p.is_active  = TRUE
  AND (%(q)s = '' OR p.name ILIKE %(like)s OR ad.city ILIKE %(like)s)
ORDER BY p.name ASC
LIMIT 50;
