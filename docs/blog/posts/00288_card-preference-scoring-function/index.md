---
title: "Scryfall Sorts Alphabetically. I Sort by CubeCobra Popularity."
date: 2026-10-24
publishDate: 2026-10-24
tags: ["postgres", "sql", "scoring"]
summary: "Search results need two distinct scoring concerns: which printing of a card to surface, and which cards to rank first. Both are numeric ORDER BY expressions baked into the card rows."
---

Lightning Bolt has been printed more than 30 times.
When you search for it, the database contains 30+ rows.
Which one should appear at the top?

That question has two parts that look similar but are genuinely independent.
The first is a *printing* question: among all the Lightning Bolt rows, which edition is the most canonical?
The second is a *relevance* question: among all the distinct cards matching your query, which ones belong at the top?
Both get answered with `ORDER BY`, but on different columns computed from different signals.

## Picking One Printing per Card

When you search with `unique=card`, the query uses `DISTINCT ON (oracle_id)`.
PostgreSQL needs a tiebreak sort to decide which row wins for each `oracle_id` group.
Before the scoring system existed, that tiebreak was just `edhrec_rank ASC` — whichever printing happened to have the best EDHREC rank won.
That surfaced the right card, but not necessarily the right version of it.

The problem is legible with any heavily-reprinted card.
Lightning Bolt from the `dbl` set uses black-and-white artwork.
A German-language printing from an older set has a 1997 frame.
The EDHREC rank on those printings is often identical to the M21 English full-art version — EDHREC ranks are per card name, not per printing — so the tiebreak was arbitrary.
Whichever version happened to sort first alphabetically by `scryfall_id` could win.

The fix: a pre-computed `prefer_score` column.
To see the effect concretely, consider three Lightning Bolt printings under the current scoring:

| Printing | frame | language | artwork_set | border | finish | … | prefer_score |
|----------|-------|----------|-------------|--------|--------|---|--------------|
| M21 (English, 2015 frame, nonfoil) | 42 | 40 | 20 | 14 | 10 | … | ~168 |
| Alpha (English, 1993 frame, nonfoil) | 10 | 40 | 20 | 14 | 10 | … | ~136 |
| dbl (black/white art) | 42 | 40 | 0 | 14 | 10 | … | ~148 |

The M21 printing wins.
Alpha loses 32 points on frame age.
The `dbl` printing loses 20 points because its set uses black-and-white artwork.
Before `prefer_score` existed, any of these three could have appeared, depending on which happened to have the lowest `edhrec_rank` for that import cycle.
Every row in `magic.cards` carries a numeric score summed from twelve independently-weighted components:

```sql
-- from api/sql/backfill_prefer_scores.sql
'frame', (
    CASE
        WHEN card_frame_data ? '2015' THEN 42
        WHEN card_frame_data ? '2003' THEN 30
        WHEN card_frame_data ? '1997' THEN 25
        WHEN card_frame_data ? '1993' THEN 10
        ELSE 0
    END
),
'language', (
    CASE WHEN raw_card_blob ->> 'lang' = 'en' THEN 40 ELSE 0 END
),
'non_showcase', (
    CASE
        WHEN NOT (COALESCE(raw_card_blob -> 'frame_effects', '[]'::jsonb) ? 'showcase')
        THEN 10 ELSE 0
    END
),
...
```

The full set of components, with their max values:

| Component | Max pts | Signal |
|-----------|---------|--------|
| frame | 42 | 2015 > 2003 > 1997 > 1993 |
| language | 40 | English only |
| artwork_set | 20 | full-color sets; `dbl` (black/white art) scores 0 |
| border | 14 | black border |
| finish | 10 | nonfoil > foil > etched |
| non_showcase | 10 | standard frame treatment |
| extended_art | 12 | extended art frame |
| highres_scan | 16 | `image_status = 'highres_scan'` |
| rarity | 16 | common = uncommon > rare > mythic (prefer the accessible printing) |
| has_paper | 6 | available in paper |
| legendary_frame | 5 | legendary frame effect |
| illustration_count | ~23 | logarithmic: 23 × ln(1 + count) / ln(40) |

The `illustration_count` component is the only non-categorical one.
An illustration that has been reused across 40 printings of the same card name scores the full 23 points; an illustration used only once scores near zero.
The formula is logarithmic rather than linear because the preference saturates quickly: the jump from 1 reuse to 5 matters a great deal (iconic vs. obscure), while the jump from 35 to 40 reuses is almost meaningless.
`ln(40) ≈ 3.69` is the chosen saturation point — roughly the maximum number of printings any single illustration appears on.

The `?` operator throughout is PostgreSQL's JSONB array membership test.
`raw_card_blob -> 'frame_effects' ? 'showcase'` returns true if the `frame_effects` JSON array contains the string `"showcase"`.
No parsing, no `LIKE`, no unnesting — one operator, and PostgreSQL's JSONB index handles the rest.

These twelve numbers sum into a single `prefer_score` column that gets recomputed whenever the Scryfall bulk data is reimported.
The `DISTINCT ON` query then has an unambiguous tiebreak:

