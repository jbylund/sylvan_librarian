"""Shared measurement core for the `explain_analyze` harnesses.

Eleven benchmarks drive `QueryEngine.explain_analyze` and, before this module, each one carried its
own copy of the same four things: the sampling loop, a nearest-rank `percentile`, a percentile-table
renderer, and the rule for turning a plan's raw `trials_ns` into "what this plan costs to run". The
copies had drifted, and the drift was not cosmetic:

- Three different netting rules. Two subtracted `ns_prepare`, disagreeing on what to do when the
  subtraction overshot; a third subtracted `acquire_ns`, which times a DIFFERENT participant. A
  number netted one way is not comparable to a number netted another, which quietly undercut the
  cross-harness working order in `docs/issues/reference-cost-model-measurement.md`.
- Five different `(warmups, trials)` settings across nine files, with no stated reason for the
  differences beyond what each author needed that day.
- One `percentile` copy had lost its empty-input guard and raised `IndexError` on an empty slice
  where the other two returned `nan`.

So the point of this module is not line count. It is that every harness answers "how long did this
plan take" the same way, and a fix to that answer lands everywhere at once.

The schema this module reads is asserted, not assumed -- see `require_schema`. `explain_analyze`'s
response shape has changed before, and a harness that reads a key the engine stopped publishing
should say so rather than `KeyError` a hundred queries in.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import math
import random
import statistics
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Iterator

    from client.query_sampler import QuerySampler

# ── engine loading ────────────────────────────────────────────────────────────────────────────
# Rows per `add_batch` during a staged reload. Large enough that the per-call overhead disappears,
# small enough that the batch list stays cheap to hold.
BATCH_SIZE = 2000


def load_engine(corpus: pathlib.Path, shm_path: pathlib.Path) -> object:
    """Build a fresh engine store from the corpus JSONL via the staged reload API.

    Lived in `bench_bitplanes.py` until 35 call sites were importing it out of a targeted benchmark
    whose own investigation had closed. Nothing about it is bitplane-specific.

    Raises:
        RuntimeError: if `reload_begin` refuses, which means another process published concurrently.
    """
    import card_engine  # noqa: PLC0415 - keeps `costbench` importable without the built extension

    engine = card_engine.QueryEngine(str(shm_path))
    if not engine.reload_begin():
        msg = "reload_begin returned False (stale archive published concurrently?)"
        raise RuntimeError(msg)
    t0 = time.monotonic()
    batch: list[dict] = []
    with corpus.open() as fh:
        for line in fh:
            batch.append(json.loads(line))
            if len(batch) == BATCH_SIZE:
                engine.add_batch(batch)
                batch.clear()
    if batch:
        engine.add_batch(batch)
    engine.reload_commit()
    print(f"Engine loaded: {engine.size():,} printings in {time.monotonic() - t0:.1f}s", flush=True)
    return engine


# ── measurement defaults ──────────────────────────────────────────────────────────────────────
# The (2, 7) pair six of the nine harnesses had already converged on. Seven is the floor for reading
# `trials_ns` as a head-to-head: participants are shuffled per round from a fixed seed rather than
# rotated, so at 2-3 trials a plan can draw the warm tail twice. See docs/issues/00801-*.
#
# These defaults are for comparisons made INSIDE one `explain_analyze` call -- plan against plan,
# measured against predicted -- where every participant ran in the same rounds under the same
# conditions, so the floor-estimation error is common-mode and largely cancels. Prefix-min of
# `trials_ns` against min-of-60, median over queries, measured in one process:
#
#     k=          2      3      5      7     10     15     20     30     45     60
#     GatheredScan    1.029  1.024  1.017  1.014  1.013  1.004  1.002  1.000  1.000  1.000
#     StreamedSelect  1.010  1.007  1.005  1.004  1.003  1.001  1.001  1.000  1.000  1.000
#
# So at k=7 an absolute measurement sits 0.4-1.4% above the true floor -- far below the errors these
# harnesses hunt (2x feature miscounts, 100x cost tails), and not worth 4x the runtime.
#
# A CROSS-PROCESS comparison is a different regime and must not use these. There each run carries its
# own floor error, and it does NOT average out over queries because it is common-mode within the run.
# `bench_plan_execution_ab.py` and `bench_query_latency_ab.py` both false-positived on same-build,
# same-seed pairs at (2, 7) and both come out clean at (6, 30); each states its own numbers.
NUM_WARMUPS = 2
NUM_TRIALS = 7
# Page shapes to sample. Mostly first-page, which is what real traffic asks for.
LIMITS = (10, 100, 175)
OFFSETS = (0, 0, 0, 100)

# Nearest-rank percentiles reported by `percentile_table`. 0 and 100 are the real min and max, and
# they earn their place here: estimates of zero cards and of every card are both common, and a p1/p99
# view clips exactly the cases where a cost arm is most likely to be shaped wrong.
PERCENTILES = (0, 1, 10, 20, 50, 70, 90, 99, 100)
# Cells thinner than this are noise; a percentile over a handful of samples says nothing.
MIN_ROWS = 30
# Below this, a per-query difference is timer jitter rather than a real change.
NOISE_FLOOR_US = 1.0

# Acquire sources where acquire only ESTIMATED, so DISPATCH pays the artifact build itself -- either
# `prepare_candidates` on a lazy materialize, or `build_card_range_bits` for CardRangePopcount. On
# these `ns_prepare` is part of the plan's cost and `plan_self_ns` ADDS it. Everywhere else the router
# built the artifact during acquire and dispatch merely reuses it, so a forced run rebuilding it is
# reporting work dispatch never pays.
RANGE_ACQUIRES = frozenset({"card_range_popcount", "printing_range_scan", "printing_compose"})

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_CI = 0.95

# ── the explain_analyze contract ──────────────────────────────────────────────────────────────
# Asserted once per run against the first response, so a shape change is a named error rather than a
# KeyError deep in a collection loop. Keep in sync with `plan_trial_to_pydict` /
# `acquire_facts_to_pydict` in card_engine/src/lib.rs.
PLAN_KEYS = frozenset(
    {
        "plan",
        "predicted_ns",
        "materialize_ns",
        "picked",
        "trials_ns",
        "declined_ns",
        "cards_visited",
        "printing_span",
        "printings_examined",
        "matches_pushed",
        # PrintingCompose only (0 for every other plan): popcount(pbits), the composed printing-space
        # bitmap. Keyed variable for sigma_bound::three_phase_cost_ns's grading, not matches.
        "set_printings",
        # Permutation entries StreamedSelect's walk stepped. Realized ground truth for
        # `cost::stream_perm_steps`, `page_span * perm_walk_span / matches` -- NOT `n_cards`, which is
        # what this said until Round 70 and what two Python mirrors of the formula also had wrong. It
        # assumes matches are spread uniformly through the walked segment; Round 69 graded that at a
        # median of 1.023 with the error living entirely in the spread (1.9x on `orderby=name` to
        # 38.8x on `cmc`), so grade it rather than trust it, and read the spread rather than the median.
        "perm_steps",
        # StreamedSelect's small-total branch only (0 for every other plan/exit): printings
        # `push_card_matches` re-examined in the second, page-selecting pass over every matching
        # card -- the redo `printings_examined` (captured only from the first, counting-only pass)
        # structurally cannot see. Round 31 of the printing-varying-leaf depth ledger.
        # `filter.card_pass` invocations, the realized quantity behind `cost::residual_card_pass` --
        # the residual-gated per-card term whose coefficient is the residual floor, and the largest
        # single term in the model (58% of GatheredScan's predicted time). Zero for every plan that
        # verifies no residual. NOT `cards_visited` for StreamedSelect: its small-total redo loop and
        # its permutation walk each re-derive `card_pass` for a second population.
        "card_pass_calls",
        # PrintingCompose only: printings its BUILD's card->printing broadcast passes wrote or
        # cleared, the realized quantity behind `PlanFeatures::broadcast_printings`. Accumulated by
        # the passes themselves (`broadcast_card_bits_to_printings`, `legality_leaf_bits_from_absent`)
        # because they sit under a recursion with no publish site of its own.
        "broadcast_printings",
        "ns_setup",
        "ns_loop",
        "ns_finish",
        "ns_round_total",
        "ns_prepare",
        "result_total",
        "paging_taken",
    }
)
# The routed phase split is asserted here too: it is a partition of `routed_ns`, so a build that
# stops publishing it should fail loudly rather than have a consumer silently read an empty list.
ACQUIRE_KEYS = frozenset(
    {
        "count_source",
        "narrowed_repr",
        "acquire_ns",
        "routed_ns",
        "routed_acquire_ns",
        "routed_choose_ns",
        "routed_dispatch_ns",
        "compose_paging",
        # Round 37a: per-query provenance for the top-level `And` node's own `compose_printing_estimate`
        # evaluation (`None` when the top-level filter isn't an `And` at all) -- see
        # `docs/issues/local-engine-gathered-scan-card-printing-varying-depth.md`.
        "and_trace",
        # Round 39: single-shot wall time (ns) of the REAL, production, acquire-time
        # `compose_printing_estimate` call -- `None` whenever this query's acquire took a branch
        # other than `PrintingCompose` -- see `docs/issues/local-engine-nway-compose-independence-search.md`.
        "and_estimate_ns",
    }
)


def require_schema(res: dict) -> None:
    """Fail loudly if `explain_analyze` is not publishing what the harnesses read.

    Raises:
        RuntimeError: naming the missing keys, so a shape change reads as itself.
    """
    missing = {"acquire": sorted(ACQUIRE_KEYS - set(res.get("acquire", {})))}
    for plan in res.get("plans", []):
        if gap := sorted(PLAN_KEYS - set(plan)):
            missing[plan.get("plan", "?")] = gap
    if named := {k: v for k, v in missing.items() if v}:
        msg = f"explain_analyze response is missing keys this harness reads: {named}"
        raise RuntimeError(msg)


# ── sampling ──────────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Budget:
    """How much to measure, and how hard.

    Exactly one of `seconds` or `sample` bounds the run. Fewer trials buys more DISTINCT queries per
    unit of CPU; for a PAIRED comparison that is the better trade, since pairing already removes
    per-query variance and breadth beats depth.
    """

    seconds: float | None = None
    sample: int | None = None
    warmups: int = NUM_WARMUPS
    trials: int = NUM_TRIALS

    def __post_init__(self) -> None:
        """Reject a budget with no bound, or with two that could disagree."""
        if (self.seconds is None) == (self.sample is None):
            msg = "Budget takes exactly one of seconds= or sample="
            raise ValueError(msg)


@dataclasses.dataclass(frozen=True)
class Sample:
    """One measured query: what was asked, and both views of the answer."""

    q: str
    kw: dict
    acquire: dict
    """`explain`'s acquire facts -- the cheap pass, with the feature vector and no timings."""
    res: dict
    """The full `explain_analyze` response: `plans` plus its own `acquire` carrying `routed_ns`."""

    @property
    def plans(self) -> list[dict]:
        """The per-plan trials, in the router's predicted-cost order."""
        return self.res["plans"]

    def key(self) -> tuple:
        """Identity for pairing this query across two builds."""
        return (self.q, self.kw["unique"], self.kw["orderby"], self.kw["direction"], self.kw["limit"], self.kw["offset"])


