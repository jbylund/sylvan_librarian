# Tier labelling: sort a card's printings into bands instead of picking one

The grid labeller asks which of a card's distinct artworks is best. That discards most of what the
grader knows, in two ways: preferences arrive in bands — "these two are clearly best, these three are
fine, these eight I dislike" — and a single pick collapses all of it to one item; and by showing one
tile per artwork it can only ever teach the score about artwork, never about frame, border or finish.

Feeds the corpus described in
[local-prefer-score-preference-corpus.md](local-prefer-score-preference-corpus.md).

## Why it is worth rebuilding the UI for

For a card with ten printings sorted into bands of 2 / 3 / 4 / 1:

| labelling mode | observations from one card |
| --- | --- |
| pick best of 10 | 9 directional, every one sharing the chosen item |
| bands of 2 / 3 / 4 / 1 | **35 directional + 10 ties** — the complete pairwise matrix |

Roughly four times the information, for arguably less effort than deliberating over a single winner.
Cross-band pairs are preferences, within-band pairs are ties.

Ties are the part that matters most. They are currently only produced by swap review, never by the grid,
which is why the 481 collected so far all come from "no real difference" on a proposal. Rao-Kupper and
Davidson cannot be fitted without them, and a parameter set that puts a wide gap between two printings
the grader considers equivalent is overconfident in a way nothing presently measures.

## Interaction

Drag and drop, with a keyboard path for speed.

```
  Wall of Wood                                            card 12 / 30
  ┌──────────────────────────────────────────────────────────────────┐
  │  unassigned                                                      │
  │   ┌────┐ ┌────┐ ┌────┐                                           │
  │   │ 4  │ │ 7  │ │ 9  │                                           │
  │   └────┘ └────┘ └────┘                                           │
  └──────────────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────────────┐
  │ 1  love it        ┌────┐ ┌────┐                                  │
  │                   │ 2  │ │ 5  │                                  │
  ├───────────────────┴────┴─┴────┴──────────────────────────────────┤
  │ 2  good           ┌────┐ ┌────┐ ┌────┐                           │
  │                   │ 1  │ │ 6  │ │10  │                           │
  ├───────────────────┴────┴─┴────┴─┴────┴───────────────────────────┤
  │ 3  fine           ┌────┐                                         │
  │                   │ 3  │                                         │
  ├───────────────────┴────┴─────────────────────────────────────────┤
  │ 4  dislike        ┌────┐                                         │
  │                   │ 8  │                                         │
  └──────────────────────────────────────────────────────────────────┘
       [ submit (enter) ]   [ skip card (s) ]   [ undo (u) ]
```

- **Whole cards, not art crops.** `shown_as = whole_card`. The score decides which *printing* to
  display, and it has components for frame, border, finish, extended art and scan quality as well as
  artwork. Labels that show only the picture cannot identify any of those coefficients — which is
  precisely why #720 got frame-versus-finish wrong three times. See below.
- **Drag to place, number keys to go fast.** Dragging thirteen thumbnails is a lot of mouse travel;
  pressing `1`–`4` assigns the focused thumbnail and advances focus, so a card can be graded without
  the pointer. Drag is for deliberate revision, keys for throughput. Support both.
- **Start everything unassigned.** Pre-seeding a band biases toward it, and an untouched card must be
  distinguishable from one deliberately graded as uniform.
- **Unassigned items are dropped, not tied.** Leaving three thumbnails in the pool means "no opinion
  recorded", and the pairs involving them are simply not emitted. The submit button should say how many
  will be skipped.
- **Explicit submit.** Bands get revised as the grader looks across the card; auto-committing on drop
  would record intermediate states.
- **Fixed four bands.** A free-form count would drift between sessions and make older labels harder to
  interpret. Four is enough to express the structure described above.

## Whole printings are what makes the other components learnable

The grid labeller showed one representative per `illustration_id`, so every label it produced was about
artwork. `frame`, `border`, `finish`, `extended_art` and `highres_scan` never varied within a comparison,
which makes their coefficients unidentifiable from that data no matter how much of it is collected.

That is the root cause of the worst mistake in #720. `finish_foil` was raised on the strength of 100
better / 5 worse, and the sign turned out to be backwards: in all 62 informative pairs the foil printing
also carried a newer frame, so the two components were perfectly confounded and the fit credited the
wrong one. It took a deliberately controlled batch — same card, set, artwork, frame, border, rarity, scan
and promo type, differing only in finish — to find that nonfoil actually wins 24–0.

Showing whole printings fixes this by construction. When the same artwork appears twice in different
frames and the grader puts them in different bands, *that is the frame signal*, measured directly rather
than inferred from a swap.

### Which printings to show

A heavily reprinted card has more printings than anyone will tier, so the set has to be sampled — and
the sampling is where identifiability is won or lost. Choose 8–12 printings per card to **span the
component space**: different frame eras, both borders, foil and nonfoil, extended and normal, more than
one artwork. Deliberately include same-artwork/different-frame and same-frame/different-artwork pairs
so each component varies against a held background.

The formal version is experimental design — maximise the conditioning of the feature matrix (a
D-optimal criterion) rather than sampling printings uniformly. Uniform sampling reproduces the corpus's
natural correlations, which is how frame and finish became inseparable in the first place.

Expect this to feel repetitive: several tiles will be the same picture. That is intended, and it is the
opposite of the earlier complaint about seeing duplicate artwork in the grid labeller. There, repeated
art was noise because the task was to choose a picture; here the difference between two printings of one
picture is the entire measurement.

## What a submission records

Each submission emits, for that card:

- every cross-band pair, as a directional preference, higher band wins
- every within-band pair, as a tie
- nothing at all for pairs involving an unassigned printing

Band *identity* is deliberately not stored as a rating. "Top band" on a card with ten strong printings
is not the same absolute standard as on a card with two, so bands are only ever a source of within-card
pairs. Storing them as a global 1–4 score would make those incomparable numbers look comparable.

## Fitting

Bands are an ordinal response, so the direct treatment is an **ordered logit** (proportional odds), or
the **graded response model** from item response theory. Prefer expanding to pairs and reusing the
Bradley-Terry-with-ties estimator already described in the corpus doc: it avoids a second estimator, and
it keeps grid submissions, tier submissions and swap verdicts in one comparable pool.

Within-band pairs should not be weighted as heavily as repeat-confirmed ties. A band groups printings the
grader did not trouble to separate, which is weaker evidence than two printings independently judged
indistinguishable when shown alone.

## Cost

This obsoletes `gen_labeller.py`'s grid page — the tier page is a rewrite, not an addition, and drag and
drop with a keyboard fallback is meaningfully more UI than a row of images and a keypress. Sampling
printings for conditioning rather than uniformly is additional work again.

The 202 existing grid picks stay valid but stay limited: a pick is a band of one against a band of
everything else, so it converts without loss, and it still says nothing about frame, border or finish.
Those 202 cards are worth regrading as printings once the page exists.

## Related

- [local-prefer-score-preference-corpus.md](local-prefer-score-preference-corpus.md) — storage,
  offline evaluation, batch selection.
- [local-prefer-score-label-harness.md](local-prefer-score-label-harness.md) — the grid labeller this
  replaces.
- [00720-prefer-score-artwork-tuning.md](00720-prefer-score-artwork-tuning.md) — the tuning that
  produced the existing labels.
