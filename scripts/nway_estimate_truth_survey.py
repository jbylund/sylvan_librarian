r"""Estimate-vs-truth survey for `compose_printing_estimate`'s `And` arm, across curated leaf shapes.

Companion measurement harness for the N-way `And` composition arc
(docs/issues/local-engine-nway-compose-independence-search.md). That doc is a design for a
partition-search estimator; every piece of evidence behind it so far came from throwaway,
hand-rolled scratchpad scripts (`pairdiag_survey.py`, `tripdiag_survey.py`, and similar), each built
per round and discarded. This is the durable, checked-in replacement: a deterministic, shape-tagged
query catalog, plus a `--compare` mode so two isolated builds (before/after an estimator change) can
be diffed quickly.

Two engine calls per (query, unique) pair, both already published, nothing new needed on the Rust
side to run this script:

  - ESTIMATE: `engine.explain(**kw)` -- the cheap, no-execution acquire pass. `acquire["matches"]` is
    the predicted cardinality; `plans` already carries each plan's `picked` bool, computed by the
    real router with no execution at all -- free routing-decision data.
  - TRUTH:    `engine.explain_analyze(num_warmups=0, num_trials=1, **kw)` -- forces every applicable
    plan to actually run once. `result_total` is ground truth for the query (lib.rs's own doc:
    "so a harness can check the model against what happened"), independent of `limit`/`offset`.

Primary comparison metric is plan-choice agreement, not raw ratio -- see `PLAN_AGREEMENT_DOC` below
for why. Ratio is a secondary, floored diagnostic for locating where the estimator is loose.

    # once per build -- point --engine-dir at a `maturin build --release -o <dir>` + unzip
    .venv/bin/python scripts/nway_estimate_truth_survey.py --engine-dir /tmp/wheel-main \\
        --n-per-shape 300 --seed 0 --out /tmp/main_truth.jsonl
    .venv/bin/python scripts/nway_estimate_truth_survey.py --engine-dir /tmp/wheel-round38 \\
        --n-per-shape 300 --seed 0 --out /tmp/round38_truth.jsonl
    .venv/bin/python scripts/nway_estimate_truth_survey.py --compare /tmp/main_truth.jsonl /tmp/round38_truth.jsonl
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json
import math
import pathlib
import random
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# ── measurement knobs ─────────────────────────────────────────────────────────────────────────
# This harness grades CORRECTNESS (predicted cardinality vs. real total, and picked-plan agreement),
# not TIMING -- unlike every other costbench-derived harness, which needs enough trials to read a
# reliable nanosecond figure. `result_total` and `picked` are deterministic for a given query/build,
# so one untimed round is enough; extra trials would only slow down the 10k-scale iteration loop this
# harness exists for. Real regret-in-nanoseconds confirmation is `bench_pairwise_ordering.py` /
# `bench_plan_misselection.py`'s job, at their own (much higher) trial counts, run only on whatever
# subset of queries this harness flags as having a different `picked_plan` between two builds.
NUM_WARMUPS = 0
NUM_TRIALS = 1
# `orderby`/`direction`/`offset` are pinned -- paging correctness is not what this harness grades.
ORDERBY = "name"
DIRECTION = "asc"
OFFSET = 0
LIMIT = 10
# Every space the "shared witness" AND semantics can diverge in (Round 34's Ge/Le asymmetry, Round
# 35's "exact in all three modes" finding) -- every query is measured in all three, not one sampled.
UNIQUES = ("card", "printing", "artwork")
# Same floor `bench_feature_accuracy.py` uses and justifies (its `MIN_COUNTER = 100` -- named rather
# than cited by line number, which went stale the first time that file grew a CLI flag): "Below this
# the counter is too small for a ratio to mean anything --
# a feature of 3 against a counter of 1 is a 3x error that costs nanoseconds." Reused verbatim, not
# reinvented -- rows below this are still written to the JSONL, just excluded from ratio tables.
MIN_TRUE_FOR_RATIO = 100
# Retries per shape before giving up on reaching --n-per-shape (a narrow shape's pool can be smaller
# than the target -- mirrors QuerySampler's own MAX_FAMILY_DRAWS safety valve).
MAX_RETRY_FACTOR = 8

PLAN_AGREEMENT_DOC = """
Raw ratio is a poor proxy for what actually matters: predicted=1 against true_total=0 reads as
"infinitely wrong" yet is completely benign, and predicted=29,000 against true_total=31,000 reads as
"6.9% off" and is ALSO completely benign, for the same underlying reason -- neither is anywhere near
a threshold that would change which plan the router picks. So the primary --compare signal is
plan-choice agreement (see `picked_plan`), computed at zero extra engine-call cost from explain()'s
own `picked` bool; ratio is graded second, as a floored diagnostic for locating where the estimator
is loose, not as the success bar.
"""


# ── curated shape catalog ──────────────────────────────────────────────────────────────────────
# (label, families tuple) -- families are the QuerySampler dedupe unit (one predicate per family per
# query), so a tuple here is exactly one flat conjunction of that many distinct-family leaves.
UNSAFE_PAIRS: tuple[tuple[str, str], ...] = (
    ("legality", "released"),
    ("legality", "set"),
    ("color", "type"),
    ("keyword", "type"),
    ("set", "type"),
    ("identity", "set"),
    ("pow", "set"),
    ("color", "identity"),
    ("usd", "eur"),
    ("usd", "tix"),
    ("eur", "tix"),
)
SAFE_PAIRS: tuple[tuple[str, str], ...] = (
    ("legality", "cn"),
    ("legality", "usd"),
    ("identity", "usd"),
    ("color", "usd"),
    ("cmc", "usd"),
    ("type", "released"),
    ("type", "usd"),
)
TRIPLES: tuple[tuple[str, str, str], ...] = (
    ("color", "legality", "type"),  # the verified color:G format:pioneer t:elf shape
    ("color", "type", "cmc"),
    # Round 42: `set:X` + a subtype leaf + a residual usd/cn bound. `set:` (unlike `color:`/`id:`) has
    # no `compile_plane` arm at all, so it can never get swept into a `compile_plane` joint alongside
    # another plane-compilable leaf the way `color`+`legality` above can (confirmed directly: on the
    # real corpus, `color:G format:pioneer t:elf`'s dim leaf gets covered by compile_plane's own
    # color+legality joint before SubtypePairIndexes's residual scan ever runs, so that curated triple
    # does not exercise this round's fix at all -- see the round's own PR notes). This shape is the one
    # that actually reaches the new residual-scan code path uncontested.
    ("set", "type", "usd"),
    ("set", "type", "cn"),
)
ARITH_SHAPES: tuple[tuple[str, ...], ...] = (
    ("arith",),
    ("arith", "type"),
    ("arith", "identity"),  # format:modern id:g t:creature power+toughness>cmc+cmc shape
    ("pow", "tou", "cmc"),  # the plain multi-leaf case ArithTupleIndex is supposed to absorb
)
SUBTYPE_CUBE_SHAPES: tuple[tuple[str, ...], ...] = (
    ("type", "pow"),
    ("type", "tou"),
    ("type", "cmc"),
    ("type", "pow", "tou"),
    ("type", "pow", "tou", "cmc"),  # Round 36's own shape
    # Round 48: `SubtypeArithBox` generalized past its old "subtype + arith leaves, nothing else"
    # gate to scan the residual, mirroring `SubtypePairIndexes`'s own Round 42 generalization (see
    # that round's own `("set", "type", "usd")` entry above for the identical shape of fix on a
    # different mechanism). This is the round's own motivating case (`t:elf cmc>=5 usd<10`): a
    # subtype leaf, one arith bound, AND an unrelated residual price leaf -- the old gate
    # (`arith_children.len() + 1 == v.len()`) declined this outright since a 4th, unrelated family is
    # present.
    ("type", "cmc", "usd"),
)
CN_SET_SHAPES: tuple[tuple[str, ...], ...] = (("cn", "set"),)
# "Star" 3-leaf shapes: two of a hub class's registered partners plus the hub itself, where the
# partner PAIR itself is NOT a registered-safe pair -- so `independence_safe_pair`'s residual scan
# (lib.rs, ~9705-9739) fires TWO separate independence candidates simultaneously (hub x partnerA,
# hub x partnerB) and `.min()`-folds both into `result`, rather than one candidate the way every
# existing `TRIPLES`/pair shape above exercises. Neither pairwise calibration round (38: color/
# identity/cmc x price; 40: legality/type x {cn,released,usd}, id/pow x set) ever measured this
# "two simultaneous independence estimates" composition -- that's what this catalog is for; see
# docs/issues/local-engine-nway-compose-independence-search.md's "Triple-level (3+-leaf) independence
# safety" open item.
#
# `Price` (the `usd` hub) has 5 registered partners (`Legality`/`ColorId`/`ColorIdentity`/`Cmc`/
# `Type` -- families `legality`/`color`/`identity`/`cmc`/`type`): all 10 pairs of those 5 taken 2 at
# a time. `SetCode` (the `set` hub) has 2 registered partners (`ColorIdentity`/`Pow` -- families
# `identity`/`pow`): its only pair.
STAR_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("legality", "color", "usd"),
    ("legality", "identity", "usd"),
    ("legality", "cmc", "usd"),
    ("legality", "type", "usd"),
    ("color", "identity", "usd"),
    ("color", "cmc", "usd"),
    ("color", "type", "usd"),
    ("identity", "cmc", "usd"),
    ("identity", "type", "usd"),
    ("cmc", "type", "usd"),
    ("identity", "pow", "set"),
)
# Predicate counts for the broad/pathological catch-all -- unrestricted families, no per-family
# tagging (that detail isn't the point here; breadth and higher-N residual behavior is).
BROAD_PREDICATE_COUNTS = range(1, 9)

# STRUCTURES template name for a given predicate count (client/query_sampler.py's STRUCTURES dict).
# Only single/and2/and3/and4 exist; every curated spec above has 1-4 families, so this always resolves.
ARITY_TO_STRUCTURE = {1: "single", 2: "and2", 3: "and3", 4: "and4"}
# Which STRUCTURES root at And vs Or (client/query_sampler.py's STRUCTURES has 12 named shapes).
STRUCTURE_ROOT = {
    "single": "leaf",
    "and2": "and",
    "and3": "and",
    "and4": "and",
    "and-of-ors": "and",
    "and-or": "and",
    "neg-and": "and",
    "neg-or": "and",
    "regex": "and",
    "or2": "or",
    "or3": "or",
    "paren-or": "or",
}


def all_family_specs() -> list[tuple[str, tuple[str, ...]]]:
    """Every curated AND-rooted spec: (label, families)."""
    from client.query_sampler import REALISTIC_FAMILY_WEIGHTS  # noqa: PLC0415

    specs: list[tuple[str, tuple[str, ...]]] = [(f"singleton:{fam}", (fam,)) for fam in REALISTIC_FAMILY_WEIGHTS]
    specs += [(f"unsafe:{a}+{b}", (a, b)) for a, b in UNSAFE_PAIRS]
    specs += [(f"safe:{a}+{b}", (a, b)) for a, b in SAFE_PAIRS]
    specs += [(f"triple:{'+'.join(t)}", t) for t in TRIPLES]
    specs += [(f"star:{'+'.join(t)}", t) for t in STAR_SHAPES]
    specs += [(f"arith:{'+'.join(t)}", t) for t in ARITH_SHAPES]
    specs += [(f"subtype_cube:{'+'.join(t)}", t) for t in SUBTYPE_CUBE_SHAPES]
    specs += [(f"cn_set:{'+'.join(t)}", t) for t in CN_SET_SHAPES]
    return specs


def or_rooted_specs() -> list[tuple[str, tuple[str, ...], str]]:
    """A modest OR-rooted slice of the pair catalog, for a baseline -- see the Or-arm note below.

    `compose_printing_estimate`'s `Or` arm (lib.rs:8908-8927) is a naive sum-of-children clamped to
    the domain -- a union upper bound, exact only when children are disjoint, structurally different
    from And's intersection/independence problem and out of scope for the partition-search design
    this harness measures. Capturing a baseline costs nothing and saves a future Or-focused round
    from starting blind. NOTE: Or's arm recurses into `compose_printing_estimate` per child, so an
    And-only fix can still shift these rows (a paren-or child is itself a 2-leaf And) -- these rows
    are not a true negative control; they should hold steady or improve, never regress.
    """
    return [(f"OR:{a}+{b}", (a, b), "or2") for a, b in (*UNSAFE_PAIRS, *SAFE_PAIRS)]


# ── same-categorical-family-twice supplement ───────────────────────────────────────────────────
# QuerySampler cannot draw two INDEPENDENT values from the same family (one predicate per family per
# query, by design) -- confirmed by reading `predicate()`'s generic branch (query_sampler.py:706),
# which calls `_pick(family, rng)` exactly once per slot. `set:X set:Y` (Round 35's disjointness
# shape) and `t:X t:Y` (a real query -- "find the human wizards", not just an adversarial probe) both
# need this. No dedicated subtype x subtype MECHANISM is proposed here -- this only generates the
# shape and measures it; whether it needs one, the way set x set did, is a decision this harness's
# own output should drive.
MIN_SUBTYPE_SOLO_COUNT = 20  # a subtype must appear on at least this many cards to be worth pairing
MIN_DISTINCT_SET_CODES = 2  # need at least a pair to draw two distinct ones


class CorpusVocab:
    """Real values mined directly from the corpus JSONL, for shapes QuerySampler cannot draw."""

    def __init__(self, corpus: pathlib.Path) -> None:
        """Scan `corpus` once for set codes and subtype co-occurrence/disjoint pairs."""
        set_codes: collections.Counter[str] = collections.Counter()
        subtype_solo: collections.Counter[str] = collections.Counter()
        subtype_pairs: collections.Counter[tuple[str, str]] = collections.Counter()
        with corpus.open() as fh:
            for line in fh:
                row = json.loads(line)
                if code := row.get("card_set_code"):
                    set_codes[code] += 1
                subtypes = sorted(set(row.get("card_subtypes") or []))
                for s in subtypes:
                    subtype_solo[s] += 1
                for a, b in itertools.combinations(subtypes, 2):
                    subtype_pairs[(a, b)] += 1
        self.set_codes = [c for c, _ in set_codes.most_common(200)]
        frequent = [s for s, n in subtype_solo.items() if n >= MIN_SUBTYPE_SOLO_COUNT]
        # Real co-occurrence, weighted by how often the pair actually appears together -- not a
        # hand-picked "sounds tribal" guess.
        self.cooccurring_subtype_pairs = [p for p, _ in subtype_pairs.most_common(100)]
        # Data-driven "known disjoint": both individually common, but the pair never co-occurs in the
        # real corpus -- computed, not guessed (e.g. a common creature subtype vs. a common land
        # subtype will typically land here).
        seen_pairs = set(subtype_pairs)
        self.disjoint_subtype_pairs = [(a, b) for a, b in itertools.combinations(sorted(frequent), 2) if (a, b) not in seen_pairs][
            :100
        ]
        if not self.cooccurring_subtype_pairs or not self.disjoint_subtype_pairs or len(self.set_codes) < MIN_DISTINCT_SET_CODES:
            msg = "corpus too small/sparse to mine same-family-twice supplement queries"
            raise RuntimeError(msg)


def same_family_twice_queries(vocab: CorpusVocab, rng: random.Random, n: int) -> Iterator[tuple[str, str, str]]:
    """Yield (label, structure, query) for the set x set and t x t hand-written supplement."""
    for _ in range(n):
        a, b = rng.sample(vocab.set_codes, 2)
        yield ("same_family:set+set", "and2", f"set:{a} set:{b}")
    for _ in range(n):
        a, b = rng.choice(vocab.cooccurring_subtype_pairs)
        yield ("same_family:type+type_realistic", "and2", f"t:{a.lower()} t:{b.lower()}")
    for _ in range(n):
        a, b = rng.choice(vocab.disjoint_subtype_pairs)
        yield ("same_family:type+type_disjoint", "and2", f"t:{a.lower()} t:{b.lower()}")


# ── query generation ───────────────────────────────────────────────────────────────────────────


class GeneratedQuery:
    """One generated query string plus the shape metadata it was drawn from."""

    __slots__ = ("families", "q", "root", "shape_label", "structure")

    def __init__(self, q: str, shape_label: str, structure: str, root: str, families: str) -> None:
        """Store a generated query alongside the catalog metadata that produced it."""
        self.q = q
        self.shape_label = shape_label
        self.structure = structure
        self.root = root
        self.families = families


def generate_queries(sampler: object, vocab: CorpusVocab, seed: int, n_per_shape: int) -> list[GeneratedQuery]:
    """The full deterministic catalog: curated AND shapes, an OR baseline slice, and the supplement."""
    from client.query_sampler import Shape  # noqa: PLC0415

    out: list[GeneratedQuery] = []

    def draw(label: str, shape: Shape, n: int) -> None:
        # `structured_query` silently DEGRADES to fewer predicates when a low-relative-weight family
        # loses too many reject-sampling draws within its retry budget (e.g. `cn`, weight 0.5, against
        # `legality`, weight 8 -- verified directly: ~35-40% of `legality+cn` draws come back as a
        # bare `f:...` singleton, not a bug, just how skewed REALISTIC_FAMILY_WEIGHTS interacts with
        # QuerySampler's own retry cap). A spec that named N families should measure N-leaf queries,
        # not silently accept a degrade -- so a returned `structure` outside what was asked for is
        # treated as a miss and retried, exactly like a duplicate or a parser reject.
        rng = random.Random(f"{seed}:{label}")
        seen: set[str] = set()
        attempts = 0
        while len(seen) < n and attempts < n * MAX_RETRY_FACTOR:
            attempts += 1
            drawn = sampler.structured_query(rng, shape=shape)
            q = drawn["query"]
            if not q or q in seen or (shape.structures is not None and drawn["structure"] not in shape.structures):
                continue
            seen.add(q)
            root = STRUCTURE_ROOT.get(drawn["structure"], "and")
            out.append(GeneratedQuery(q=q, shape_label=label, structure=drawn["structure"], root=root, families=drawn["families"]))

    for label, families in all_family_specs():
        structure = ARITY_TO_STRUCTURE[len(families)]
        draw(label, Shape(families=frozenset(families), predicates=len(families), structures=frozenset({structure})), n_per_shape)

    for label, families, structure in or_rooted_specs():
        draw(
            label,
            Shape(families=frozenset(families), predicates=len(families), structures=frozenset({structure})),
            max(n_per_shape // 3, 10),
        )

    # OR baseline beyond pairs: a small fixed quota of unrestricted or3/paren-or queries.
    draw("OR:baseline_or3", Shape(structures=frozenset({"or3"})), max(n_per_shape // 5, 10))
    draw("OR:baseline_paren_or", Shape(structures=frozenset({"paren-or"})), max(n_per_shape // 5, 10))

    # Broad/pathological catch-all: unrestricted families, no structured_query (and5+ isn't a real
    # STRUCTURES template), so this uses the plain flat-conjunction method and a synthetic label.
    for k in BROAD_PREDICATE_COUNTS:
        rng = random.Random(f"{seed}:broad:{k}")
        seen: set[str] = set()
        attempts = 0
        while len(seen) < n_per_shape and attempts < n_per_shape * MAX_RETRY_FACTOR:
            attempts += 1
            q = sampler.query(rng, shape=Shape(predicates=k))
            if not q or q in seen:
                continue
            seen.add(q)
            out.append(
                GeneratedQuery(q=q, shape_label=f"broad:n{k}", structure=f"flat_and{k}", root="and", families="unrestricted")
            )

    rng = random.Random(f"{seed}:same_family")
    for label, structure, q in same_family_twice_queries(vocab, rng, n_per_shape):
        out.append(GeneratedQuery(q=q, shape_label=label, structure=structure, root="and", families=label))

    return out


# ── measurement ─────────────────────────────────────────────────────────────────────────────────


def tree_mechanisms(node: dict | None) -> list[str]:
    """Every `joint_lookup`/`independence` mechanism used anywhere in an `and_trace.tree`, root-to-leaf order.

    Used to derive a cheap, bucketable summary (`and_mechanism`) from the tree without the harness
    needing to understand every `op` value Round 38+ might add later -- it only ever looks for the
    one thing that's stable across the whole arc: which mechanism(s), if any, actually tightened
    something. A bare `min_fold` over plain leaves (nothing tightened) yields an empty list.

    Round 38's `"independence"` op carries no `mechanism` string of its own (the op name already says
    what happened -- there's exactly one independence formula, unlike `joint_lookup`'s several named
    table/scan mechanisms), so this records the literal string `"Independence"` in its place -- the
    same bucketable-summary role `joint_lookup`'s own `mechanism` field plays.
    """
    if node is None or node["kind"] == "leaf":
        return []
    op = node.get("op")
    if op == "joint_lookup":
        out = [node["mechanism"]]
    elif op == "independence":
        out = ["Independence"]
    else:
        out = []
    for child in node.get("children", []):
        out += tree_mechanisms(child)
    return out


def measure_one(engine: object, gq: GeneratedQuery, unique: str, parse_scryfall_query: object) -> dict | None:
    """One (query, unique) row: estimate + ground truth + plan-choice, or None on a parser reject."""
    try:
        filters = parse_scryfall_query(gq.q)
    except Exception:  # noqa: BLE001 - a rejected query is a skipped sample, same as costbench
        return None
    kw = {"filters": filters, "unique": unique, "orderby": ORDERBY, "direction": DIRECTION, "limit": LIMIT, "offset": OFFSET}
    try:
        quick = engine.explain(**kw)
        analyzed = engine.explain_analyze(num_warmups=NUM_WARMUPS, num_trials=NUM_TRIALS, prefer="default", **kw)
    except Exception:  # noqa: BLE001
        return None

    acquire = quick["acquire"]
    predicted = acquire.get("matches")
    picked = next((p["plan"] for p in quick["plans"] if p["picked"]), None)

    ran = [p for p in analyzed["plans"] if p["trials_ns"]]
    totals = {p["result_total"] for p in ran}
    if len(totals) > 1:
        print(f"WARNING: plans disagree on result_total for {gq.q!r} [{unique}]: {sorted(totals)}", file=sys.stderr)
    true_total = next(iter(totals), None) if totals else None
    if predicted is None or true_total is None:
        return None

    row = {
        "q": gq.q,
        "unique": unique,
        "shape_label": gq.shape_label,
        "structure": gq.structure,
        "root": gq.root,
        "families": gq.families,
        "predicted_matches": predicted,
        "count_source": acquire.get("count_source"),
        "and_trace": acquire.get("and_trace"),  # None until Round 37a ships
        # Every mechanism that actually tightened something in the tree, "+"-joined -- "" until
        # Round 37a ships, or when nothing tightened at all (a bare min_fold over plain leaves).
        "and_mechanism": "+".join(tree_mechanisms((acquire.get("and_trace") or {}).get("tree"))),
        # Round 39: single-shot wall time (ns) of the real, production, acquire-time
        # `compose_printing_estimate` call -- None whenever this query's acquire took a branch
        # other than `PrintingCompose` (not "ran in 0ns"). The baseline a future round grades the
        # general partition-search estimator's own "tax" against.
        "and_estimate_ns": acquire.get("and_estimate_ns"),
        "picked_plan": picked,
        "true_total": true_total,
        "n_plans_ran": len(ran),
        "ratio": None,
        "abs_log_ratio": None,
        "predicted_is_also_zero": None,
    }
    if true_total == 0:
        row["predicted_is_also_zero"] = predicted == 0
    else:
        row["ratio"] = predicted / true_total
        row["abs_log_ratio"] = abs(math.log(max(predicted, 1e-9) / true_total))
    return row


def collect(engine: object, queries: list[GeneratedQuery], parse_scryfall_query: object) -> list[dict]:
    """Measure every generated query in every space, dropping parser-rejected/undecidable rows."""
    rows: list[dict] = []
    for i, gq in enumerate(queries):
        for unique in UNIQUES:
            if (row := measure_one(engine, gq, unique, parse_scryfall_query)) is not None:
                rows.append(row)
        if (i + 1) % 500 == 0:
            print(f"  ...{i + 1:,}/{len(queries):,} queries measured, {len(rows):,} rows so far", flush=True)
    return rows


# ── comparison ──────────────────────────────────────────────────────────────────────────────────


def load_rows(path: pathlib.Path) -> list[dict]:
    """Read a JSONL run written by this script's `--out` mode."""
    return [json.loads(line) for line in path.open()]