def sample_kwargs(sampler: QuerySampler, rng: random.Random, *, vary_prefer: bool = False) -> dict:
    """One query shape: distinct-on, order, direction and page, weighted by the sampler's mode.

    `vary_prefer` draws `prefer` from the sampler instead of pinning it to `"default"`, and it is
    OFF by default for a reason that is not timidity: drawing it consumes the rng, so turning it on
    shifts every subsequent query in the stream. Baselines on disk were taken without it, and a
    harness comparing against one must keep the stream byte-identical.

    Turn it on where `prefer` is the variable under study. It is not cosmetic -- it decides whether
    the card-mode match kernels may stop at the first qualifying printing (`Prefer::Default`,
    printings are stored in prefer-desc order) or must score all of them to find the max. That is a
    ~3x difference in per-card work that no other sampled parameter reaches.
    """
    kwargs = {
        "filters": None,
        "unique": sampler.unique(rng),
        "orderby": sampler.orderby(rng),
        "direction": rng.choice(("asc", "desc")),
        "limit": rng.choice(LIMITS),
        "offset": rng.choice(OFFSETS),
    }
    if vary_prefer:
        kwargs["prefer"] = sampler.prefer(rng)
    return kwargs


def iter_samples(
    engine: object, sampler: QuerySampler, rng: random.Random, budget: Budget, *, vary_prefer: bool = False
) -> Iterator[Sample]:
    """Yield measured queries until the budget runs out, skipping any the parser rejects.

    The schema is checked against the first response that comes back, so a run against a build whose
    `explain_analyze` has moved on fails immediately instead of part way through.

    `vary_prefer` is forwarded to `sample_kwargs`; see there for why it defaults off. The drawn
    `prefer` lands in `Sample.kw`, so a caller that turns it on can slice by it.
    """
    # Imported here, not at module scope, so `costbench` stays importable (and unit-testable)
    # without the `api` package on the path. The cost is one import per process.
    from api.parsing import parse_scryfall_query  # noqa: PLC0415

    checked = False
    deadline = time.monotonic() + budget.seconds if budget.seconds is not None else math.inf
    taken = 0
    while time.monotonic() < deadline and (budget.sample is None or taken < budget.sample):
        taken += 1
        kw = sample_kwargs(sampler, rng, vary_prefer=vary_prefer)
        q = sampler.query(rng)
        try:
            kw["filters"] = parse_scryfall_query(q)
            # Both calls get the same `prefer`, and since Round 66 that MATTERS -- it is no longer a
            # convention. This comment used to say `PlanFeatures` does not carry `prefer`, "so the
            # features and every `predicted_ns` come back identical whatever is passed, while execution
            # honours it". That stopped being true when `compose_scan_printings` gained a
            # `Mode::Card if Prefer::Default` arm: the ACQUIRE reads `prefer` even though the struct
            # does not store it, so acquiring under one prefer and executing under another grades the
            # wrong feature against the right counter. (Measured cost of getting this wrong: compose's
            # gather cell reads 0.508 mismatched against 1.470 matched -- a 2.9x error that looks like
            # a finding.) Any harness written against the old claim must pass `prefer` to `explain` too.
            # What still holds is the useful half: most features are prefer-independent, so a ratio that
            # shifts with `prefer` is usually the feature failing to model a real difference in work.
            acquire = engine.explain(**kw)["acquire"]
            res = engine.explain_analyze(num_warmups=budget.warmups, num_trials=budget.trials, **{"prefer": "default", **kw})
        except Exception:  # noqa: BLE001, S112 - a rejected query is a skipped sample
            continue
        if not checked:
            require_schema(res)
            checked = True
        yield Sample(q=q, kw=kw, acquire=acquire, res=res)


