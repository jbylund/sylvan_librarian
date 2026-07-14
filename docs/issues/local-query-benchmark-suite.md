# Query benchmark suite

A reproducible benchmark harness for comparing query approaches against the full dataset.
Useful for evaluating any schema or query generation change — not specific to any single
optimization.

## Design

**Seeded-random query corpus.** Rather than maintaining a hand-curated list, generate N queries
from a fixed seed using `query_runner.py --generate-corpus`. This gives broader coverage across
all search dimensions (name, type, color, format, price, etc.) while staying fully reproducible —
the same seed produces the same corpus on any branch or machine, so runs are directly comparable.

```bash
# Reproduce the standard 200-query benchmark corpus
python client/query_runner.py --generate-corpus --seed 42 --count 200
```

Each generated entry is a `(query, orderby, unique)` triple drawn from the same weighted
distributions used for load testing, so the mix reflects realistic user traffic. To change the
corpus size or distribution, bump the seed or count; document the change in the benchmark PR.

**Named query generators.** Each generator produces the SQL for a given approach. Connect
directly to PostgreSQL (bypassing the application cache) via the same `PG*` env vars used by
`query_runner.py --realistic`. Start with `distinct_on` (current) and `hashagg` (proposed), add
more as new approaches are evaluated.

**Metrics per query per approach**, collected via `EXPLAIN (ANALYZE, BUFFERS)`:
- Planning time (ms)
- Execution time (ms)
- Total buffer hits (shared hit)
- Dedup node type and memory

**Warm-cache runs only.** Run each query once as a throwaway before timing, so shared buffers are
populated. Report median over N=5 timed runs to reduce noise.

**Output.** Print a side-by-side table and write a CSV to `client/benchmark_results.csv` for
comparison across branches:

```
query                                          | orderby    | unique   | approach    | plan_ms | exec_ms | buffers
format:modern                                  | toughness  | card     | distinct_on |     3.0 |   191.0 |   19913
format:modern                                  | toughness  | card     | hashagg     |     1.2 |   116.0 |   19313
...
```

## Implementation tasks

- [ ] Add `--generate-corpus --seed N --count M` mode to `client/query_runner.py` that prints a
      JSON array of `[query, orderby, unique]` triples and exits.
- [ ] Write `client/benchmark.py` that loads the corpus from `query_runner.generate_corpus()`,
      runs each query through named SQL generators with warm-cache EXPLAIN ANALYZE, and reports
      the results table + CSV.
- [ ] Add a `make benchmark` target that sets `PG*` vars from `.env` and runs it.
- [ ] Add `client/benchmark_results.csv` to `.gitignore` (results are environment-specific).
