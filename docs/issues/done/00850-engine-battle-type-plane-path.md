# `card_types` Has No `Battle`, and `t:battle` Poisons an `Or`'s Plane Path

**CLOSED — root cause found, and it is not type extraction. Folded into
[#400](https://github.com/jbylund/sylvan_librarian/issues/400) (DFC support).**

The suspicion in (1) was right — it *is* a wrong-answer bug — but the cause is one layer up, and far wider
than battles. **The corpus stores the BACK face of every multi-face card.**

Every battle is a Siege and every Siege is double-faced, so the front face is exactly what is dropped:
`Invasion of Fiora // Marchesa, Resolute Monarch` is stored with `type_line` = `Legendary Creature — Human
Noble`, its back. All 54 `Invasion of …` cards are present under their back sides, `Battle` appears in **zero**
type lines corpus-wide, and `card_types` therefore has no `Battle`. So (1) is not a missing type — it is a
missing *face*.

It is not only the type line. On the same card, `oracle_text`, `creature_power`/`toughness`, `card_colors`
(`{B}` where the cost is `{3}{W}{B}`) and `mana_cost_text` (empty) all come from the back too.

**Blast radius, measured:** 1,102 printings / 461 distinct cards carry the mechanical signature — an empty
`mana_cost_text` against a nonzero card-level `cmc`, which only a non-front face produces. `transform` 969 of
984, `flip` 36 of 36, `meld` 21 of 63, `modal_dfc` 76 of 260. A lower bound, since MDFC backs *do* carry a
cost; the real figure is nearer all 1,343 multi-face printings.

So (2), the `t:battle` `Or` poisoning, should be **re-tested after the face fix rather than diagnosed now**:
`TYPE_BATTLE` is currently an all-zero plane, and an empty plane is a plausible cause of the very behaviour
that section could not explain.

The measured detail and the correction to #400's stated premise — it says DFCs are *filtered out*, and they are
not, which makes this a silent wrong answer rather than a visible gap — are in
[that issue's thread](https://github.com/jbylund/sylvan_librarian/issues/400#issuecomment-5210677523).

**One item here is NOT covered by #400 and is now untracked:** the `is:old` / `is:historic` 6.8× gap below
(7,191 cards in 399 µs against 7,261 in 59 µs, both `Or`s over `card_frame_data`). It rode along on this issue
and has nothing to do with faces. Still only a diagnostic, never explained; file it if it becomes worth
chasing.

*Original status, kept for dating* — proposed, items 2 and 3 of
[the `is:` / `frame:` audit](local-engine-is-frame-predicates.md), whose other findings shipped in
#840 and #842.

## 1. The data question — check this first

Counting `card_types` across the corpus gives 12 distinct names and **`Battle` is not among them**:

    Creature 45,976   Legendary 13,537   Land 11,552   Artifact 10,949   Instant 10,725
    Sorcery 10,626    Enchantment 9,914  Basic 4,196   Planeswalker 1,379  Snow 262
    Kindred 183       World 42

The corpus carries release dates through 2025-02-14 and battles arrived with March of the Machine in 2023,
so they should be present. If the live store looks the same, `t:battle` silently returns nothing and
**`is:permanent` misses every battle** — a wrong answer, not a slow one.

Check the importer's type extraction before treating this as a corpus artifact. It is cheap to check, and
it decides whether (2) is even the right framing: fixing the plane path would still return zero battles.

## 2. The plane path

`is:permanent` desugars to `t:creature or t:artifact or t:enchantment or t:land or t:planeswalker or
t:battle`, and that last disjunct costs **11.5×**:

| query | count source | plan | µs |
| --- | --- | --- | --: |
| 5 disjuncts (through `t:planeswalker`) | plane | PlanePopcountOrder | **55** |
| … `or t:snow` (6 disjuncts) | plane | PlanePopcountOrder | 57 |
| `t:instant or t:sorcery or t:snow or t:world or t:basic or t:kindred` (6) | plane | PlanePopcountOrder | 47 |
| … `or t:battle` (6 disjuncts) | **candidates** | StreamedSelect | **497** |
| `t:battle or t:creature` (2 disjuncts) | **candidates** | StreamedSelect | 90 |
| `is:permanent` | **candidates** | StreamedSelect | **641** |

**Two explanations are ruled out by that table.** It is not the disjunct count — six other-type disjuncts
stay on the plane path. And it is not a missing type plane: `TYPE_PLANES = 14` covers every bit including
`TYPE_BATTLE = 1 << 2`, and `PERMANENT_TYPES` includes it.

**The mechanism is not identified.** `t:battle` alone acquires as `printing_compose` returning 0 rows in
3.5 µs, which is already odd for a `TypeCmp` — that is the thread to pull. Do not guess; two plausible
mechanisms are already eliminated above.

Whatever it turns out to be, the fix is probably algebraic rather than local: an empty or non-compilable
disjunct should be **dropped** from an `Or` (`Or(x, ∅) = x`) rather than poisoning the whole expression.
That generalizes past `Battle` and is the version worth shipping.

## Also carried over: the `is:old` / `is:historic` gap

Recorded here so it is not lost with its parent doc, and it may explain more than it looks like.

`is:old` narrows to **7,191** cards and takes **399 µs**. `is:historic` narrows to **7,261** cards — 0.4%
more — and takes **59 µs**. Same field, same count source, near-identical evaluation domain, **6.8×** apart.
Both are `Or`s over `card_frame_data`.

Nothing in the audit explains it, and whatever makes `is:historic` fast is presumably available to `is:old`.
Diagnosis only; it may share a cause with (2), since both are `Or`s that route differently than their
siblings.

## Traffic caveat

`is:` is absent from `REALISTIC_FAMILY_WEIGHTS`, so the 24%-of-dispatch figure that motivated the parent
audit is a **uniform-mode** number and the realistic share is unmodelled. That caveat sizes (2); it does not
apply to (1), which is a correctness question regardless of how often the query is typed.

## Related

- [done/local-engine-is-frame-predicates.md](local-engine-is-frame-predicates.md) — the parent audit,
  the `frame_data` hybrid that shipped, and the threshold-conflation fix.
- [local-engine-layout-postings.md](../local-engine-layout-postings.md) — item 4 of the same audit,
  deprioritized separately.
- [local-engine-vanilla-face-narrowing.md](../local-engine-vanilla-face-narrowing.md) — `is:vanilla` /
  `is:permanent` from the other direction: a plane-composable `Or` falling back to the general path.
  (Renamed from `local-engine-empty-text-narrowing.md` when `is:vanilla` stopped expanding to a text
  equality — see that doc's opening.)
