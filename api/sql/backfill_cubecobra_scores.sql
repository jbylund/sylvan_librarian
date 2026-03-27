-- Backfill cubecobra_score for all cards.
--
-- Score is a weighted average of per-dimension PERCENT_RANK values (0=best, 1=worst),
-- scaled to a 0–100 range where 0 is best and 100 is worst.
-- Weights are passed as named parameters (relative importance only; they are normalized by
-- backfill_cubecobra_scores so that they sum to 100):
--   %(w_edhrec)s, %(w_elo)s, %(w_cube_count)s, %(w_pick_count)s
--
-- Cards with NULL on a dimension are ranked last (worst) for that dimension via NULLS LAST.
-- One score is computed per distinct card_name, then propagated to all printings.

WITH per_card AS (
    SELECT DISTINCT ON (card_name)
        card_name,
        edhrec_rank,
        cubecobra_elo,
        cubecobra_cube_count,
        cubecobra_pick_count
    FROM magic.cards
    ORDER BY card_name
),
scored AS (
    SELECT
        card_name,
        (
            %(w_edhrec)s     * PERCENT_RANK() OVER (ORDER BY edhrec_rank          ASC  NULLS LAST)
          + %(w_elo)s        * PERCENT_RANK() OVER (ORDER BY cubecobra_elo        DESC NULLS LAST)
          + %(w_cube_count)s * PERCENT_RANK() OVER (ORDER BY cubecobra_cube_count DESC NULLS LAST)
          + %(w_pick_count)s * PERCENT_RANK() OVER (ORDER BY cubecobra_pick_count DESC NULLS LAST)
        ) AS cubecobra_score
    FROM per_card
)
UPDATE magic.cards
SET
    cubecobra_score = scored.cubecobra_score
FROM
    scored
WHERE
    magic.cards.card_name = scored.card_name AND
    magic.cards.cubecobra_score IS DISTINCT FROM scored.cubecobra_score
