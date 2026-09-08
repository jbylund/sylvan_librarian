-- Backfill prefer_score and prefer_score_components for all cards
-- This script recalculates the prefer score for all existing cards based on multiple attributes

-- How heavily each ARTWORK has been reprinted, as a proxy for how canonical it is. Counts
-- only real printing events -- three kinds of row are not:
--
--   non-English  the same printing already counted in its English row. Mark Poole's Birds
--                of Paradise art picked up 4bb (Spanish) and fbb (French) on top of the
--                English 4th Edition printing.
--   memorabilia  World Championship decks, Collectors' Edition and 30th Anniversary: not
--                tournament-legal, not real sets. Four of the eight on that same artwork
--                are BLACK-bordered (30a x2, ced, cei), so a border test alone misses half
--                of them.
--   gold/yellow  any remaining non-standard product Scryfall types as something other than
--                memorabilia.
--
-- Together these took Poole's art from 21 counted printings to 11, against Marcelo
-- Vignali's 12 -- reversing which artwork the site shows for that card.
--
-- White borders are deliberately NOT filtered: 2ed-6ed are white-bordered and perfectly
-- real, and dropping them would penalise exactly the core-set reprints this component
-- should reward.
--
-- Evidence (docs/issues/done/00720-prefer-score-artwork-tuning.md): a 47-card blind swap
-- review returned 11 better, 36 same, 0 worse, and a later 378-card review against
-- production added 2 more with no regressions. This changes only the numerator -- it does
-- not stop such printings being displayed.
--
-- Aggregated once for the whole corpus rather than re-counted per card. As a correlated
-- subquery this was one index lookup per row, and because the planner inlines the CTEs
-- below it ran FOUR times per row -- once each for the target list and the IS DISTINCT
-- FROM guard, in both the component object and the score sum.
WITH artwork_printings AS MATERIALIZED (
    SELECT
        illustration_id,
        card_name,
        COUNT(*) AS printings
    FROM magic.cards
    CROSS JOIN LATERAL JSONB_TO_RECORD(raw_card_blob) AS blob(lang text, set_type text)
    WHERE (
        illustration_id IS NOT NULL AND
        blob.lang = 'en' AND
        COALESCE(blob.set_type, '') <> 'memorabilia' AND
        COALESCE(card_border, '') NOT IN ('gold', 'yellow')
    )
    GROUP BY illustration_id, card_name
),
-- MATERIALIZED on both CTEs is load-bearing, not decoration. Inlined, the whole
-- JSONB_BUILD_OBJECT expression is substituted into the UPDATE's target list AND its
-- IS DISTINCT FROM guard, so every component is computed twice over -- and any subquery
-- inside it twice again.
-- Cards with at least one ON-STYLE printing. The representative pin below yields to `art_style`
-- only when there is somewhere else to go: a card printed solely in licensed sets keeps its label
-- rather than losing its representative entirely.
on_style_cards AS MATERIALIZED (
    SELECT DISTINCT oracle_id
    FROM magic.cards
    WHERE NOT (
        (
            card_art_tags ? 'external-ip'
            AND NOT (card_art_tags ?| ARRAY['dungeons-and-dragons', 'the-lord-of-the-rings'])
        ) OR card_art_tags ?| ARRAY['anime', 'comic-style', 'line-art', 'word-art-title']
    )
),
computed_components AS MATERIALIZED (
    SELECT
        source.scryfall_id,
        JSONB_BUILD_OBJECT(
            -- See the artwork_printings CTE above for what counts as a printing.
            'illustration_count', ROUND((23 * LN(1 + COALESCE(artwork_printings.printings, 0)) / LN(40))::numeric, 4),
            'rarity', (
                CASE
                    WHEN card_rarity_int = 0 THEN 16  -- common
                    WHEN card_rarity_int = 1 THEN 16  -- uncommon
                    WHEN card_rarity_int = 2 THEN 11  -- rare
                    WHEN card_rarity_int = 3 THEN 0   -- mythic
                    ELSE 0
                END
            ),
            'border', (
                CASE
                    WHEN card_border = 'black' THEN 14
                    WHEN card_border = 'white' THEN 0
                    WHEN card_border = 'borderless' THEN 0
                    ELSE 0
                END
            ),
            'frame', (
                CASE
                    WHEN card_frame_data ? '2015' THEN 42
                    WHEN card_frame_data ? '2003' THEN 30
                    WHEN card_frame_data ? '1997' THEN 25
                    WHEN card_frame_data ? '1993' THEN 10
                    ELSE 0
                END
            ),
            -- Extended art is a VARIANT of a printing, not the printing most people picture, so it
            -- is scored below the base version rather than above it. This weight used to be +12,
            -- which is the single largest disagreement between this score and Scryfall's own
            -- choice of representative printing.
            --
            -- Evidence, and the method is new here so it is worth stating: Scryfall's `oracle_cards`
            -- bulk file contains exactly one card object per oracle_id, and that object IS its
            -- chosen representative. That is 31,724 labelled preferences for this corpus, free and
            -- external, against the 1,070 that ten human grading sessions produced for #720 — and
            -- it is directly the "score every candidate against every preference" shape #771 asks
            -- for, since a stored preference is reusable across candidates.
            --
            -- Measured on a 31,724-card corpus, agreement with Scryfall's representative:
            --
            --     extended_art = +12 (before)   66.2%
            --     extended_art =   0            70.4%
            --     extended_art =  -3            73.7%
            --     extended_art =  -6            73.9%   <- the knee; -9 and -12 buy nothing further
            --
            -- Held out: the corpus was split 70/30 on a hash of oracle_id and the 30% was never
            -- fitted against (a #771 guard). It tracks the fit set within 0.2 points at every value
            -- above, so this is not overfitting: 66.4% -> 74.0% on data the weight never saw.
            --
            -- Mechanism, isolated rather than inferred: of the 3,079 disagreements where both
            -- printings come from the SAME set, Scryfall picks the LOWER collector number 91.9% of
            -- the time, and 2,800 of those differ by exactly this flag — ours carries `Extendedart`
            -- and theirs does not. Extended-art variants sit at high collector numbers, so +12 was
            -- systematically lifting the variant over the base printing.
            --
            -- -6 rather than -12: the curve is flat past the knee, so this is the smallest value
            -- that captures the whole gain.
            --
            -- Caveat worth stating plainly: this optimises agreement with SCRYFALL's notion of a
            -- representative printing, which is a proxy for "the version people picture" and not
            -- the same objective as a blind aesthetic review. It is proposed on the strength of
            -- this being one of the few weights in this table with no recorded evidence at all —
            -- `illustration_count` and `art_style` both cite #720's review counts; this one cited
            -- nothing. A blind batch on the cards it moves would settle the sign for good.
            'extended_art', (
                CASE
                    WHEN card_frame_data ? 'Extendedart' THEN -6
                    ELSE 0
                END
            ),
            'highres_scan', (
                CASE
                    WHEN blob.image_status = 'highres_scan' THEN 16
                    ELSE 0
                END
            ),
            'has_paper', (
                CASE
                    WHEN blob.games ? 'paper' THEN 6
                    ELSE 0
                END
            ),
            'language', (
                CASE
                    WHEN blob.lang = 'en' THEN 40
                    ELSE 0
                END
            ),
            'legendary_frame', (
                CASE
                    WHEN blob.frame_effects ? 'legendary' THEN 5
                    ELSE 0
                END
            ),
            'non_showcase', (
                CASE
                    WHEN NOT (COALESCE(blob.frame_effects, '[]'::jsonb) ? 'showcase') THEN 10
                    ELSE 0
                END
            ),
            'finish', (
                CASE
                    WHEN blob.finishes ? 'nonfoil' THEN 10
                    WHEN blob.finishes ? 'foil' THEN 5
                    WHEN blob.finishes ? 'etched' THEN 0
                    ELSE 0
                END
            ),
            'artwork_set', (
                CASE
                    WHEN card_set_code IS NULL OR card_set_code NOT IN ('dbl') THEN 20
                    ELSE 0
                END
            ),
            -- Bonus for artwork in Magic's core style, i.e. NOT a licensed crossover and
            -- not a stylistic departure. Written as a bonus for being on-style rather than
            -- a penalty for being off-style so every component stays non-negative, like the
            -- rest of this table.
            --
            -- `external-ip` is the Scryfall tagger's parent tag over ~57 licensed
            -- franchises (Fallout, Warhammer, Marvel, Doctor Who, Fortnite, ...).
            -- `dungeons-and-dragons` and `the-lord-of-the-rings` are deliberately exempt:
            -- external IP whose art matches Magic's high-fantasy look. Verified complete --
            -- no artwork carries a sibling tag (arda, hobbit, abeir-toril, dnd-multiverse)
            -- without also carrying its parent, so no Middle-earth or Forgotten Realms art
            -- is demoted by accident.
            --
            -- The second clause covers stylistic departures that are not licensed universes:
            -- anime, comic-style, line-art and word-art-title. Note it applies even to the
            -- exempt IPs -- Drizzt Do'Urden (afr #338) is tagged dungeons-and-dragons AND
            -- line-art, and is demoted, because a line-art rendering is a departure from the
            -- painted core style whoever owns the IP. Confirmed against the artwork.
            --
            -- Evidence (docs/issues/done/00720-prefer-score-artwork-tuning.md): in 177 labelled
            -- artwork comparisons where exactly one side was off-style, the on-style side
            -- was chosen 177 times. Blind swap review across weights 6, 9 and 14 gave 78
            -- "better" and 0 "worse" over 114 changed cards. Fixes both cards that opened
            -- the issue -- Puresteel Paladin was showing its Fallout printing and Sword of
            -- the Animist its Marvel one.
            --
            -- A year/era term was the obvious alternative and was rejected: it would
            -- permanently penalise all future art, whereas a tag on the artwork generalises.
            -- Scryfall's own representative choice, as a PIN rather than another weight: where a
            -- label exists it decides, and every component above ranks the printings underneath it.
            --
            -- Why a label at all, when this table exists to make that judgement: because it is a
            -- vastly larger evidence base for the same question. `oracle_cards` is one card object
            -- per oracle_id and that object IS the printing Scryfall shows, so it is ~38k
            -- preferences against the 1,070 that ten grading sessions produced for #720. Measured
            -- on a 31,724-card corpus, agreement with it goes 73.9% -> 96.6%; the ceiling is the
            -- 3.4% of labels naming a printing that corpus does not carry.
            --
            -- THE VETO IS THE INTERESTING PART, and it is why this is not simply "defer to
            -- Scryfall". Its dump optimises "most up-to-date recognizable version" -- measured
            -- median ~4 months newer than this score's pick -- and newer increasingly means
            -- licensed crossovers. On 213 cards it names an OFF-STYLE printing while an on-style
            -- one exists: it would show Marvel Super Heroes Commander art for Birds of Paradise,
            -- Harmonize, Shock and Skullclamp, which is exactly what `art_style` was built to
            -- prevent on 177 of 177 labelled comparisons. So the pin yields to `art_style` in that
            -- case and nowhere else, costing ~0.7 points of agreement to keep #720's result.
            --
            -- LEFT JOIN, so an empty table scores every card exactly as before: the extra dump is
            -- an optional input, not a hard dependency.
            'representative', (
                CASE
                    WHEN scryfall_representatives.scryfall_id IS NULL THEN 0
                    WHEN (
                        (
                            card_art_tags ? 'external-ip'
                            AND NOT (card_art_tags ?| ARRAY['dungeons-and-dragons', 'the-lord-of-the-rings'])
                        ) OR card_art_tags ?| ARRAY['anime', 'comic-style', 'line-art', 'word-art-title']
                    ) AND on_style_cards.oracle_id IS NOT NULL THEN 0
                    ELSE 1000
                END
            ),
            'art_style', (
                CASE
                    WHEN (
                        card_art_tags ? 'external-ip'
                        AND NOT (card_art_tags ?| ARRAY['dungeons-and-dragons', 'the-lord-of-the-rings'])
                    ) OR card_art_tags ?| ARRAY['anime', 'comic-style', 'line-art', 'word-art-title'] THEN 0
                    ELSE 14
                END
            )
        ) AS new_components
    FROM magic.cards source
    -- Pull every raw_card_blob field in one pass. Written as separate `->>` extractions,
    -- each one detoasts the blob again -- seven times per row, and raw_card_blob is large
    -- enough to be stored out of line.
    CROSS JOIN LATERAL JSONB_TO_RECORD(source.raw_card_blob) AS blob(
        image_status text, lang text, games jsonb, frame_effects jsonb, finishes jsonb
    )
    LEFT JOIN magic.scryfall_representatives ON (
        scryfall_representatives.scryfall_id = source.scryfall_id
    )
    LEFT JOIN on_style_cards ON (on_style_cards.oracle_id = source.oracle_id)
    LEFT JOIN artwork_printings ON (
        artwork_printings.illustration_id = source.illustration_id AND
        artwork_printings.card_name = source.card_name
    )
),
computed_scores AS MATERIALIZED (
    SELECT
        scryfall_id,
        new_components,
        (
            SELECT
                SUM(value::numeric)
            FROM
                jsonb_each(new_components)
        )::real AS new_score
    FROM computed_components
)
UPDATE magic.cards
SET
    prefer_score_components = computed_scores.new_components,
    prefer_score = computed_scores.new_score
FROM computed_scores
WHERE magic.cards.scryfall_id = computed_scores.scryfall_id
  AND (
      magic.cards.prefer_score_components IS DISTINCT FROM computed_scores.new_components
      OR magic.cards.prefer_score IS DISTINCT FROM computed_scores.new_score
  );