# ── the one netting rule ──────────────────────────────────────────────────────────────────────


def predicted_ns(plan: dict) -> float | None:
    """The model's cost for this plan, or None when it did not give one.

    `cost::plan_cost` returns `f64::INFINITY` for `ComposePaging::Decline` — that is "never pick me",
    not a prediction. It arrives in Python as `inf`, and the `predicted_ns <= 0` guard every harness
    used does not catch it, so any ratio built from such a row is `inf` and poisons whichever
    percentile cell it lands in. Visible as `inf` in `PrintingCompose`'s p90/p99/p100 before this
    existed; the old p1..p99 tables hid it wherever those rows stayed under a tenth of the cell.
    """
    p = plan["predicted_ns"]
    return p if math.isfinite(p) and p > 0 else None


def plan_self_ns(plan: dict, acquire: dict) -> float | None:
    """What this plan costs to RUN, in nanoseconds -- the single definition every harness uses.

    The quantity wanted is DISPATCH: what the routed path spends after it has chosen. It is built by
    ADDITION now, from two measured pieces, where it used to be recovered by subtracting `ns_prepare`
    from a trial.

    **The executor**, from `ns_setup + ns_loop + ns_finish`. Contiguous by construction, so the sum IS
    the executor -- exact, with nothing to overshoot. Every plan publishes these; the two materializing
    ones split them three ways, `PrintingCompose` splits them two ways (`ns_setup` for the build,
    `ns_loop` for the paging branch, no `ns_finish`), and the three remaining plans report one
    undivided span in `ns_loop` because they have no phases to attribute between at all. Verified
    against the old netted figure on 45k paired rows before the switch: median ratio 0.998
    (GatheredScan) and 0.997 (StreamedSelect), 0.08us of the round unaccounted.

    **Plus any shared artifact DISPATCH pays for**, which depends on the acquire and not on the plan:

    - `candidates` / `plane` acquire -- the router built the artifact during acquire and dispatch just
      reuses it, so dispatch is the executor alone. A forced run rebuilt it and reported the rebuild in
      `ns_prepare`; that is exactly what must NOT be counted.
    - `RANGE_ACQUIRES` -- acquire only estimated. Dispatch pays the build itself, whether that is
      `prepare_candidates` on a lazy materialize or `build_card_range_bits` for `CardRangePopcount`, so
      `ns_prepare` IS part of the plan's cost and is added.

    Why this is better than the subtraction it replaces: an addition cannot overshoot, so the guard
    that dropped rows is gone, and with it the reason `bench_regret_matrix` was skipping queries. That
    guard cost 39% of all queries as a fraction and 1.8% as an absolute floor; it is now 0%.

    Returns:
        The plan's dispatch nanoseconds, or None if it produced no page (declined, or never ran).
    """
    if not plan["trials_ns"]:
        return None
    dispatch = float(plan["ns_setup"] + plan["ns_loop"] + plan["ns_finish"])
    if dispatch <= 0:
        return None  # ran but published no phase: cannot be priced, and must not read as zero
    if acquire["count_source"] in RANGE_ACQUIRES:
        dispatch += float(plan["ns_prepare"])
    return dispatch


