# CardRangePopcount extended to collector-number and date ranges (#726)

Follow-on to the bare-`usd` fast path: a bare `cn` or date/`year` range under `unique=card` is now
answered by the same `CardRangePopcount` plan (card-existence bitmap → `popcount` total → page off
the sort permutation) instead of a full scan with a per-card count pass. The plan's gate widened from
usd-only to any bare range leaf `bare_range_bounds` recognizes (usd/collector_number/released_at);
the build takes the range index rather than hardcoding the price index. No new plan and no new
correctness surface — cn/date are printing-varying integer ranges exactly like usd.

Measured on the 97,206-printing corpus (`limit=100`, min of an 8 s window, same build, kill-switch
off vs on):

| query | before | after | speedup |
|---|---:|---:|---:|
| `cn<100` / card | 0.589 ms | 0.088 ms | 6.66× |
| `cn<100` / card, offset 700 | 0.595 ms | 0.092 ms | 6.47× |
| `year>=2015` / card | 0.416 ms | 0.124 ms | 3.36× |
| `year<2005` / card | 0.280 ms | 0.064 ms | 4.40× |

Offset-flat, same as usd. Totals byte-identical off vs on; the usd rows and every control are
unchanged; calibration stays 88/88 gold.

Compound ranges (`usd<50 cn<100`) remain out of scope: composing two printing-varying ranges is a
shared-witness case (`∃p: usd(p) ∧ cn(p)`) that must AND in printing space and project to card space
once — the printing-space plan's structure, not this card-space one's.