def key_of(row: dict) -> tuple:
    """Identity for pairing this row across two `--compare` runs."""
    return (row["q"], row["unique"])


def plan_agreement_table(a: list[dict], b: list[dict]) -> None:
    """Headline table: for shared (q, unique), did the picked plan change between builds?"""
    a_by_key = {key_of(r): r for r in a}
    shared = [(a_by_key[k], r) for r in b if (k := key_of(r)) in a_by_key]
    if not shared:
        print("plan agreement: no observations in common")
        return
    print(f"\nPLAN-CHOICE AGREEMENT over {len(shared):,} shared (query, unique) observations")
    print(PLAN_AGREEMENT_DOC)
    buckets: dict[str, list[tuple[bool, dict, dict]]] = collections.defaultdict(list)
    for ra, rb in shared:
        same = ra["picked_plan"] == rb["picked_plan"]
        buckets["ALL"].append((same, ra, rb))
        buckets[f"root={rb['root']}"].append((same, ra, rb))
        buckets[f"shape={rb['shape_label']}"].append((same, ra, rb))
    header = f"{'bucket':<44}{'n':>8}{'same':>8}{'changed':>9}{'changed %':>11}"
    print(header)
    for name, vals in sorted(buckets.items(), key=lambda kv: -sum(1 for same, *_ in kv[1] if not same)):
        n = len(vals)
        changed = sum(1 for same, *_ in vals if not same)
        # ALL/root rows always print (the headline shape of the change); a per-shape row only prints
        # when it actually has a flip -- 88+ shape labels reading 0 changed every time is exactly the
        # noise this table exists to cut through, per the plan's "visible rather than averaged away"
        # intent (which is about SURFACING a regression, not printing every unchanged cell).
        if changed == 0 and name.startswith("shape="):
            continue
        print(f"{name:<44}{n:>8}{n - changed:>8}{changed:>9}{changed / n:>10.1%}")
        if changed and name.startswith("shape="):
            for same, ra, rb in vals:
                if not same:
                    print(f"    {rb['q']!r} [{rb['unique']}]: {ra['picked_plan']} -> {rb['picked_plan']}")


