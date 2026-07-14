# Calibrate Engine Cost Guards via Synthetic-Selectivity Sweeps

The engine's strategy-switch constants were set by judgment, not measurement
(except the memo gate). Calibrate them with the `bench_memo_crossover.py`
pattern: force each branch, sweep workload selectivity via a synthetic column
with exactly known selectivity, find the crossover, then set each guard a
comfortable margin on the *simple-branch* side (prefer full scan over chasing
a thin win too deep).

## Constants in scope

| Constant | Value | Gates | Simple branch |
| --- | --- | --- | --- |
| `MAX_NARROW_FRACTION` / `NARROW_FLOOR` (lib.rs:1335) | 0.25 / 1000 | range index narrows vs declines | full scan |
| `AND_SKIP_THRESHOLD` (lib.rs:1955) | 2048 | And evaluates costlier children vs skips | skip (driver verifies) |
| `STREAM_MIN_MATCHES` (lib.rs:2622) | 1024 | streamed selection vs gather | gather |
| `BITS_PROMOTE` (lib.rs:1668) | 4096 | bitmap scatter vs sorted-vec merge | vec merge |
| `MEMO_DOMAIN_FACTOR` (filter.rs:503) | 2 | already measured (crossover ~1.25x, shipped 2x) — the margin precedent | no memo |

## Step 1 — env-var branch forcing

Convert each `const` to a `LazyLock` static reading `CARD_ENGINE_<NAME>`,
defaulting to the current value. Purpose is not to sweep the constant — it is
to *force* branches: `0` forces one, `usize::MAX` (or `1.0`) the other. One
build, two env settings per guard; fresh subprocess per setting so the
LazyLock reads the right env.

## Step 2 — synthetic corpus

`scripts/build_guard_corpus.py`: start from the real blue-DB JSONL export
(see `bench_bitplanes.py` header). Overwrite, with a recorded seed:

- **`price_usd`** (printing-space knob): a shuffled permutation of
  `0..n_printings`, scaled to `(0,1]`. `usd<x` then matches *exactly*
  `floor(x*N)` printings — a permutation beats iid `random.random()` (zero
  sampling variance). All printings get a price (no nulls).
- **`cmc`** (card-space knob): shuffled card rank quantized to 0.1% steps.
  `cmc<K` matches an exact, dialable card count. Step 0 sanity: confirm load
  time/memory on this corpus is unchanged (cmc is range-indexed, not a
  plane, so high cardinality should be fine — verify).
- **Variant B (correlated)**: `price_usd` derived from cmc rank plus noise.
  Independence makes And intersections multiply exactly; real columns
  correlate, which is precisely where `AND_SKIP_THRESHOLD` earns or wastes
  its keep. Measure both.

## Step 3 — per-guard sweeps

`scripts/bench_cost_guards.py`, CSVs to `benchmarks/cost-guards/`. Median of
k repeats in a fixed time window (as `bench_bitplanes.bench_one`). Every row
records `total`; it must be identical across forced branches — the guards
are speed dials, so parity doubles as a differential test.

- **`MAX_NARROW_FRACTION`**: bare `usd<x`, narrow-always vs narrow-never,
  sweep x in 0.02..1.0. Crossover is a fraction directly.
- **`STREAM_MIN_MATCHES`**: `cmc<K order:name` (precomputed sort perm),
  streamed vs gathered, sweep matched count 64..32768 (log steps).
- **`AND_SKIP_THRESHOLD`**: `cmc<K usd<0.5` — driver is an exact K-card
  set, second child a rank-1 printing range. Include-always vs skip-always,
  sweep K 256..16384. Repeat on the correlated corpus and with a rank-2
  child (`cmc<K -oracle:draw`).
- **`BITS_PROMOTE`**: `usd<x or usd>y` (two dialable sets), bits vs
  vec-merge, sweep combined size around 4096.
- **Memo gate**: optional confirmation rerun on the continuous knob.

## Step 4 — set constants with margin

Do not pick the win-factor by taste — let measured reproducibility pick it.
Near the crossover the curves are close by definition, so a locally wrong
choice is cheap; the real risk is the whole curve shifting (hardware,
distribution, predicate cost). Accordingly:

- Run each sweep 3x (once on a second machine if available; spot-check the
  synthetic crossover against a real-data query at similar selectivity) and
  record the crossover's spread in knob space alongside its location.
- Set each trigger at the most aggressive win-factor whose knob position
  sits clear of that spread. Crisp, reproducible crossover: 1.05-1.1x is
  fine. Wobbly crossover: the spread, not the factor, sets the margin.
- Floor: never place a trigger inside the timing noise of the benchmark
  itself (~5% run-to-run is typical for microsecond queries) — below that
  you are fitting a particular benchmark run, not the cost curve.
- Sanity-check penalty asymmetry from the paired curves: if the fancy
  branch degrades steeply past its own crossover, shade toward the simple
  side regardless of how crisp the measurement is.

## Step 5 — validate on real data

- Rerun the wild-queries corpus (`scripts/survey_queries.py`,
  `benchmarks/wild-queries/`) on the *real* corpus, new vs old constants.
- Full test suite; parity totals across all sweep rows.
- Corpus-size sensitivity: repeat key sweeps at N, N/2, N/4 to learn whether
  each crossover is a count or a fraction — this decides whether constants
  like `AND_SKIP_THRESHOLD` should stay absolute counts.

## Caveats carried from design discussion

- Uniform values are worst-case for locality; thresholds tuned on them bias
  slightly toward the simple branch. Acceptable — it matches the stated
  preference.
- Selectivity is not the only cost input: repeat the And-skip sweep with a
  cheap vs expensive verifier child (numeric vs oracle-regex) before
  trusting one number.
