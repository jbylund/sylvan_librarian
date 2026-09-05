"""Pin the Python copy of `cost::stream_runs_small_gather` against the engine's own derived value.

`bench_stream_card_pass_value.py` recomputes that predicate in Python to classify rows, and a harness
holding its own copy of a gate is exactly the second definition `cost.rs` keeps warning about. This
checks it against something the engine derives itself: `cost::stream_perm_steps` returns 0 whenever
the small-total gather runs, so `model_small => stream_perm_steps == 0` must hold on every row.

One-directional on purpose. The converse is not a bug: `stream_perm_steps` is exposed through a `u32`
cast, so a walk shorter than one entry also reads 0.

    PYTHONPATH=<wheel> .venv/bin/python scripts/check_small_gather_mirror.py --corpus <c> --shm-path <s>
"""

from __future__ import annotations

import argparse
import pathlib
import random
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.query_sampler import MODES, QuerySampler  # noqa: E402
from scripts.bench_stream_card_pass_value import small_gather  # noqa: E402
from scripts.costbench import load_engine, sample_kwargs  # noqa: E402

#: Queries drawn. Enough to reach every acquire route several thousand times.
N_QUERIES = 6000


def main() -> None:
    """Draw queries, and count rows where the Python predicate and the engine's derived value clash."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--shm-path", type=pathlib.Path, required=True)
    parser.add_argument("--mode", choices=MODES, default="uniform")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    from api.parsing import parse_scryfall_query  # noqa: PLC0415

    engine = load_engine(args.corpus, args.shm_path)
    sampler = QuerySampler(args.corpus, args.mode)
    rng = random.Random(args.seed)
    rows = small = violations = offset_mismatch = 0
    for _ in range(N_QUERIES):
        kw = sample_kwargs(sampler, rng, vary_prefer=True)
        try:
            kw["filters"] = parse_scryfall_query(sampler.query(rng))
            acq = engine.explain(**kw)["acquire"]
        except Exception:  # noqa: BLE001, S112 - a rejected query is a skipped sample
            continue
        if not acq:
            continue
        rows += 1
        offset_mismatch += acq["offset"] != kw["offset"]
        if small_gather(float(acq["matches"]), kw["offset"]):
            small += 1
            violations += acq["stream_perm_steps"] != 0
    print(f"rows                                  {rows:,}")
    print(f"PlanFeatures.offset != request offset {offset_mismatch:,}  (must be 0)")
    print(f"python says small-total gather        {small:,}")
    print(f"  of those, stream_perm_steps != 0    {violations:,}  (must be 0)")


if __name__ == "__main__":
    main()
