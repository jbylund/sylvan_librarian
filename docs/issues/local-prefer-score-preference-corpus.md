# A preference corpus for `prefer_score`, and batches chosen to be informative

The tuning in [#720](00720-prefer-score-artwork-tuning.md) produced **1,070 directional preferences
and 481 ties** across ten grading sessions. Almost none of it is reusable, because each verdict was
collected to answer one specific proposal. This is a design for making the preferences the durable
artifact and the proposals disposable.

Complements [local-prefer-score-tuning-loop.md](local-prefer-score-tuning-loop.md), which covers the
propose/accept control flow. This doc is about the data and how batches get chosen.

## The problem: labels are welded to proposals

A swap review asks "the score currently shows A, this change would show B — which is better?" The
answer is about A and B, but it was *recorded* against a change. When the next candidate moves the same
card to C, the verdict is silent, and there is no way to tell without recomputing. Destination-matching
recovered some of it late in #720, but the accounting was wrong for several rounds before that: a
config was credited with verdicts belonging to a different destination, which made a change with a
backwards sign look like the best-evidenced option in the whole session.

The corpus fixes this by storing what was actually learned. `A > B for card C` is durable. It stays
true regardless of which parameter set surfaced the comparison, so every future candidate can be scored
against every preference ever given.

## The primitive

One record per comparison:

```
card, printing_a, printing_b, verdict ∈ {a, b, tie}, shown_as ∈ {whole_card, art_crop},
batch_id, timestamp, what_varied
```

`what_varied` is computed at generation time — artwork / frame / border / finish / scan / set-type —
not inferred later. Three separate wrong conclusions in #720 came from discovering after the fact that
a batch confounded two properties: 63 of 69 "foil vs nonfoil" pairs also changed frame, and the
6-of-69 that isolated finish said the opposite of the 50 controlled pairs collected afterwards.

`shown_as` matters because verdicts are not comparable across it. An art-crop batch cannot speak to
frame or finish, and one that showed crops for a frame-only change returned "no difference" on
everything and nearly shipped a harmful weight.

## Offline evaluation

With the corpus in place, scoring a parameter set costs no labelling:

> for every directional preference, does the score rank the two printings the same way?

Agreement over the corpus is the objective. It is a dot product per row over levels already extracted
by `prefer_weights.py`, so a candidate is evaluated in milliseconds. Ties are informative separately —
a parameter set that puts a large gap between two printings the grader called indistinguishable is
overconfident, which is exactly the defect behind the 5-point foil penalty.

The existing 202 grid picks give a second, coarser view: agreement with a directly chosen artwork, which
rose from 79.2% to 83.2% with #766. Keep both — the pairwise measure is sensitive, the grid measure is
interpretable.

## Choosing the ~30

A batch should reduce uncertainty, not confirm what is already known. Candidates, cheapest first:

- **Disagreement between parameter sets.** Hold several live candidates and show the pairs they rank
  differently. Every verdict then discriminates rather than re-confirming.
- **Small margins.** Pairs the current score separates by fractions of a point are where it is
  guessing. Birds of Paradise sat at 0.0008 between two artworks.
- **Under-covered regions.** Corpus coverage by frame era, set type and artwork age, with sampling
  toward the thin cells. The controlled foil population was 162 pairs corpus-wide and unrepresented
  until deliberately sought.
- **Repeat a small fraction deliberately.** Roughly 10% of each batch should be pairs already graded,
  unmarked. Agreement means a stable preference the model should be confident about; disagreement means
  the two printings sit below the grader's discrimination threshold, which is a property of the
  comparison rather than noise. This is intra-rater reliability, scored with Cohen's kappa (agreement
  corrected for chance), and the psychophysical analogue is a just-noticeable difference. Pairs that
  flip on re-presentation should be down-weighted in fitting or treated as ties — a model driven to
  separate them is being asked to reproduce a coin flip. Outside that fraction, do not re-ask.

30 is a reasonable sitting: 47 and 89-card batches were fine, 378 was too long to stay attentive
through, and the 20-card batch was mostly cards already answered.

## Fitting, not just accepting

Enough pairwise data makes this a ranking problem rather than a search over hand-set weights. The named
methods, and where each applies:

