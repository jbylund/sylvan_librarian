"""Where does the model's compose-paging PREDICTION disagree with the exit the executor takes?

`empty-page`'s cost-model half is two failures wearing one gate. `cost.rs` prices
`ComposePaging::Decline` at `f64::INFINITY`, which keeps compose out of the argmin entirely, and
that is right only when the plan really refuses:

- **Excluded wrongly** — the model predicts `Decline`, but the executor's real exit is fast
  (`EmptyPage` returns the correct answer in ~1.4 us). Compose is never given the chance.
- **Admitted wrongly** — the model predicts a runnable branch, compose wins the argmin, and then the
  executor refuses after paying the whole build. `declined_ns` is the time thrown away before the
  fallback even starts.

The two are visible only as a CROSS-TAB, because the enums are asymmetric on purpose: the model has
four `ComposePaging` values, the executor reports nineteen `PagingTaken` exits -- `EmptyPage`,
`NotComposable`, four distinct `Decline*` reasons, and the `Range*` family. Collapsing either side
loses the disagreement.

`explain_analyze` forces every plan, so compose's realized exit is observable even on queries the
router would never route to it. That is what makes the excluded population measurable at all.

**A declining plan has EMPTY `trials_ns`** -- `declined_ns` is recorded INSTEAD of it, not beside it
(see `PlanTrial::declined_ns`). So the two populations must be filtered separately: requiring a
timing at all would silently drop every declining row, which is the population-parity trap in
`.claude/rules/benchmark-methodology-review.md`. An earlier version of this script did exactly that
and reported the wasted-build population as empty.

Run BOTH samplers, per this arc's standing rule: rank by `uniform`, value by `realistic`.

    PYTHONPATH=<enginedir> .venv/bin/python scripts/bench_compose_paging_confusion.py --mode uniform
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import random
import statistics
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.query_sampler import MODES, QuerySampler  # noqa: E402
from scripts import costbench  # noqa: E402
from scripts.costbench import Budget, iter_samples, load_engine, plan_self_ns  # noqa: E402

#: Realized exits that mean "compose refused after doing work". `NotComposable` is excluded: it is a
#: shape rejection decided before any build, so there is nothing thrown away.
DECLINE_EXITS = frozenset({"DeclineBroad", "DeclineSparseEstimate", "DeclineSparseExact", "GatherWalkDeclined"})
#: The exit that makes the INFINITY price wrong: compose returns the right answer faster than anything.
FAST_EXIT = "EmptyPage"
#: Below this a plan's measured time is timer resolution (~41.67 ns ticks, 24 MHz timebase).
MIN_MEASURED_NS = 500.0
DEFAULT_N_QUERIES = 8000
#: Individual rows to print per population, before falling back to the aggregate.
MAX_EXAMPLES = 8


def median_or_none(vals: object) -> float | None:
    """Median of a per-trial counter list, tolerating a scalar or an absent value."""
    if vals is None:
        return None
    if isinstance(vals, (int, float)):
        return float(vals)
    seq = [float(v) for v in vals if v is not None]
    return statistics.median(seq) if seq else None


def main() -> None:  # noqa: PLR0912, PLR0915 - a report of four sections, each a few prints
    """Cross-tab predicted compose paging against the executor's realized exit."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n-queries", type=int, default=DEFAULT_N_QUERIES)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="uniform")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, required=True)
    args = parser.parse_args()

    engine = load_engine(args.corpus, args.shm_path)
    sampler = QuerySampler(args.corpus, args.mode)
    budget = Budget(sample=args.n_queries, warmups=costbench.NUM_WARMUPS, trials=costbench.NUM_TRIALS)

    grid: dict[tuple[str, str], int] = collections.Counter()
    excluded: list[dict] = []
    wasted: list[dict] = []
    n = 0

    for sample in iter_samples(engine, sampler, random.Random(args.seed), budget, vary_prefer=True):
        acq = sample.acquire
        pc = next((p for p in sample.plans if p["plan"] == "PrintingCompose"), None)
        # NOT gated on `trials_ns`: a declining compose has none, and those rows are half the point.
        if pc is None or not (pc.get("trials_ns") or pc.get("declined_ns")):
            continue
        predicted = acq.get("compose_paging") or "?"
        realized = pc.get("paging_taken") or "?"
        n += 1
        grid[(predicted, realized)] += 1
        picked = next((p["plan"] for p in sample.plans if p.get("picked")), None)
        pc_ns = plan_self_ns(pc, acq) if pc.get("trials_ns") else None
        best_other = min(
            (plan_self_ns(p, acq) or float("inf") for p in sample.plans if p["plan"] != "PrintingCompose" and p.get("trials_ns")),
            default=float("inf"),
        )
        row = {
            "q": sample.q,
            "unique": sample.kw["unique"],
            "offset": sample.kw["offset"],
            "predicted": predicted,
            "realized": realized,
            "pc_ns": pc_ns,
            "best_other_ns": best_other,
            "picked": picked,
            "declined_ns": median_or_none(pc.get("declined_ns")),
        }
        # Excluded wrongly: priced INFINITY, but the real exit is the fast one.
        if predicted == "Decline" and realized == FAST_EXIT and pc_ns and pc_ns >= MIN_MEASURED_NS:
            row["saving_ns"] = best_other - pc_ns
            excluded.append(row)
        # Admitted wrongly: compose was PICKED and then refused after paying the build. Keyed on the
        # COUNTER rather than the exit label, so a decline reason this script does not know about is
        # still counted.
        if picked == "PrintingCompose" and row["declined_ns"]:
            wasted.append(row)

    print(f"\n{n:,} queries where PrintingCompose was costed and timed, mode={args.mode}\n")
    print(f"{'=' * 100}\nPREDICTED ComposePaging  x  REALIZED PagingTaken\n{'=' * 100}")
    preds = sorted({p for p, _ in grid})
    reals = sorted({r for _, r in grid}, key=lambda r: -sum(v for (_, rr), v in grid.items() if rr == r))
    print(f"  {'predicted':<14}" + "".join(f"{r[:17]:>19}" for r in reals[:5]))
    for p in preds:
        cells = "".join(f"{grid[(p, r)] or '':>19}" for r in reals[:5])
        print(f"  {p:<14}{cells}")
    if len(reals) > 5:  # noqa: PLR2004
        print(f"  (+{len(reals) - 5} rarer realized exits: {', '.join(reals[5:])})")

    print(f"\n{'=' * 100}\nEXCLUDED WRONGLY -- predicted Decline (priced INFINITY), real exit {FAST_EXIT}\n{'=' * 100}")
    if not excluded:
        print("  none")
    else:
        savings = [r["saving_ns"] for r in excluded if r["saving_ns"] not in (None, float("inf"))]
        print(f"  {len(excluded):,} queries. Compose measured p50 {statistics.median([r['pc_ns'] for r in excluded]) / 1000:.2f} us")
        if savings:
            print(f"  Against the best plan that DID run: p50 saving {statistics.median(savings) / 1000:.2f} us, total {sum(savings) / 1e6:.2f} ms")
        for r in sorted(excluded, key=lambda r: -(r["saving_ns"] or 0))[:MAX_EXAMPLES]:
            print(f"    saves {(r['saving_ns'] or 0) / 1000:>8.1f} us  compose {r['pc_ns'] / 1000:>7.2f} us  "
                  f"picked={r['picked']!s:<16} off={r['offset']:<4} {r['q'][:32]}")

    print(f"\n{'=' * 100}\nADMITTED WRONGLY -- compose PICKED, then refused after paying the build\n{'=' * 100}")
    if not wasted:
        print("  none")
    else:
        dn = [r["declined_ns"] for r in wasted if r["declined_ns"]]
        print(f"  {len(wasted):,} queries, by realized exit: {dict(collections.Counter(r['realized'] for r in wasted))}")
        if dn:
            s = sorted(dn)
            print(f"  declined_ns: p50 {statistics.median(s) / 1000:.2f} us  p90 {s[int(0.9 * (len(s) - 1))] / 1000:.2f} us  "
                  f"TOTAL {sum(s) / 1e6:.2f} ms thrown away")
        for r in sorted(wasted, key=lambda r: -(r["declined_ns"] or 0))[:MAX_EXAMPLES]:
            print(f"    wasted {(r['declined_ns'] or 0) / 1000:>8.2f} us  predicted={r['predicted']:<12} "
                  f"realized={r['realized']:<22} {r['q'][:30]}")


if __name__ == "__main__":
    main()