def ratio_paired_diff(a: list[dict], b: list[dict]) -> None:
    """Secondary, floored: did the accuracy diagnostic (abs_log_ratio) get better or worse."""
    from scripts import costbench  # noqa: PLC0415

    a_by_key = {
        key_of(r): r["abs_log_ratio"] for r in a if r["abs_log_ratio"] is not None and r["true_total"] >= MIN_TRUE_FOR_RATIO
    }
    shared = [
        (a_by_key[k], r["abs_log_ratio"])
        for r in b
        if r["abs_log_ratio"] is not None and r["true_total"] >= MIN_TRUE_FOR_RATIO and (k := key_of(r)) in a_by_key
    ]
    if not shared:
        print(f"\nratio diagnostic: no observations in common with true_total >= {MIN_TRUE_FOR_RATIO}")
        return
    deltas = [db - da for da, db in shared]
    lo, hi = costbench.paired_bootstrap(deltas)
    print(f"\nRATIO DIAGNOSTIC (abs log ratio, floored at true_total >= {MIN_TRUE_FOR_RATIO}), {len(shared):,} shared rows")
    print(f"  mean A {sum(a for a, _ in shared) / len(shared):.3f}   mean B {sum(b for _, b in shared) / len(shared):.3f}")
    print(f"  B - A {sum(deltas) / len(deltas):+.3f}   95% CI [{lo:+.3f}, {hi:+.3f}]")
    verdict = "no detectable difference" if lo <= 0.0 <= hi else ("B is MORE accurate" if hi < 0 else "B is LESS accurate")
    print(f"  verdict: {verdict}")


