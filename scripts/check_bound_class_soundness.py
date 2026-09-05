r"""Standing soundness check for the `And` arm's BOUND-class mechanisms: none may predict below truth.

Round 59's whole thesis is one admission rule -- *a source may claim `SpaceMeasure::guaranteed` only
if its number is a real count of a real set* -- and the property that rule buys is checkable from data
this repo already collects. This is that check, as a script that fails loudly rather than as a table
someone assembled by hand once (which is how Round 59's own audit was produced, and exactly the kind
of evidence that goes stale the moment a new mechanism lands).

    .venv/bin/python scripts/check_bound_class_soundness.py /tmp/after.jsonl
    .venv/bin/python scripts/check_bound_class_soundness.py /tmp/before.jsonl /tmp/after.jsonl

Input is one or more JSONL runs written by `scripts/nway_estimate_truth_survey.py --out`. Exit status
is 0 only if every bound-class candidate in every file is `>= true_total`; any violation, or any
mechanism string this script cannot classify, exits 1.

## What is checked, and against what

Each row's `and_trace.considered` holds one entry per mechanism the `And` arm ATTEMPTED, with that
mechanism's OWN printing-space numbers and whether it produced any (`hit`). Those are the individual
claims the admission rule governs, so those are what this checks -- not the row's final
`predicted_matches`, which is `SpaceMeasure::best()` over every channel and every mechanism at once
and so can sit below truth for reasons that have nothing to do with any bound-class candidate (an
estimate-class mechanism undershooting, or one of the three reprint-ratio leaf arms doing so). Both
views are printed; only the per-candidate one decides the exit status. See `ROW_LEVEL_VIEW_DOC`.

## Round 60: read the engine's own answer, keep the name map as a cross-check

Since Round 60 each trace entry reports the two CHANNELS behind its collapsed `printing`:
`printing_guaranteed` (a proven bound) and `printing_estimate` (a guess). So "is this candidate
bound-class" stopped being a question this script has to answer from a name -- a candidate is
bound-class exactly when it populated `guaranteed`, which is the admission rule stated directly. The
checked number is `printing_guaranteed` itself, not the `best()`-collapsed `printing`: the invariant
is about what was CLAIMED as a bound.

`BOUND_CLASS_MECHANISMS`/`ESTIMATE_CLASS_MECHANISMS` are kept, demoted from classifier to
CROSS-CHECK: every hit is classified both ways and any disagreement is a hard failure. That turns
what used to be a maintenance hazard (a new mechanism silently misclassified, or worse, classified
correctly here while writing the wrong channel in Rust) into a consistency test between the two.
Older runs, written before Round 60, have no channel keys at all; they are handled by falling back to
the name map, with the fallback reported so a comparison against a pre-Round-60 baseline is not
silently weaker.

## The confound this respects: printing space only

`unique=card`/`unique=artwork` rows are skipped, and this is not a convenience. In those spaces
`predicted_matches` is a DERIVED figure -- the printing estimate pushed through occupancy plus
`COMPOSE_CARD_ESTIMATE_BIAS` -- so the printing->card derivation's own bias dominates any mechanism
defect, and a "violation" there says nothing about the mechanism. The trace's own `printing` numbers
are printing-space regardless of the query's `unique`, but `true_total` is not: it is the count in
that row's own space. So a card-space row has no printing-space truth to compare against at all, and
including it would be comparing two different quantities. That is the R57/R58 story, and it is why
Round 59's own audit was printing-space only.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

# ── mechanism classification (cross-check only, since Round 60) ────────────────────────────────
# Deliberately this script's OWN list rather than a mirror of `is_estimate_class_mechanism` in
# lib.rs. That predicate does NOT answer "does this mechanism contribute a bound": it classifies a
# mechanism by whether it can offer a same-set (printing, card, artwork) triple, for trace-attribution
# priority. `LegalityDateTotals` is a member of it AND contributes a bound; `PriceJointTable` is not a
# member and also contributes a bound. Mirroring it would silently mis-scope this check.
#
# Round 60 demoted these from CLASSIFIER to CROSS-CHECK: the class now comes from whether the trace
# entry populated `printing_guaranteed`, and any disagreement with these sets fails the run. They are
# still the fallback for runs produced before Round 60, which carry no channel keys.
#
# BOUND: writes `SpaceMeasure::guaranteed`, so its number must be `>= truth`, always.
#   - Three-space exact triples, folded `Candidate::Exact`.
#   - Printing-only bounds, folded `Candidate::PrintingBound` (Round 59): `LegalityDateTotals` (an
#     exact prefix-sum subtraction) and `PriceJointTable` (a cell is counted whenever it overlaps the
#     query rectangle at all, and every printing is in exactly one cell, so a match cannot be missed).
BOUND_CLASS_MECHANISMS = frozenset(
    {
        "ArithIdProbe",
        "ColorCmcTable",
        "LegalityDateTotals",
        "PairRangeSum",
        "PairTotals",
        "PlanePopcount",
        "PriceJointTable",
        "SubtypeArithBox",
        "SubtypePairIndexes",
        "SubtypeSubtypeExact",
        "arith_tuple_totals",
        "leaves_are_disjoint",
    }
)
# ESTIMATE: a guess that can land on either side of the truth. Listed so an unrecognized mechanism is
# a hard failure rather than being silently treated as "probably a guess, skip it".
ESTIMATE_CLASS_MECHANISMS = frozenset(
    {
        "ColorCmcAnchoredIndependence",
        "Independence",
        "SetCollectorRange",
        "SubtypeArithAnchoredIndependence",
        "SubtypePairEstimate",
        "SubtypeSubtypeEstimate",
    }
)

# The one space where `true_total` and the trace's `printing` numbers are the same quantity -- see the
# module docstring's confound section.
CHECKED_UNIQUE = "printing"
# How many individual violating rows to print per mechanism before summarizing the rest. A failure
# wants examples to reproduce from, not the whole population.
MAX_EXAMPLES_PER_MECHANISM = 10

ROW_LEVEL_VIEW_DOC = """
The row-level view below reproduces the shape of Round 59's own hand-built audit table (bucket by
`and_mechanism`, compare `predicted_matches` against `true_total`) and is a DIAGNOSTIC ONLY -- it
never decides the exit status. `predicted_matches` is `SpaceMeasure::best()`, a min over both channels
and every mechanism at once, so a bound-class mechanism can appear in `and_mechanism` on a row whose
final number is below truth without having contributed that number: an estimate-class mechanism, or
one of the three reprint-ratio leaf arms, can be the one binding. Round 59's audit read 0 violations
here because on those rows the bound happened to be binding; that is an observation about a
population, not an invariant. The per-candidate check above is the invariant.
"""


def load_rows(path: pathlib.Path) -> list[dict]:
    """Read a JSONL run written by `nway_estimate_truth_survey.py --out`."""
    with path.open() as fh:
        return [json.loads(line) for line in fh]


def bound_class_claims(row: dict) -> list[tuple[str, int, bool]]:
    """Every (mechanism, claimed_bound, from_channels) this row's `And` arm actually PROVED a number for.

    Reads `and_trace.considered`, which holds one entry per ATTEMPTED mechanism -- winners and losers
    alike. Checking the losers matters: a bound-class candidate that undershoots is unsound whether or
    not it happened to win the fold on this particular query, and a future mechanism could easily
    undershoot only on rows where something else was tighter.

    A `hit: false` entry is a clean decline with no number in any channel and is skipped, not treated
    as a zero -- the trace expresses absence as `null` precisely so it cannot be misread as
    "proved empty".

    `from_channels` says whether this claim was read from `printing_guaranteed` (Round 60 and later)
    or fell back to the mechanism-name map, so `check_file` can report a pre-Round-60 run as the
    weaker check it is.
    """
    trace = row.get("and_trace")
    if not trace:
        return []
    out: list[tuple[str, int, bool]] = []
    for group in trace.get("considered", []):
        if not group.get("hit"):
            continue
        if "printing_guaranteed" in group:
            # Round 60: a candidate is bound-class exactly when it populated `guaranteed`, and the
            # number to check is that channel's own -- not the `best()`-collapsed `printing`.
            if group["printing_guaranteed"] is not None:
                out.append((group["mechanism"], group["printing_guaranteed"], True))
        elif group.get("printing") is not None and group["mechanism"] in BOUND_CLASS_MECHANISMS:
            out.append((group["mechanism"], group["printing"], False))
    return out


def channel_map_disagreements(rows: list[dict]) -> list[str]:
    """Mechanisms whose written CHANNEL disagrees with this script's own name map.

    The cross-check Round 60 keeps the map for: the engine says a candidate is bound-class by
    populating `printing_guaranteed`, and this script says so by name. If they ever differ, one of
    them is wrong and both are load-bearing, so the run fails rather than quietly preferring either.
    """
    bad: set[str] = set()
    for row in rows:
        for group in (row.get("and_trace") or {}).get("considered", []):
            if not group.get("hit") or "printing_guaranteed" not in group:
                continue
            engine_says_bound = group["printing_guaranteed"] is not None
            map_says_bound = group["mechanism"] in BOUND_CLASS_MECHANISMS
            if engine_says_bound != map_says_bound:
                bad.add(
                    f"{group['mechanism']} (engine: {'bound' if engine_says_bound else 'estimate'}, map: {'bound' if map_says_bound else 'estimate'})"
                )
    return sorted(bad)


def unknown_mechanisms(rows: list[dict]) -> set[str]:
    """Mechanism strings in these rows that this script has no classification for.

    A hard failure rather than a warning: an unclassified mechanism is precisely the case this check
    exists to catch (someone added a mechanism and did not decide which channel it may write), and
    defaulting it to either class would make this script quietly stop covering it.
    """
    seen: set[str] = set()
    for row in rows:
        trace = row.get("and_trace") or {}
        for group in trace.get("considered", []):
            seen.add(group["mechanism"])
        for name in filter(None, (row.get("and_mechanism") or "").split("+")):
            seen.add(name)
    return seen - BOUND_CLASS_MECHANISMS - ESTIMATE_CLASS_MECHANISMS


def per_candidate_check(rows: list[dict]) -> tuple[dict[str, list[dict]], collections.Counter, bool]:
    """The invariant: every bound-class candidate's own claimed bound must be `>= true_total`.

    Returns (violations by mechanism, attempt counts by mechanism, whether every claim was read from
    the trace's own channels) over `unique=printing` rows only.
    """
    violations: dict[str, list[dict]] = collections.defaultdict(list)
    attempts: collections.Counter = collections.Counter()
    all_from_channels = True
    for row in rows:
        if row["unique"] != CHECKED_UNIQUE or row.get("true_total") is None:
            continue
        for mechanism, claimed, from_channels in bound_class_claims(row):
            all_from_channels &= from_channels
            attempts[mechanism] += 1
            if claimed < row["true_total"]:
                violations[mechanism].append({"q": row["q"], "claimed": claimed, "true_total": row["true_total"]})
    return violations, attempts, all_from_channels


def row_level_table(rows: list[dict]) -> None:
    """Print the coarse `and_mechanism` x `predicted_matches` view -- diagnostic only."""
    buckets: dict[str, list[bool]] = collections.defaultdict(list)
    for row in rows:
        if row["unique"] != CHECKED_UNIQUE or row.get("true_total") is None:
            continue
        for name in filter(None, (row.get("and_mechanism") or "").split("+")):
            buckets[name].append(row["predicted_matches"] < row["true_total"])
    print("\nROW-LEVEL VIEW (diagnostic, does NOT decide the exit status)")
    print(ROW_LEVEL_VIEW_DOC)
    print(f"{'mechanism':<36}{'class':>10}{'rows':>8}{'under truth':>14}")
    for name, unders in sorted(buckets.items(), key=lambda kv: (-sum(kv[1]), kv[0])):
        cls = "bound" if name in BOUND_CLASS_MECHANISMS else "estimate"
        print(f"{name:<36}{cls:>10}{len(unders):>8}{sum(unders):>14}")


def check_file(path: pathlib.Path) -> bool:
    """Run both views over one survey run. Returns True if the invariant holds."""
    rows = load_rows(path)
    printing_rows = [r for r in rows if r["unique"] == CHECKED_UNIQUE]
    traced = [r for r in printing_rows if r.get("and_trace")]
    print(f"\n{'=' * 96}\n{path}")
    print(f"  {len(rows):,} rows; {len(printing_rows):,} in {CHECKED_UNIQUE} space; {len(traced):,} of those carry an and_trace")
    if not traced:
        print("  FAIL: no and_trace rows at all -- this run cannot check anything (was it produced before Round 37a?)")
        return False

    if unknown := unknown_mechanisms(rows):
        print(f"  FAIL: unclassified mechanism(s) {sorted(unknown)} -- add each to BOUND_CLASS_MECHANISMS or")
        print("        ESTIMATE_CLASS_MECHANISMS in this script, deliberately, after deciding which channel it may write")
        return False

    if disagreements := channel_map_disagreements(rows):
        print("  FAIL: the trace's own channels disagree with this script's name map for:")
        for line in disagreements:
            print(f"        {line}")
        print("        One of the two is wrong. Either the mechanism writes the wrong channel in lib.rs, or this")
        print("        script's BOUND_CLASS_MECHANISMS/ESTIMATE_CLASS_MECHANISMS needs a deliberate update.")
        return False

    violations, attempts, from_channels = per_candidate_check(rows)
    source = (
        "trace channels (printing_guaranteed)" if from_channels else "the mechanism-name map (pre-Round-60 run: no channel keys)"
    )
    print("\nPER-CANDIDATE CHECK (the invariant): every bound-class candidate's own claimed bound vs true_total")
    print(f"  bound-class decided by: {source}")
    print(f"{'mechanism':<36}{'candidates':>12}{'under truth':>14}")
    for name in sorted(BOUND_CLASS_MECHANISMS):
        if attempts[name]:
            print(f"{name:<36}{attempts[name]:>12}{len(violations.get(name, [])):>14}")
    unattempted = sorted(n for n in BOUND_CLASS_MECHANISMS if not attempts[n])
    if unattempted:
        print(f"  (not exercised by this run, so unchecked: {', '.join(unattempted)})")

    row_level_table(rows)

    if not violations:
        print(f"\nOK: {sum(attempts.values()):,} bound-class candidates, none below truth.")
        return True
    print("\nFAIL: a bound-class mechanism predicted BELOW the true count. Either its number is not a real")
    print("      count of a real set (move it to the estimate channel), or its bound argument is wrong.")
    for name, examples in sorted(violations.items()):
        print(f"\n  {name}: {len(examples)} of {attempts[name]} candidates under truth")
        for ex in examples[:MAX_EXAMPLES_PER_MECHANISM]:
            print(f"    claimed {ex['claimed']:,} vs true {ex['true_total']:,}  {ex['q']!r}")
        if len(examples) > MAX_EXAMPLES_PER_MECHANISM:
            print(f"    ... and {len(examples) - MAX_EXAMPLES_PER_MECHANISM} more")
    return False


def main() -> None:
    """Check every given survey run; exit 1 if any of them violates the invariant."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=pathlib.Path, help="JSONL run(s) from nway_estimate_truth_survey.py --out")
    args = parser.parse_args()
    for path in args.runs:
        if not path.is_file():
            parser.error(f"not a file: {path}")
    ok = all([check_file(path) for path in args.runs])  # noqa: C419 - every file must be reported, not short-circuited
    print()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
