"""Does `cost::materialize_cost` predict the realized `PhaseStats::ns_prepare`?

`materialize_cost` is the engine's model of the artifact a plan builds **in dispatch** -- for the two
materializing plans, `prepare_candidates`. It is computed, published as `explain`'s `materialize_ns`,
and (today) excluded from the argmin, so nothing has ever graded it. This harness does, against the
one counter that measures the thing it claims to predict.

Three questions, in the order they have to be answered:

1. **Shape.** `prepare_candidates` has three phases -- `narrow_candidates_exact`, the
   projection/materialization, and `memoize_text_predicates` -- and they scale with three different
   things. A model with one term for one of them cannot be repaired by moving its constants.
2. **Constants**, refit on that shape, calibration/held-out split by hash of the query string so no
   query informs both the fit and the number reported from it.
3. **Population.** Reported separately for `--mode uniform` and `--mode realistic`, never pooled: the
   standing rule is RANK by uniform and VALUE by realistic, and these two disagree sharply here --
   the acquire mix that decides which phase dominates is itself mode-dependent.

Grading is median |ln(predicted/measured)| and the fraction within 25%, both over the held-out half.

    .venv/bin/python scripts/bench_prepare_cost_shape.py --n-queries 4000 --mode uniform
"""

from __future__ import annotations

import argparse
import hashlib
import math
import pathlib
import random
import statistics
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.query_sampler import MODES, QuerySampler  # noqa: E402
from scripts import costbench  # noqa: E402
from scripts.fit_cost_model import fit_log_ratio  # noqa: E402

#: The two plans whose dispatch calls `prepare_candidates`, and so the only two `materialize_cost`
#: gives a non-zero term to. Every other plan's `ns_prepare` measures an artifact the ROUTER built
#: during acquire (or one `plan_cost` already charges directly) -- see `materialize_cost`'s doc.
MATERIALIZING = ("GatheredScan", "StreamedSelect")

#: Held-out fraction, by hash of the query STRING. Splitting on the query rather than on the row keeps
#: the two halves disjoint in shapes: one query yields up to two plan rows and is sampled under many
#: page shapes, so a row-wise split would put near-duplicates on both sides and report a held-out
#: number that is really an in-sample one.
HELDOUT_FRACTION = 0.5
#: Hash space the split is taken modulo. Any large power of two; fixed so a split is reproducible
#: across runs and across the two modes.
SPLIT_MODULUS = 1 << 20

#: `abs(ln(ratio))` under which a prediction counts as "within 25%".
WITHIN_25 = math.log(1.25)

#: Rows whose `ns_prepare` is at or below this are dropped from BOTH grading and fitting. The mach
#: timebase on Apple Silicon is 24 MHz, so every measurement is a multiple of ~41.67 ns and a
#: one-to-two-tick reading carries a +-50% quantization error of its own. Three ticks (125 ns) is the
#: first value with better than 33% resolution.
MIN_GRADED_NS = 125.0

#: Ridge anchor for the fit -- the SHIPPED constants, so a column this corpus cannot identify stays
#: where measurement put it instead of drifting to whatever the intercept wants. Order matches
#: `design_row`.
TERM_NAMES = ("PREPARE_FIXED_NS", "PREPARE_PER_NODE_NS", "PREPARE_PLANE_PER_WORD_OP_NS", "PREPARE_PER_CAND_NS")


def design_row(r: dict) -> list[float]:
    """The four columns of the refitted shape, in `TERM_NAMES` order.

    Mirrors `cost::materialize_cost`'s materializing arm exactly; `mirror_check` asserts that.
    """
    return [1.0, float(r["prepare_nodes"]), float(r["prepare_plane_word_ops"]), float(r["prepare_cands"])]


#: The four constants `cost.rs` currently ships, in `TERM_NAMES` order. Used ONLY as the fit's ridge
#: anchor and by `mirror_check` -- the graded "shipped" column reads the engine's own `materialize_ns`,
#: never this tuple, so a stale copy here cannot flatter the result. `mirror_check` fails the run if it
#: drifts from `cost.rs` anyway.
SHIPPED = (121.0, 942.0, 1.543, 1.641)


def mirror_check(rows: list[dict]) -> float:
    """Fraction of rows where `design_row · SHIPPED` reproduces the engine's own `materialize_ns`.

    The same guard `fit_cost_model.mirror_matches_engine` applies: if this file's arm has drifted from
    `cost.rs`, every constant it prints is fitted for a model the engine does not run.
    """
    if not rows:
        return float("nan")
    ok = 0
    for r in rows:
        mine = sum(c * v for c, v in zip(SHIPPED, design_row(r), strict=True))
        if abs(mine - r["materialize_ns"]) <= max(1e-6, 1e-9 * abs(mine)):
            ok += 1
    return ok / len(rows)