def zero_hit_rate_diff(a: list[dict], b: list[dict]) -> None:
    """Compare the disjoint/empty-composition hit rate between two runs."""
    a_by_key = {key_of(r): r["predicted_is_also_zero"] for r in a if r["predicted_is_also_zero"] is not None}
    shared = [
        (a_by_key[k], r["predicted_is_also_zero"])
        for r in b
        if r["predicted_is_also_zero"] is not None and (k := key_of(r)) in a_by_key
    ]
    if not shared:
        print("\nzero-true-count hit rate: no observations in common")
        return
    hit_a = sum(1 for h, _ in shared if h) / len(shared)
    hit_b = sum(1 for _, h in shared if h) / len(shared)
    print(f"\nZERO-TRUE-COUNT HIT RATE ('does the estimator recognize disjoint/empty compositions'), {len(shared):,} rows")
    print(f"  A: {hit_a:.1%}   B: {hit_b:.1%}")


def _worst_median_first(vals: list[float]) -> float:
    """Rank key: highest MEDIAN first, for a metric that is already a non-negative distance.

    `costbench.BY_MISCALIBRATION` computes `abs(log(median))`, which is the right rule for a RATIO
    centered at 1.0 (median exactly 1 -> log 0 -> best). `abs_log_ratio` is not that kind of value --
    it is already `abs(log(ratio))`, a distance centered at 0 where 0 is perfect and bigger is worse.
    Feeding it through `BY_MISCALIBRATION`'s extra `log()` inverts the ordering: a median of exactly
    0.00 produces `log(max(0, 1e-9)) ~= -20.7`, ranking a PERFECT cell as the single worst one --
    confirmed directly against this survey's own first full run, where `unsafe:color+type` (median
    0.00) sorted ahead of `same_family:type+type_realistic` (median 1.12, a real ~3x typical
    overestimate). Don't reuse `BY_MISCALIBRATION` for a value shaped like this one.
    """
    from scripts import costbench  # noqa: PLC0415

    return -costbench.percentile(sorted(vals), 50)


