"""Build synthetic exact-selectivity corpora for cost-guard calibration.

Reads the real blue-DB corpus export (the bench_bitplanes.py JSONL) and
overwrites two knob columns with seeded, exactly-dialable values (see
docs/issues/engine-cost-guard-calibration.md, Step 2):

- ``price_usd`` (printing-space knob): a shuffled permutation of ``1..N``
  scaled to ``(0, 1]``, so ``usd<x`` matches exactly ``ceil(x*N)-1``
  printings — a permutation has zero sampling variance, unlike iid
  ``random.random()``. Every printing gets a price (no nulls).
- ``cmc`` (card-space knob): the card's shuffled rank quantized to 0.1%
  steps (integers ``0..999``, identical across all printings of an
  oracle_id), so ``cmc<K`` matches an exactly dialable card count.

Three corpora are written:

- ``independent.jsonl`` — price permutation independent of cmc rank, so And
  intersections multiply exactly.
- ``correlated.jsonl`` — ``price_usd`` derived from the card's cmc rank plus
  seeded Gaussian noise; real columns correlate, which is where the And-skip
  guard earns or wastes its keep.
- ``independent-half.jsonl`` — the independent variant restricted to cards
  with even rank (half the cards, ~half the printings, same fractional
  selectivities), for the count-vs-fraction sensitivity check.

A ``meta.json`` sidecar records the seed, sizes, and git sha.

    .venv/bin/python scripts/build_guard_corpus.py \
        --corpus ../sylvan_librarian/benchmarks/bitplanes/corpus.jsonl \
        --outdir benchmarks/cost-guards
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

CMC_STEPS = 1_000  # 0.1% quantization steps for the card-space knob
NOISE_SD = 0.05  # correlated-variant Gaussian noise, in price units


def main() -> None:
    """Read the real corpus, rewrite the knob columns, and write all variants."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, required=True, help="real corpus JSONL (read-only)")
    parser.add_argument("--outdir", type=pathlib.Path, default=REPO_ROOT / "benchmarks/cost-guards")
    parser.add_argument("--seed", type=int, default=20260708)
    args = parser.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    # Pass 1: count printings and collect card identities in file order.
    oracle_ids: dict[str, int] = {}  # oracle_id -> first-seen order
    n_printings = 0
    with args.corpus.open() as fh:
        for line in fh:
            row = json.loads(line)
            key = row["oracle_id"] or row["scryfall_id"]
            oracle_ids.setdefault(key, len(oracle_ids))
            n_printings += 1
    n_cards = len(oracle_ids)

    rng = random.Random(args.seed)
    price_perm = list(range(n_printings))
    rng.shuffle(price_perm)
    card_ranks = list(range(n_cards))
    rng.shuffle(card_ranks)
    noise_rng = random.Random(args.seed + 1)

    # Pass 2: write all three variants in one sweep over the file.
    paths = {v: args.outdir / f"{v}.jsonl" for v in ("independent", "correlated", "independent-half")}
    outs = {v: p.open("w") for v, p in paths.items()}
    min_price = 1.0 / n_printings
    with args.corpus.open() as fh:
        for i, line in enumerate(fh):
            row = json.loads(line)
            key = row["oracle_id"] or row["scryfall_id"]
            rank = card_ranks[oracle_ids[key]]
            row["cmc"] = rank * CMC_STEPS // n_cards  # integer 0..999, shared by all printings of the card
            row["price_usd"] = (price_perm[i] + 1) / n_printings
            outs["independent"].write(json.dumps(row) + "\n")
            if rank % 2 == 0:
                outs["independent-half"].write(json.dumps(row) + "\n")
            noisy = rank / n_cards + noise_rng.gauss(0.0, NOISE_SD)
            row["price_usd"] = min(1.0, max(min_price, noisy))
            outs["correlated"].write(json.dumps(row) + "\n")
    for out in outs.values():
        out.close()

    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=REPO_ROOT).stdout.strip()
    meta = {
        "seed": args.seed,
        "source": str(args.corpus),
        "n_printings": n_printings,
        "n_cards": n_cards,
        "cmc_steps": CMC_STEPS,
        "correlated_noise_sd": NOISE_SD,
        "git_sha": sha,
        "variants": {v: p.name for v, p in paths.items()},
    }
    (args.outdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))
    # Sanity: exact match counts are dialable. cmc<K matches ceil(K * n_cards / CMC_STEPS)
    # cards (ranks with rank*CMC_STEPS//n_cards < K), usd<x matches ceil(x*n_printings)-1.
    for k in (1, 10, 100):
        print(f"cmc<{k} should match {sum(1 for r in range(n_cards) if r * CMC_STEPS // n_cards < k)} cards")


if __name__ == "__main__":
    main()