def is_heldout(q: str) -> bool:
    """Split by hash of the query STRING -- stable across runs, modes and process restarts."""
    h = int.from_bytes(hashlib.blake2b(q.encode(), digest_size=8).digest(), "big")
    return (h % SPLIT_MODULUS) < HELDOUT_FRACTION * SPLIT_MODULUS


#: The three phase-split keys this harness reads on top of `costbench.PLAN_KEYS`. Registered rather
#: than assumed, so a build that stops publishing them fails in `require_schema` by name.
PHASE_KEYS = frozenset({"ns_narrow", "ns_project", "ns_memo"})
costbench.PLAN_KEYS = costbench.PLAN_KEYS | PHASE_KEYS


def collect(engine: object, sampler: QuerySampler, seed: int, n_queries: int) -> list[dict]:
    """One row per (query, materializing plan) that ran, carrying features and realized counters."""
    rows: list[dict] = []
    budget = costbench.Budget(sample=n_queries)
    for s in costbench.iter_samples(engine, sampler, random.Random(seed), budget, vary_prefer=True):
        for p in s.plans:
            if p["plan"] not in MATERIALIZING or not p["trials_ns"]:
                continue
            rows.append(
                {
                    "q": s.q,
                    "plan": p["plan"],
                    "src": s.acquire["count_source"],
                    "repr": s.acquire["narrowed_repr"],
                    "ns_prepare": float(p["ns_prepare"]),
                    "materialize_ns": float(p["materialize_ns"]),
                    "eval_domain": s.acquire["eval_domain"],
                    "n_cards": s.acquire["n_cards"],
                    "cards_visited": p["cards_visited"],
                    "prepare_nodes": s.acquire["prepare_nodes"],
                    "prepare_plane_word_ops": s.acquire["prepare_plane_word_ops"],
                    "prepare_cands": s.acquire["prepare_cands"],
                    # All three are 0 unless the engine was built `--features prepare-phases`; see
                    # `report_phases`.
                    "ns_narrow": float(p["ns_narrow"]),
                    "ns_project": float(p["ns_project"]),
                    "ns_memo": float(p["ns_memo"]),
                }
            )
    return rows


def report_phases(rows: list[dict]) -> None:
    """Where inside `prepare_candidates` the time goes, by acquire -- the evidence for the SHAPE.

    Needs an engine built with the `prepare-phases` cargo feature; without it the three phase timers
    compile away and every row reads zero, which this says rather than printing a table of zeros.

    Reported as the median of the per-row SHARE, not as a ratio of medians: the three phases have very
    different distributions and a ratio of medians is not a share of anything. The `accounted` column
    is the same three shares summed, which must come out at 1.00 -- the phases are contiguous and
    cover the whole timed span, so anything less means a timer boundary has drifted.
    """
    timed = [r for r in rows if r["ns_prepare"] >= MIN_GRADED_NS and r["ns_narrow"] + r["ns_project"] + r["ns_memo"] > 0]
    if not timed:
        print("\n  phase split: unavailable (build the engine with `--features prepare-phases` to fill it in)")
        return
    print(f"\n  prepare_candidates phase split, median of the per-row share (n={len(timed):,})")
    print(f"  {'acquire':<22}{'n':>6}{'ns_prepare':>12}{'narrow':>9}{'project':>9}{'memo':>7}{'accounted':>11}")
    for src in [None, *sorted({r["src"] for r in timed})]:
        cell = [r for r in timed if src is None or r["src"] == src]
        if len(cell) < costbench.MIN_ROWS and src is not None:
            continue
        shares = [statistics.median([r[k] / r["ns_prepare"] for r in cell]) for k in ("ns_narrow", "ns_project", "ns_memo")]
        acc = statistics.median([(r["ns_narrow"] + r["ns_project"] + r["ns_memo"]) / r["ns_prepare"] for r in cell])
        med = statistics.median([r["ns_prepare"] for r in cell])
        print(
            f"  {(src or 'ALL'):<22}{len(cell):>6}{med:>12.0f}"
            + "".join(f"{s:>9.2f}" for s in shares[:2])
            + f"{shares[2]:>7.2f}{acc:>11.2f}"
        )


def grade(rows: list[dict], pred: Callable[[dict], float]) -> tuple[float, float, int]:
    """Median |ln(predicted/measured)| and the within-25% fraction over `rows`."""
    ls = []
    for r in rows:
        p = pred(r)
        if r["ns_prepare"] < MIN_GRADED_NS or p <= 0:
            continue
        ls.append(abs(math.log(p / r["ns_prepare"])))
    if not ls:
        return float("nan"), float("nan"), 0
    return statistics.median(ls), sum(1 for x in ls if x < WITHIN_25) / len(ls), len(ls)


