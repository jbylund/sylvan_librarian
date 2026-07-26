# Tier labelling: sort a card's artworks into bands instead of picking one

The grid labeller asks which of a card's distinct artworks is best. That discards most of what the
grader knows. Preferences arrive in bands — "these two are clearly best, these three are fine, these
eight I dislike" — and a single pick collapses all of it to one item.

Feeds the corpus described in
[local-prefer-score-preference-corpus.md](local-prefer-score-preference-corpus.md).

## Why it is worth rebuilding the UI for

For a card with ten distinct artworks sorted into bands of 2 / 3 / 4 / 1:

| labelling mode | observations from one card |
| --- | --- |
| pick best of 10 | 9 directional, every one sharing the chosen item |
| bands of 2 / 3 / 4 / 1 | **35 directional + 10 ties** — the complete pairwise matrix |

Roughly four times the information, for arguably less effort than deliberating over a single winner.
Cross-band pairs are preferences, within-band pairs are ties.

Ties are the part that matters most. They are currently only produced by swap review, never by the grid,
which is why the 481 collected so far all come from "no real difference" on a proposal. Rao-Kupper and
Davidson cannot be fitted without them, and a parameter set that puts a wide gap between two artworks
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

- **Art crops, not whole cards.** The judgement is about the picture, so `shown_as = art_crop`. This is
  the one task where a crop is correct rather than misleading — the opposite of swap review, where a
  crop hid the frame difference that was the entire point.
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

## What a submission records

Each submission emits, for that card:

- every cross-band pair, as a directional preference, higher band wins
- every within-band pair, as a tie
- nothing at all for pairs involving an unassigned artwork

Band *identity* is deliberately not stored as a rating. "Top band" on a card with ten strong artworks
is not the same absolute standard as on a card with two, so bands are only ever a source of within-card
pairs. Storing them as a global 1–4 score would make those incomparable numbers look comparable.

## Fitting

Bands are an ordinal response, so the direct treatment is an **ordered logit** (proportional odds), or
the **graded response model** from item response theory. Prefer expanding to pairs and reusing the
Bradley-Terry-with-ties estimator already described in the corpus doc: it avoids a second estimator, and
it keeps grid submissions, tier submissions and swap verdicts in one comparable pool.

Within-band pairs should not be weighted as heavily as repeat-confirmed ties. A band groups artworks the
grader did not trouble to separate, which is weaker evidence than two printings independently judged
indistinguishable when shown alone.

## Cost

This obsoletes `gen_labeller.py`'s grid page — the tier page is a rewrite, not an addition, and drag and
drop with a keyboard fallback is meaningfully more UI than a row of images and a keypress. The 202
existing grid picks stay valid: a pick is a band of one against a band of everything else, so they
convert without loss.

## Related

- [local-prefer-score-preference-corpus.md](local-prefer-score-preference-corpus.md) — storage,
  offline evaluation, batch selection.
- [local-prefer-score-label-harness.md](local-prefer-score-label-harness.md) — the grid labeller this
  replaces.
- [00720-prefer-score-artwork-tuning.md](00720-prefer-score-artwork-tuning.md) — the tuning that
  produced the existing labels.
