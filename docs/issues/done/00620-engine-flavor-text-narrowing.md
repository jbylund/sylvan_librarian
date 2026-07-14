# Engine: flavor-text narrowing via distinct-text scan + CSR

Follow-on to [00605-engine-unindexed-predicates.md](done/00605-engine-unindexed-predicates.md)
(PR #605). Status: written up 2026-07-03, not started. GitHub: #620.

## Problem

Flavor text is the last unindexed *text* field, and it owns the top of the
latency survey's slow tail: bare `ft:` is ~1.4 ms (full per-printing contains
over 52,184 flavored printings), and because an unindexable Or child voids
narrowing for the whole node, Or-combos are the engine's worst measured queries
— `(o:flying or ft:dream)` 2.41 ms, `(ft:fire or frame:showcase)` 2.0 ms.

## Why not a trigram index

Measured from the corpus with the oracle index's dedup-CSR design: 26,314
distinct flavor texts → 2.08 M trigram postings across 17,666 distinct trigrams
≈ **9 MB archived** (~12% of the 74 MB archive) to reach ~0.1–0.2 ms. Flavor is
the least-searched text field; the price/benefit is upside down. (Calibration:
the same arithmetic reproduces the oracle trigram index's known ~15 MB.)

## Proposed: distinct-text scan at bind + CSR (~0.4 MB)

The artist-vocab trick ([PR #605](done/00605-engine-unindexed-predicates.md)) scaled up.
Flavor has only 26.3k distinct texts (2.3 MB payload, already interned as
`flavor_text_lower_id` in the strings table):

1. Build-time: dense-remap the distinct `flavor_text_lower_id`s (first-seen
   order, exactly like `build_oracle_text_index`'s remap) and store a CSR
   `flavor_text_id → [printing ids]` — offsets ~26k × 4 B + ids 52k × 4 B
   ≈ 0.4 MB. No trigrams.
2. Bind-time: evaluate the predicate (contains/exact/regex — one mechanism for
   all three, like ArtistMatch) once over the ~26.3k distinct strings
   (~2.3 MB linear scan, ~0.2–0.4 ms) → matching dense text ids, rewritten into
   a resolved node (`FlavorMatch { text_ids }` mirroring `ArtistMatch`).
3. Match: per printing, compare its dense flavor text id against the resolved
   set (binary search) — needs a per-printing dense flavor id (u16 won't fit
   26.3k → u32, or keep resolving through `flavor_text_lower_id` with a
   bind-built id set keyed on the *global* string id, sorted Vec<u32> —
   membership by binary search, no new Printing field).
4. Narrow: expand matching text ids through the CSR → printing-space
   candidates, making `ft:` Or-combos fully narrowable (the structural win).

Expected: `ft:` ~1.4 → ~0.3–0.5 ms (bind-dominated), worst-case Or-combos
~2.4 → well under 1 ms, for ~0.4 MB.

## Notes

- The bind scan cost is per `ft:` term per query; fine for a rare field. If
  `ft:` ever becomes hot, the trigram index is the escalation path (drop-in:
  same CSR, add the trigrams — the oracle index is the template).
- The same "distinct-value scan at bind + CSR" pattern generalizes to any
  medium-cardinality field (watermark, border, collector number) if one ever
  shows up in traffic; artist (2.2k values) and flavor (26k) bracket the
  practical range.
- Decide during implementation: dedicated dense flavor id on Printing vs
  binding a sorted set of global string ids (option 3b above — zero archive
  change beyond the CSR, slightly slower membership on large match sets).

## Tasks

- [ ] Flavor CSR (dense text ids → printing ids) in CardIndexes
- [ ] Bind-time rewrite of ft: contains/exact/regex into a resolved match node
- [ ] narrow_candidates arm expanding matched text ids → printing candidates
- [ ] Re-run the 452-config survey; acceptance: `(o:flying or ft:dream)` and
      bare `ft:` drop out of the slowest-30

## Related

- [00605-engine-unindexed-predicates.md](done/00605-engine-unindexed-predicates.md) — parent
  ticket; artist vocab is the small-cardinality version of this pattern
- [00603-engine-card-printing-split.md](done/00603-engine-card-printing-split.md) — candidate
  space rules the narrowing arm plugs into