def old_shape(r: dict) -> float:
    """The shape this harness replaces: `143.0 + 4.95 * eval_domain`, for a before/after column."""
    old_fixed, old_per_cand = 143.0, 4.95
    return old_fixed + old_per_cand * r["eval_domain"]


def fit(rows: list[dict]) -> list[float]:
    """Refit the four constants on the CALIBRATION rows only, in log space."""
    usable = [r for r in rows if r["ns_prepare"] >= MIN_GRADED_NS]
    design = [design_row(r) for r in usable]
    targets = [r["ns_prepare"] for r in usable]
    return fit_log_ratio(design, targets, list(SHIPPED), [1.0] * len(usable))


def report_cells(rows: list[dict], preds: dict[str, Callable[[dict], float]], title: str) -> None:
    """One grading table, split by acquire source -- the axis the phases differ along."""
    print(f"\n{title}  (n={len(rows)})")
    header = f"  {'acquire':<22}{'n':>6}"
    for name in preds:
        header += f"{name + ' |ln|':>14}{'w25':>8}"
    print(header)
    for src in [None, *sorted({r["src"] for r in rows})]:
        cell = [r for r in rows if src is None or r["src"] == src]
        if len(cell) < costbench.MIN_ROWS and src is not None:
            continue
        line = f"  {(src or 'ALL'):<22}{len(cell):>6}"
        for pred in preds.values():
            med, w25, n = grade(cell, pred)
            line += f"{med:>14.3f}{w25:>8.1%}" if n else f"{'-':>14}{'-':>8}"
        print(line)


def main() -> None:
    """Collect, grade the shipped shape, refit on the calibration half, grade on the held-out half."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n-queries", type=int, default=4000, help="sampled queries; never --seconds, so a pair is exact")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="uniform")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None)
    args = parser.parse_args()
    if args.n_queries <= 0:
        parser.error("--n-queries must be positive")

    engine = costbench.load_engine(args.corpus, args.shm_path or args.corpus.with_suffix(".prepshape.store"))
    rows = collect(engine, QuerySampler(args.corpus, args.mode), args.seed, args.n_queries)
    print(f"\n{len(rows):,} plan-rows over {len({r['q'] for r in rows}):,} distinct queries, mode={args.mode}")

    agree = mirror_check(rows)
    print(f"  arm mirror agrees with the engine's own materialize_ns on {agree:.1%} of rows")
    if agree < 0.99:  # noqa: PLR2004 - the same bar fit_cost_model uses
        print("  REFUSING to fit: this file's design_row has drifted from cost::materialize_cost")
        return

    calib = [r for r in rows if not is_heldout(r["q"])]
    held = [r for r in rows if is_heldout(r["q"])]
    print(f"  split by query hash: {len(calib):,} calibration rows / {len(held):,} held out")

    coeffs = fit(calib)
    print("\n  fitted (calibration half only):")
    for name, c, s in zip(TERM_NAMES, coeffs, SHIPPED, strict=True):
        print(f"    {name:<28}{c:>12.3f}   (prior {s})")

    def fitted(r: dict) -> float:
        return sum(c * v for c, v in zip(coeffs, design_row(r), strict=True))

    report_phases(rows)

    preds = {"old": old_shape, "shipped": lambda r: r["materialize_ns"], "fitted": fitted}
    report_cells(held, preds, "HELD OUT")
    report_cells(calib, preds, "calibration (in-sample, for reference only)")

    # `prepare_cands` claims to be the candidate count; `cards_visited` on GatheredScan IS it.
    gs = [r for r in held if r["plan"] == "GatheredScan" and r["cards_visited"] > 0]
    if gs:
        ratios = sorted(r["prepare_cands"] / r["cards_visited"] for r in gs)
        print(
            f"\n  prepare_cands / realized cards_visited (GatheredScan, held out, n={len(gs)}): "
            f"p10={costbench.percentile(ratios, 10):.2f} p50={costbench.percentile(ratios, 50):.2f} "
            f"p90={costbench.percentile(ratios, 90):.2f}"
        )

    dropped = sum(1 for r in rows if r["ns_prepare"] < MIN_GRADED_NS)
    print(f"\n  {dropped:,} of {len(rows):,} rows dropped as below the {MIN_GRADED_NS:.0f} ns (3-tick) timer floor.")
    print("  Every cell above is graded over the SAME row population for all three predictors.")


if __name__ == "__main__":
    main()