```sql
SELECT DISTINCT ON (oracle_id)
    ...
    edhrec_rank AS sort_value
FROM magic.cards
WHERE <user filter>
ORDER BY
    oracle_id,
    prefer_score DESC NULLS LAST
```

The 2015-frame, English, nonfoil, full-color printing of Lightning Bolt wins every time, regardless of which printing EDHREC happens to rank first.

## Ranking Cards by Relevance

Once you have one row per card, you still need to sort those rows.
Alphabetical is the worst possible default for a search engine — it tells you nothing about which cards are worth your attention.

Sylvan Librarian uses EDHREC rank as the default sort signal, with CubeCobra as an alternative.
EDHREC rank is a single integer — lower is more popular — already present in the Scryfall bulk data.
No additional ingestion needed.
It orders a `format:modern` query so that Lightning Bolt, Thoughtseize, and Snapcaster Mage appear before fringe cards with similar type lines.

The CubeCobra option required more work.
CubeCobra exposes a paginated API at `/tool/api/topcards/` with ELO, cube count, and pick count per card.
The ingest pipeline fetches all pages, filters to oracle IDs already in the database, and bulk-updates three raw columns.
The score itself is computed with `PERCENT_RANK()` window functions across all four dimensions, then combined with equal weights (each set to 25 out of 100 after normalization) and scaled to 0–100 (lower is better, matching `edhrec_rank` convention):

```sql
-- from api/sql/backfill_cubecobra_scores.sql
-- weights are normalized so they sum to 100; default is 25 each
25 * PERCENT_RANK() OVER (ORDER BY edhrec_rank          ASC  NULLS LAST)
+ 25 * PERCENT_RANK() OVER (ORDER BY cubecobra_elo        DESC NULLS LAST)
+ 25 * PERCENT_RANK() OVER (ORDER BY cubecobra_cube_count DESC NULLS LAST)
+ 25 * PERCENT_RANK() OVER (ORDER BY cubecobra_pick_count DESC NULLS LAST)
```

One score per distinct `card_name` is computed and propagated to all printings.
A card missing from CubeCobra scores worst on those dimensions via `NULLS LAST` — it does not get a free pass.

Both scores are stored as columns.
The `ORDER BY` in the outer query just picks the right one:

```python
sql_orderby: str = {
    CardOrdering.EDHREC:    "edhrec_rank",
    CardOrdering.CUBECOBRA: "cubecobra_score",
    CardOrdering.CMC:       "cmc",
    CardOrdering.USD:       "price_usd",
    ...
}.get(orderby, "edhrec_rank")
```

The final SQL shape composes both scores in a single query:

```sql
WITH distinct_cards AS (
    SELECT DISTINCT ON (oracle_id)
        ...,
        edhrec_rank AS sort_value   -- card-level relevance signal
    FROM magic.cards
    WHERE <user filter>
    ORDER BY
        oracle_id,
        prefer_score DESC NULLS LAST  -- printing-level preference
)
SELECT ...
FROM distinct_cards
ORDER BY
    sort_value ASC NULLS LAST,    -- card ranking
    edhrec_rank ASC NULLS LAST,   -- tiebreak
    prefer_score DESC NULLS LAST  -- printing preference as last resort
LIMIT %(limit)s
```

The inner `ORDER BY` determines *which* printing appears.
The outer `ORDER BY` determines *where* that card appears in the list.
The two concerns share a query but operate on independent columns.

## The Assumption Baked In

`prefer_score` encodes aesthetic preferences: modern frame, black border, English, nonfoil.
These are reasonable defaults for tournament players and deck builders, but not universal.
A player specifically seeking old-bordered cards to complete a vintage aesthetic would rather have the 1993-frame version score highest.
A debug endpoint at `/prefer_score_tuner` lets you load any card by name, see how each of the twelve components scores each printing, and slide the weights to experiment — but those weights are global to the server, not stored per user.
The current system produces one canonical preferred printing per card, not one per user.

The CubeCobra signal has a separate limitation: it reflects the preferences of players who build cubes, which skews toward powerful Constructed cards.
Cards strong in Commander but weak in cube (Consecrated Sphinx, for example) may not rank as well under CubeCobra ordering as their EDHREC rank would suggest.
Whether that matters depends on what you are searching for.

There is also the staleness question.
`prefer_score` is recomputed on every Scryfall import.
CubeCobra scores are recomputed on demand via `POST /ingest_cubecobra`, separately from the card data.
A card that spiked in CubeCobra popularity after the last ingest will rank lower than it should until the next run.

The scores are right for most searches most of the time.
When they are wrong, the user can change the sort dropdown — and because the two concerns are in separate columns, a bad printing preference and a bad card ranking can each be wrong independently, and fixed independently, without touching the other.

---

[Prefer score SQL](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/sql/backfill_prefer_scores.sql) · [CubeCobra score SQL](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/sql/backfill_cubecobra_scores.sql) · [Search query assembly](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/api_resource.py#L1290-L1323) · [PR #235](https://github.com/jbylund/sylvan_librarian/pull/235) · [PR #243](https://github.com/jbylund/sylvan_librarian/pull/243) · [PR #448](https://github.com/jbylund/sylvan_librarian/pull/448)