RANK_WORST_ABS_LOG_RATIO = ("worst abs_log_ratio median", _worst_median_first)


def worst_cell_tables(rows: list[dict], label_suffix: str) -> None:
    """Percentile tables of abs_log_ratio by shape/family/structure/mechanism/unique, root=and only.

    Shared between `--compare` (graded on build B, "where did Round 38+ leave the most headroom
    relative to the other build") and `--report` (graded on the one available build, "where is the
    most headroom, full stop") -- the question is the same either way, just against a different
    denominator of runs on hand.
    """
    from scripts import costbench  # noqa: PLC0415

    and_rows = [r for r in rows if r["root"] == "and" and r["abs_log_ratio"] is not None and r["true_total"] >= MIN_TRUE_FOR_RATIO]
    for key, label in (
        (lambda r: r["shape_label"], f"shape_label ({label_suffix})"),
        (lambda r: r["families"], f"families ({label_suffix})"),
        (lambda r: r["structure"], f"structure ({label_suffix})"),
        (lambda r: r["and_mechanism"] or "(no and_trace)", f"and_mechanism ({label_suffix})"),
        (lambda r: r["unique"], f"unique ({label_suffix})"),
    ):
        costbench.percentile_table(
            and_rows, key, label, value="abs_log_ratio", rank=RANK_WORST_ABS_LOG_RATIO, min_rows=costbench.MIN_ROWS
        )


