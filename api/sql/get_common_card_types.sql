WITH card_types AS (
    SELECT
        jsonb_array_elements_text(card_types) as type_name
    FROM
        magic.cards
    WHERE
        card_types IS NOT NULL
),
card_subtypes AS (
    SELECT
        jsonb_array_elements_text(card_subtypes) as subtype_name
    FROM
        magic.cards
    WHERE
        card_subtypes IS NOT NULL
),
card_types_and_subtypes AS (
    SELECT
        type_name
    FROM card_types
    UNION ALL
    SELECT
        subtype_name
    FROM card_subtypes
),
counted AS (
    SELECT
        type_name,
        count(1) as num_occurrences
    FROM card_types_and_subtypes
    GROUP BY type_name
)
SELECT
    type_name AS t,
    num_occurrences AS n
FROM
    counted
ORDER BY
    type_name
