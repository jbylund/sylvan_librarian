# 3-Key Ordering Parity Across Plans

[#707](https://github.com/jbylund/sylvan_librarian/issues/707) — deferred from
[#706](https://github.com/jbylund/sylvan_librarian/pull/706) /
[00702](./00702-engine-plan-selection-layer.md).

The plan-selection layer enforces **2-key** ordering parity (primary sort
column → `edhrec_rank`). Key 3 (`prefer_score`) is deliberately not enforced
across plans. This is the note for if/when we decide to close that gap.

## The divergence

`build_sort_permutations` (lib.rs) bakes the **default** representative
printing's `prefer_score` into the permutation's 3rd sort key (the perm comment
says as much: "the first (store-preferred) printing's default prefer score").
Under a **non-default prefer** the query-chosen representative differs, so:

- **gathered path** (`sort_key_bits`) reads the *chosen* printing's
  `prefer_score` — matches SQL, whose `DISTINCT ON` representative supplies it;
- **perm-based plans** (StreamedSelect / PlanePopcountOrder / PrintingRangeScan)
  inherit the *default* representative's — so a key-3 tie can order differently.

Pre-existing; the force-plan differential test surfaced it. SQL's `ORDER BY` is
only three terms then arbitrary, so parity past key 3 is undefined regardless.

## Why deferred

Manifests only under *all* of: non-default prefer ∧ card-level sort column ∧ a
`(primary, edhrec_rank)` tie (edhrec is ~unique per card → rare) ∧ a
broad/streamed query. Vanishingly rare, never observed in results.

## Options (measure before choosing)

**Step 0 — quantify.** Instrument how often the divergence actually occurs:
non-default-prefer broad queries landing on a keys-1-2 tie. If ~never, leave as
wontfix.

**(A) Gate perm-based plans on default prefer.** Non-default prefer routes to
the gathered path (correct key 3). Cheap: one applicability condition. Cost: the
streaming / popcount / range fast-paths are lost for *all* non-default-prefer
broad queries — a real perf hit on that slice to fix a rarely-visible tie.

**(F) Prefer-independent key 3.** Define the 3rd sort key as the card's
canonical/default `prefer_score`, regardless of the query's prefer, in both
`sort_key_bits` and SQL's `ORDER BY`. Perf-neutral — keeps the perm and every
fast-path. Cost: a cross-cutting SQL + Rust change, plus a product-semantics
decision — card order becomes stable regardless of which printing `prefer`
surfaces (arguably cleaner than order shifting with prefer, but a behavior
change to confirm).

## Closing it out

Whichever option, the differential test (`force_plan_differential_agreement`)
upgrades from 2-key to 3-key value-sequence comparison (drop the `>> 32` /
compare the low 32 bits too), exercised under non-default prefer — which is
exactly the assertion that currently would fail and is why 3-key isn't enforced
today.