# ── statistics and tables ─────────────────────────────────────────────────────────────────────


def percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile; no interpolation, so every printed number is a real observation."""
    if not sorted_vals:
        return float("nan")
    idx = min(math.ceil(pct / 100.0 * len(sorted_vals)) - 1, len(sorted_vals) - 1)
    return sorted_vals[max(idx, 0)]


def spread(sorted_vals: list[float]) -> float:
    """p90/p10 -- flat means a uniform rate error, wide means something unmodelled."""
    lo = percentile(sorted_vals, 10)
    return percentile(sorted_vals, 90) / lo if lo > 0 else float("inf")


#: Ranking strategies for `percentile_table`, by what puts the interesting cell first.
BY_COUNT = ("n", lambda vals: -len(vals))
BY_MISCALIBRATION = ("worst-calibrated", lambda vals: -abs(math.log(max(percentile(sorted(vals), 50), 1e-9))))
BY_TOTAL = ("share of total", lambda vals: -sum(vals))


def percentile_table(  # noqa: PLR0913 - a table renderer's arguments ARE its output format
    rows: list[dict],
    key: Callable[[dict], object],
    label: str,
    *,
    value: str = "ratio",
    rank: tuple[str, Callable[[list[float]], float]] = BY_COUNT,
    limit: int = 40,
    min_rows: int = MIN_ROWS,
    annotate: Callable[[list[float]], str] | None = None,
) -> None:
    """Print one grouping of `rows` as a nearest-rank percentile table.

    Args:
        rows: collected rows, each carrying `value` and whatever `key` reads.
        key: the slice -- what goes in the leftmost column.
        label: header for that column.
        value: which field to build the distribution from.
        rank: one of `BY_COUNT` / `BY_MISCALIBRATION` / `BY_TOTAL`, deciding row order.
        limit: how many cells to print.
        min_rows: cells thinner than this are dropped as noise.
        annotate: optional trailing column, given the sorted values.
    """
    groups: dict[object, list[float]] = collections.defaultdict(list)
    for r in rows:
        groups[key(r)].append(r[value])
    kept = {name: sorted(vals) for name, vals in groups.items() if len(vals) >= min_rows}
    if not kept:
        print(f"\n{label}: no cell reached {min_rows} samples")
        return
    head = "".join(f"{f'p{p}':>8}" for p in PERCENTILES)
    print(f"\n{label:<44}{'n':>7}{head}{'p90/p10':>9}   [{rank[0]} first]")
    for name, vals in sorted(kept.items(), key=lambda kv: rank[1](kv[1]))[:limit]:
        cells = "".join(f"{percentile(vals, p):>8.2f}" for p in PERCENTILES)
        tail = annotate(vals) if annotate else ""
        print(f"{name!s:<44}{len(vals):>7}{cells}{spread(vals):>9.1f}{tail}")


def paired_bootstrap(deltas: list[float]) -> tuple[float, float]:
    """Central `BOOTSTRAP_CI` interval for the mean of `deltas`, by resampling with replacement."""
    rng = random.Random(0)  # fixed: the interval should not wobble between reads of the same data
    n = len(deltas)
    means = sorted(sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(BOOTSTRAP_RESAMPLES))
    tail = (1.0 - BOOTSTRAP_CI) / 2.0
    return means[int(tail * BOOTSTRAP_RESAMPLES)], means[int((1.0 - tail) * BOOTSTRAP_RESAMPLES) - 1]


# ── paired A/B across two builds ──────────────────────────────────────────────────────────────


def write_rows(path: pathlib.Path, rows: list[dict]) -> None:
    """Write per-observation rows as JSONL, for a later `--compare`."""
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    print(f"wrote {len(rows):,} rows to {path}")


def read_keyed(path: pathlib.Path, key_fields: tuple[str, ...], value: str) -> dict[tuple, float]:
    """Read a JSONL run back as {identity: value}, for pairing against another run."""
    out: dict[tuple, float] = {}
    for line in path.open():
        r = json.loads(line)
        out[tuple(r[k] for k in key_fields)] = r[value]
    return out


def report_paired(  # noqa: PLR0913 - a print function's arguments ARE its output
    a: dict[tuple, float],
    b: dict[tuple, float],
    *,
    unit: str,
    label_a: str,
    label_b: str,
    noise_floor: float = NOISE_FLOOR_US,
) -> None:
    """Compare two runs over the observations they have in common.

    Never compares two headline means: those are heavy-tailed enough that the same engine and seed
    have produced 0.26 and 0.82 µs on consecutive runs. Pairing over identical observations removes
    the sampling variance, and the bootstrap interval says whether what remains is real.
    """
    shared = sorted(set(a) & set(b))
    if not shared:
        print("no observations in common -- both runs need the same --mode/--sample/--seed")
        return
    deltas = [b[k] - a[k] for k in shared]
    lo, hi = paired_bootstrap(deltas)
    mean_a = statistics.fmean(a[k] for k in shared)
    mean_b = statistics.fmean(b[k] for k in shared)
    ratios = sorted(b[k] / a[k] for k in shared if a[k] > 0)
    worse = sum(1 for d in deltas if d > noise_floor)
    better = sum(1 for d in deltas if d < -noise_floor)

    print(f"\npaired over {len(shared):,} observations in common ({len(a):,} / {len(b):,} recorded)")
    print(f"  A  {mean_a:>10.2f} {unit}   median {statistics.median(a[k] for k in shared):>9.2f} {unit}   ({label_a})")
    print(f"  B  {mean_b:>10.2f} {unit}   median {statistics.median(b[k] for k in shared):>9.2f} {unit}   ({label_b})")
    print(f"  B - A {mean_b - mean_a:>+9.2f} {unit}   {BOOTSTRAP_CI:.0%} CI [{lo:+.2f}, {hi:+.2f}]")
    if ratios:
        print(
            f"  per-observation B/A: median {statistics.median(ratios):.3f}   p10 {ratios[len(ratios) // 10]:.3f}   p90 {ratios[len(ratios) * 9 // 10]:.3f}"
        )
    print(f"  slower on {worse:,}, faster on {better:,}, within ±{noise_floor:g}{unit} on {len(shared) - worse - better:,}")
    verdict = "NO DETECTABLE DIFFERENCE (interval spans zero)" if lo <= 0.0 <= hi else ("B is SLOWER" if lo > 0 else "B is FASTER")
    print(f"  verdict: {verdict}")