def zero_hit_rate_single(rows: list[dict], label: str) -> None:
    """Single-run zero-true-count hit rate -- the one-build form of `zero_hit_rate_diff`."""
    zero_rows = [r for r in rows if r["predicted_is_also_zero"] is not None]
    if not zero_rows:
        print(f"\nzero-true-count hit rate ({label}): no zero-true rows")
        return
    hit = sum(1 for r in zero_rows if r["predicted_is_also_zero"]) / len(zero_rows)
    print(f"\nZERO-TRUE-COUNT HIT RATE ({label}), {len(zero_rows):,} rows: {hit:.1%}")


def mechanism_coverage_table(rows: list[dict]) -> None:
    """Per shape_label: what fraction of root=and rows had ANY existing mechanism tighten them.

    Complements `worst_cell_tables`: a shape can read a modest abs_log_ratio purely because most of
    its rows are small numbers (where ratio is forgiving) while still having NO real mechanism behind
    it at all (a pure `min_fold` over bare leaves) -- this answers "does this shape have zero coverage
    today" directly, which is exactly the population `considered`'s `hit: false` entries describe and
    the design doc's `groups` field was meant to surface in the first place.
    """
    and_rows = [r for r in rows if r["root"] == "and"]
    buckets: dict[str, list[bool]] = collections.defaultdict(list)
    for r in and_rows:
        buckets[r["shape_label"]].append(bool(r["and_mechanism"]))
    print("\nMECHANISM COVERAGE (fraction of root=and rows where SOME existing mechanism tightened the estimate)")
    print(f"{'shape_label':<36}{'n':>8}{'covered':>10}{'coverage %':>12}")
    for label, vals in sorted(buckets.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
        n = len(vals)
        covered = sum(vals)
        print(f"{label:<36}{n:>8}{covered:>10}{covered / n:>11.1%}")


def compare(path_a: pathlib.Path, path_b: pathlib.Path) -> None:
    """Print the plan-agreement, ratio-diagnostic, and worst-cell tables for two runs."""
    a, b = load_rows(path_a), load_rows(path_b)
    print(f"loaded {len(a):,} rows from {path_a}, {len(b):,} rows from {path_b}")
    plan_agreement_table(a, b)
    ratio_paired_diff(a, b)
    zero_hit_rate_diff(a, b)
    mechanism_coverage_table(b)
    # Which cells are worst in build B RIGHT NOW -- where Round 38+ should look next.
    worst_cell_tables(b, "build B, root=and only")


def report(path: pathlib.Path) -> None:
    """Print a single run's own summary tables -- no second build needed to make use of a run."""
    rows = load_rows(path)
    by_root = collections.Counter(r["root"] for r in rows)
    print(f"loaded {len(rows):,} rows from {path}; by root: {dict(by_root)}")
    zero_hit_rate_single(rows, "single run")
    mechanism_coverage_table(rows)
    worst_cell_tables(rows, "single run")


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """Generate + measure the query catalog (--out), or diff two prior runs (--compare)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine-dir", type=pathlib.Path, default=None, help="isolated maturin build+extract dir")
    parser.add_argument("--out", type=pathlib.Path, default=None)
    parser.add_argument("--compare", nargs=2, type=pathlib.Path, default=None, metavar=("A", "B"))
    parser.add_argument("--report", type=pathlib.Path, default=None, help="summarize one run with no second build to diff against")
    parser.add_argument("--n-per-shape", type=int, default=50, help="queries per curated shape spec (300 for the full survey)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=("realistic", "uniform"), default="realistic")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None)
    args = parser.parse_args()
    sys.path.insert(0, str(REPO_ROOT))  # needed for `from scripts import costbench` in both branches

    if args.compare:
        compare(*args.compare)
        return
    if args.report:
        report(args.report)
        return
    if args.out is None:
        parser.error("--out is required unless --compare/--report is given")

    if args.engine_dir:
        sys.path.insert(0, str(args.engine_dir.resolve()))
    import card_engine  # noqa: PLC0415

    print(f"card_engine: {card_engine.__file__}")

    from api.parsing import parse_scryfall_query  # noqa: PLC0415
    from client.query_sampler import QuerySampler  # noqa: PLC0415
    from scripts import costbench  # noqa: PLC0415

    print("mining corpus vocab for the same-family-twice supplement...")
    vocab = CorpusVocab(args.corpus)
    engine = costbench.load_engine(args.corpus, args.shm_path or args.corpus.with_suffix(".nway.store"))
    sampler = QuerySampler(args.corpus, args.mode)

    print("generating query catalog...")
    queries = generate_queries(sampler, vocab, args.seed, args.n_per_shape)
    print(f"  {len(queries):,} distinct queries across {len({q.shape_label for q in queries}):,} shape specs")

    print("measuring (estimate + ground truth + picked plan, all 3 spaces)...")
    rows = collect(engine, queries, parse_scryfall_query)
    costbench.write_rows(args.out, rows)


if __name__ == "__main__":
    main()
