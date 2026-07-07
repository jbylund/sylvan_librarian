"""Generate the frozen 128-feature table for the flavor-text fingerprint.

Selects 128 ASCII-alpha 1/2/3-grams over the distinct lowercase flavor texts:
greedy selection minimizing residual pass rate over a needle workload sampled
from the corpus vocabulary, with a backfill invariant that reserves enough
tail slots for every unchosen letter of the alphabet — so every possible
needle fires at least one bit and the filter can forgo benefit but never
regress to worse than the letter-mask floor.

The output is the FLAVOR_FP_FEATURES const for card_engine/src/lib.rs.
Staleness only costs selectivity, never correctness (the fingerprint is a
necessary-condition filter, and texts and needles are masked with the same
table), so re-running this is only warranted when measured selectivity drifts.

Usage:
    # export the corpus (or reuse an engine-columns export)
    docker exec sylvan_blue-postgres-1 psql -U foouser -d magic -X -At \
      -c "SELECT row_to_json(t) FROM (SELECT flavor_text FROM magic.cards) t" > /tmp/flavor.jsonl
    python scripts/generate_flavor_fingerprint.py /tmp/flavor.jsonl

Deterministic for a given corpus (fixed seed, sorted tie-breaks).
"""

import json
import math
import random
import re
import sys
from collections import Counter

import numpy as np

BITS = 128
SEED = 7
TRAIN_NEEDLES = 300
VOCAB_POOL = 4000
CANDIDATE_POOL = 1000
MIN_TEXT_DF = 20  # ignore grams rarer than this many texts (too corpus-specific)
MIN_NEEDLE_COVERAGE = 2  # unless the gram is rare enough to be a filter on its own
RARE_DF_FRACTION = 0.15
ALPHABET = set("abcdefghijklmnopqrstuvwxyz")


def grams_of(s: str) -> set:
    """All ASCII-alpha 1/2/3-grams of `s`."""
    out = set()
    for n in (1, 2, 3):
        for i in range(len(s) - n + 1):
            g = s[i : i + n]
            if g.isalpha() and g.isascii():
                out.add(g)
    return out


def load_distinct_texts(corpus_path: str) -> list[str]:
    """Distinct lowercase flavor texts from a JSONL export with a flavor_text field."""
    texts = set()
    with open(corpus_path) as fh:
        for line in fh:
            ft = json.loads(line).get("flavor_text")
            if ft:
                texts.add(ft.lower())
    return sorted(texts)


def candidate_grams(df: Counter, n_texts: int, train: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Grams worth considering.

    Common enough in texts, and either present in the training workload or rare
    enough to filter hard whenever they fire.
    """
    cands = [g for g, c in df.items() if c >= MIN_TEXT_DF]
    train_contains = {g: [w for w in train if g in w] for g in cands}
    cands = [g for g in cands if len(train_contains[g]) >= MIN_NEEDLE_COVERAGE or df[g] / n_texts <= RARE_DF_FRACTION]
    cands.sort(key=lambda g: (-len(train_contains[g]) * -math.log(max(df[g] / n_texts, 1e-6)), g))
    return cands[:CANDIDATE_POOL], train_contains


def select_features(texts: list[str], text_grams: list[set], df: Counter) -> list[str]:
    """Greedy residual selection with the letter-backfill invariant."""
    n_texts = len(texts)
    vocab = Counter()
    for t in texts:
        for w in re.findall(r"[a-z]{4,}", t):
            vocab[w] += 1
    rng = random.Random(SEED)
    words = [w for w, _ in vocab.most_common(VOCAB_POOL)]
    train = rng.sample(words, TRAIN_NEEDLES)

    cands, train_contains = candidate_grams(df, n_texts, train)
    pres = {g: np.zeros(n_texts, dtype=bool) for g in cands}
    for i, gs in enumerate(text_grams):
        for g in gs:
            if g in pres:
                pres[g][i] = True

    masks = {w: np.ones(n_texts, dtype=bool) for w in train}
    chosen: list[str] = []
    pool = list(cands)
    while True:
        uncovered = ALPHABET - {g for g in chosen if len(g) == 1}
        if BITS - len(chosen) <= len(uncovered):
            chosen.extend(sorted(uncovered))
            return chosen
        best_g, best_obj = None, None
        for g in pool:
            delta = 0.0
            for w in train_contains[g]:
                old = masks[w]
                old_n = old.sum()
                if old_n == 0:
                    continue
                delta += math.log(max((old & pres[g]).sum(), 1) / old_n)
            if best_obj is None or delta < best_obj or (delta == best_obj and g < best_g):
                best_g, best_obj = g, delta
        chosen.append(best_g)
        for w in train_contains[best_g]:
            masks[w] &= pres[best_g]
        pool.remove(best_g)


def main(corpus_path: str) -> None:
    """Print the Rust const to stdout, selection stats to stderr."""
    texts = load_distinct_texts(corpus_path)
    print(f"// {len(texts):,} distinct flavor texts", file=sys.stderr)

    df = Counter()
    text_grams = []
    for t in texts:
        gs = grams_of(t)
        text_grams.append(gs)
        df.update(gs)

    chosen = select_features(texts, text_grams, df)
    if len(chosen) != BITS or len(set(chosen)) != BITS:
        msg = f"selection produced {len(chosen)} features ({len(set(chosen))} distinct), expected {BITS}"
        raise RuntimeError(msg)
    n_single = sum(1 for g in chosen if len(g) == 1)
    print(f"// {n_single} single letters, {BITS - n_single} multi-grams", file=sys.stderr)

    print(f"const FLAVOR_FP_FEATURES: [&str; {BITS}] = [")
    for i in range(0, BITS, 8):
        row = ", ".join(f'"{g}"' for g in chosen[i : i + 8])
        print(f"    {row},")
    print("];")


if __name__ == "__main__":
    main(sys.argv[1])