| method | what it gives us |
| --- | --- |
| **Bradley-Terry with covariates** | strength `exp(β·x)` from component levels, so `P(A>B) = logistic(β·(x_A − x_B))` — plain logistic regression on feature *differences*, and `β` is component contribution. Equivalently **conditional logit** / McFadden choice model; **Thurstone-Mosteller** is the probit-link variant. |
| **Rao-Kupper** or **Davidson** | an explicit indifference threshold, so the 481 ties are modelled rather than discarded |
| **Plackett-Luce** | the grid picks, where one of N artworks is chosen. Expanding a pick into N−1 pairwise rows (what #720 did) overstates the effective sample size, because they share the chosen item |
| **Elo / Glicko / TrueSkill** | sequential updating; Glicko and TrueSkill carry a per-item variance, which is where preference stability naturally lives. Rates items, not features, so it does not give component contributions directly |
| **uncertainty sampling**, **query-by-committee** | which pairs to show next. Query-by-committee is the formal version of "show pairs where live candidates disagree"; **expected information gain** / **BALD** is the information-theoretic form |
| **SPRT** (Wald) | accept/reject on a running batch, in place of fixed count thresholds |
| **Cohen's kappa** | intra-rater agreement on the repeated fraction |

`fit_artwork_score.py` prototyped the first of these with a softplus reparameterisation to keep declared
signs, plus 5-fold CV. It was abandoned because the corpus was too small and confounded — not because
the approach was wrong.

Two cautions carried from #720. Collinear features produce meaningless individual coefficients: illustration
count and artist prominence correlated at 0.986, so their split was noise. And declared monotonic signs
cannot represent a preference with an interior peak, which is what artwork age turned out to be.

## Guards

- **Blinding.** Randomise sides, label them left/right, and never name the proposal. The first three
  batches in #720 labelled them `CURRENT` and `PROPOSED`.
- **Holdout.** Reserve a third of the corpus, never fit against it, report holdout agreement at accept
  time. Three things were fitted against one set of 89 labels, making every p-value optimistic.
- **Coupled parameters.** Declare groups (the frame ladder 1993/1997/2003/2015) and move them together.
  Raising `frame_2003` to widen one gap narrowed another and was rejected 8–22; lowering `frame_1997`
  sent 161 of 179 swaps to the oldest frame.
- **Positional bias.** With randomised sides it is measurable — test against the actual side split, not
  50%. A run reported as significant at p=0.032 was p=0.192 once the null was right.

## Storage: Postgres as the working store, git as the record

Anywhere is better than `~/Downloads`, where ten files share filenames across batches — one session's
verdicts were graded against a contaminated batch and nearly analysed as a clean one because two runs
produced the same download name.

Postgres is the right working store. Votes join directly against `magic.cards`, so feature extraction
and coverage analysis become queries instead of Python that re-reads CSV; a uniqueness constraint on
the printing pair makes re-asking impossible rather than merely discouraged; and the labelling page can
write as it goes, removing the download-and-rename step that lost a session's work.

```sql
CREATE TABLE labels.printing_preference (
    card_name     text    NOT NULL,
    printing_a    uuid    NOT NULL,          -- scryfall_id, ordered so (a,b) is canonical
    printing_b    uuid    NOT NULL,
    verdict       text    NOT NULL CHECK (verdict IN ('a', 'b', 'tie')),
    shown_as      text    NOT NULL CHECK (shown_as IN ('whole_card', 'art_crop')),
    what_varied   text[]  NOT NULL,          -- artwork / frame / border / finish / scan / set_type
    batch_id      text    NOT NULL,
    graded_at     timestamptz NOT NULL DEFAULT now(),
    -- One row per grading, not per pair: repeated presentations are the reliability signal, so
    -- the key admits several verdicts for the same pair and they are aggregated when fitting.
    PRIMARY KEY (printing_a, printing_b, shown_as, graded_at)
);
```

**Not in the `magic` schema.** `2025-09-29-great-reset.sql` opens with
`DROP SCHEMA IF EXISTS magic CASCADE`, and `magic.query_log` sits inside it — the established pattern
for DB-resident data here is expendable telemetry, rebuildable from Scryfall. Preferences are the
opposite: hours of human judgement that cannot be regenerated at any price. A separate `labels` schema
keeps them outside the blast radius.

Even so, the durable record should be a git-tracked export, with the table as the working copy. The
database is rebuildable by design and exists twice (blue and green); the corpus is neither. An export
also makes the evidence in #720 and #766 reproducible by someone who does not have this machine's
containers, which it currently is not.

**Incremental path.** Direct writes need an endpoint, and the review pages open over `file://`, so
that means either serving them from the API or a CORS allowance. Neither is required to start: add an
`import-votes` command that loads a downloaded JSONL into the table idempotently, keyed on the pair.
That captures the 1,070 preferences already collected — which is the pressing part, since they are
currently one `rm` away from gone — and direct writes can follow.

## Related

- [local-prefer-score-tuning-loop.md](local-prefer-score-tuning-loop.md) — propose/accept control flow.
- [00720-prefer-score-artwork-tuning.md](00720-prefer-score-artwork-tuning.md) — the tuning that
  produced the corpus, and the record of what went wrong.
- [local-prefer-score-label-harness.md](local-prefer-score-label-harness.md) — the labelling instrument.
- [`scripts/prefer_weights.py`](../../scripts/prefer_weights.py) — level extraction and review pages.
