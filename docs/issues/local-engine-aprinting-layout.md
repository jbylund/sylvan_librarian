# Engine: `APrinting` Layout — Stop Streaming Unused Bytes

Status: design note, unfiled. `APrinting` is a wide row struct; any query that scans many printings
streams the whole thing to read a few fields. Two complementary moves — **shrink** the struct and
**split** hot vs cold fields — cut that memory traffic. Benefits every broad scan, not one query.

## Problem: row storage is memory-bandwidth-bound on wide scans

`APrinting` is ~146 B of data (160 B aligned to its `u128`s — measured `size_of` 160, align 16):
22 fields, two `u128` UUIDs, a `u64` legalities word, three archived `Vec`s, a dozen `u32`/`Option`s.
A hot loop reads a couple of narrow fields per printing but pulls the whole cache line(s) each time.
On a broad result that is pure DRAM traffic.

Profiled on `border:black -(name:storm or name:dragon)` / `unique=artwork` / `orderby=usd` (main after
#736, ~1.6 ms; `CARD_ENGINE_PROF_STAGE` stage-delta timers, since reverted):

| phase | time | % |
|---|---:|---:|
| loop overhead | 49 µs | 3% |
| verification (`card_pass`, `-(names)`) | 431 µs | 26% |
| **stream 95,600 `APrinting` structs to read `artwork_group_id`** | **925 µs** | **55%** |
| `group_best` bookkeeping + emit | 215 µs | 13% |
| `prefer_score` + `usd` sort-key + quickselect | ~75 µs | 4% |

The dominant 55% is *just touching the structs* — reading a 2-byte field from 95,600 matches streams
~12 MB. Not the ordering (sort-key 2%), not grouping bookkeeping (13%), not a `group_best` cache miss
(an early wrong guess; that access is ~sequential). It is bytes-per-visit on a wide struct.

## Struct anatomy: ~⅔ is dead weight for a common scan

Read-site survey (`card_engine/src/lib.rs`) sorts the fields by what a broad scan actually needs:

| bucket | ~bytes | fields |
|---|---:|---|
| **hot** (common scan / sort / group) | ~46 B | `artwork_group_id`, `prefer_score`, `price_usd`, `released_at_int`, `card_rarity_int`, `collector_number_int`, `flavor_text_lower_id`, `card_artist_vid`, `card_set_code` |
| **display-only** (the 100-row output page) | ~52 B | `scryfall_id` (16), `illustration_id` (16), `set_name_id`, `flavor_text_id`, `collector_number_id`, `card_border_id`, `card_watermark_id` |
| **rare-predicate** (only when that filter is present) | ~48 B | `card_art_tags`/`card_is_tags`/`card_frame_data` Vecs (24), `card_legalities` (8, divergent-only ~1.8%), `price_eur`/`price_tix` (16) |

Border/watermark *filter* via the printing planes (#664/#724), so their `_id` fields are output-only;
`illustration_id` is superseded by `artwork_group_id` in hot paths (#629) — only build (artwork-group
assignment, tiebreak sort) and output read it. So ~98 B of every streamed struct is display-only or
rare-predicate — dead weight for the common scan.

## The shrink ladder

> **⚠ STOP — see "MEASURED" below.** Both a rung-1 eviction prototype *and* a columnar `artwork_group_id`
> prototype came out **flat**. A probe then showed the profiled 55% was the per-printing `border`
> residual verification, **not** `artwork_group_id` streaming — so this whole ladder targets the wrong
> cost for the surfacing query. Kept for the analysis, but **don't implement it** on this evidence.

Ordered by how you'd actually do it. Eviction leads on purpose — width-packing's *value* depends on it:
packing alone stalls at 144 B (the `u128`s force align-16, so most of the 27 B of savings becomes
padding that rounds back up), and eviction is what drops align to 8 so the later savings land in fine
steps. Each rung is its own archive-format bump, but rungs 1–2 are batchable.

### 1. Evict the two `u128` UUIDs — ~32 B (~22%), the pivotal first move

`scryfall_id` (output-only) and `illustration_id` (build + output; superseded in hot paths by
`artwork_group_id`, #629) → side `Vec<u128>` arrays. **No index field needed:** `scryfall_id` is unique
per printing, so the array is 1:1 with printings and the printing's own **pid is the index**
(`scryfall_ids[pid]`). The 100-row output page does the lookup; hot scans never touch them.

It's **pivotal, not just −32 B**: those two `u128`s are what force the struct to align-16 (the 16-B
size quantization that would otherwise cap the width pack in §2 at 144 B). Evicting them drops
`APrinting` to align-8, so every later rung's savings quantize in finer 8-B steps and can actually land.
And it's the **lowest-risk** rung: both fields are cold (output/build only), build reads `CardRow`
pre-split (untouched), and output resolution is the `str_at`-style lookup the extractors already use.

`illustration_id` *could* dedup further — it's shared by same-art printings (~40,334 distinct vs 97,206),
so it could live once per artwork group instead of per pid (~645 KB vs ~1.5 MB). But it's cold
(output/build only), so that's cold footprint, not hot bandwidth — evict per-pid for simplicity; dedup
only if cold memory matters. `scryfall_id` is unique, so it's per-pid regardless.

**Returning the UUID in the output page.** Same mechanism the output already uses for `oracle_text` /
`set_name` / `collector_number`: those read an *id* from the struct and resolve it with `str_at(strings,
id)` — a side-table lookup, not a stored value. UUIDs are just the inline exception. After eviction the
extractor changes from `p.scryfall_id` to `scryfall_ids[pid]`; the pid is already known per row (`Match =
(sort_key, cid, pid)`), so carry it in the page tuple and pass it (plus the uuid arrays) into the
extractor context alongside the `strings` table it already receives. Cost: 100 output rows × one cold-array
lookup ≈ µs — the whole point of the split is you pay the side lookup only for the rows you *return*, not
the ~95,600 you *scan*. For `unique=cards` it's the rep printing's pid, which the page already picked.

**Future UUID search (`illustration_id:` / `scryfall_id:`) — eviction enables it, doesn't block it.**
Not searchable today, but on the someday list; when added, it's an *equality lookup*, which wants a
dedicated **index**, never a scan of the inline field (scanning 97,206 `u128`s per query is both slow and
re-introduces the exact struct-streaming this doc removes — Scryfall answers these by lookup, not scan).
Build once at load: `scryfall_id:X` → sorted `(u128, pid)` / hashmap → one pid; `illustration_id:X` →
`u128 → art group → printings` (~40,334 entries), projected to cards for `unique=cards` — a narrowing leaf
through the normal `narrow_rec` path. So the value and the search key are two structures with two jobs —
side array (render the UUID in output) + index (answer `uuid:X`) — and the index is built from the same
values wherever they live. The inline field served *neither* job well (bloated every hot scan, wrong
structure for search), so supporting UUID search is an argument *for* eviction, not against it.

### 2. Width / sentinel packing — ~24 B (more if `prefer_score` is re-encoded), no restructure

With eviction (§1) having dropped align to 8, these savings now quantize finely instead of rounding
back up. Drop `Option` (archived 8 B for `Option<u32>`, no niche) for a bare value + reserved sentinel,
and narrow where the corpus allows. Corpus maxima (97,206 printings) validate every choice:

| field | now | proposed | sentinel / rep | corpus max → headroom |
|---|---:|---:|---|---|
| `price_usd` | `Option<u32>` 8 B | `u32` 4 B | `u32::MAX` = None | 514,202 ¢ ($5,142) ≪ 4.29 B |
| `price_eur` | `Option<u32>` 8 B | `u32` 4 B | `u32::MAX` = None | 3,097,514 ¢ ≪ 4.29 B |
| `released_at_int` | `Option<u32>` 8 B | `u16` 2 B | days since 1990; `u16::MAX` = None | 2026 ≈ 13,176 days ≪ 65,535 |
| `prefer_score` | `Option<f32>` 8 B | see below | — | 954 distinct, range 84.85–209.30 |
| `card_rarity_int` | `Option<u8>` 2 B | `u8` 1 B | `u8::MAX` = None | 0–5 |
| `collector_number_int` | `Option<u16>` 4 B | `u16` 2 B | `u16::MAX` = None | — |
| `price_tix` | `Option<u32>` 8 B | `u32` 4 B (sentinel) | `u32::MAX` = None | 49,106 ¢ (491 tix) |

Notes: `released_at` → **days-since-1990** is the standout (8 → 2 B on a *hot* field) but is a
*representation* change (load `yyyymmdd`→days; date/year filter bounds → days — `year:2020` becomes a
clean `[days(2020-01-01), days(2021-01-01))`; output days→date). The range index and prefer-sort stay
correct (days are monotonic). `price_tix` **fits `u16`** (491 of 655 tix) but with tight headroom on a
cold field — keep it a `u32`-sentinel unless every byte counts (else clamp `> 65,534` on load).
Everything else is a pure type swap. Precedent for sentinels: `NONE_STR`, `ARTIST_NONE`. Do the trivial
swaps (prices, rarity/cn_int) first — batchable with §1; the representation changes below are heavier.

**`prefer_score` — re-encode as a within-card rank (its own sub-ladder).** It's an `f32` (954 distinct,
84.85–209.30) but the *value* is never meaningful — it's only an **ordering key within a card**: the
build sorts each card's printings descending-`prefer_score` (storage order = prefer order), and the
score is read for (a) rep selection [max within a group], (b) printing-mode within-card ordering, (c)
the sort key's 3rd-level tiebreak `(primary, edhrec_rank, prefer_score)`. So:

- **f16 (4 B)** — measured 0 rep-winner flips across 18,474 multi-printing cards; keeps a global value,
  so it preserves (c) too. Zero behavior change.
- **`u16` within-card rank (2 B)** — max ~385 printings/card (basic lands), so 0–512 fits. Covers (a)/(b)
  exactly; (c)'s *cross-card* case (two different cards tied on primary **and** `edhrec_rank`) degrades to
  the pid tiebreak — rare, cosmetic.
- **drop entirely (0 B)** — storage order already *is* the rank, so rep = first-matching printing per
  group (Card mode does this today; Artwork mode + `card_match_count` would adopt it) and the sort key's
  within-card ordering is already identical to its pid tiebreak. Trades leaning on the storage-order
  invariant + two rep-selection code changes for the last 2 B.

The cross-card caveat (c) is **measured to be vestigial**: non-null `edhrec_rank` is *unique per card*
(0 of 31,508 shared), so the `prefer_score` sort tiebreak only fires among the **65 NULL-edhrec cards
(0.2%)** that collapse into the `edhrec = MAX` bucket — and only in that trailing block. So dropping
`prefer_score` reorders at most those 65 edhrec-less cards (prefer → pid); `drop` (0 B) and the `u16`
rank are both safe, with `f16` reserved for making even that 65-card tail byte-exact. (Data-dependent:
assumes edhrec ranks stay unique and NULL-edhrec cards stay few.) **Chosen first step: the `u16` rank**
(keeps behavior explicit; revisit `drop`/storage-order later).

**Where the rank is assigned (build-time, free from the existing sort).** In the commit/grouping pass
(`reload_commit`): the rows are already `sort_unstable_by(oracle_id, then prefer_score desc, …)`, then
split into `cards`/`printings`/`offsets`. A printing's position within its card's contiguous range *is*
its prefer-rank (0 = highest score), so it's a one-liner at the `printings.push`:
`prefer_rank = printings.len() − offsets.last()` (= `pid − card_start`), or a small post-pass beside
`assign_artwork_groups`/`assign_name_ranks` (which already walk by card range). The DB `f32` stays a
parse-time `CardRow` field feeding that sort and is never archived; the archived `Printing` carries only
`prefer_rank: u16`. Reader flips: `prefer_score()`'s Default arm and `sort_key_bits` invert to "lower
rank wins / sort ascending"; the build sort is unchanged. Caveat: the Python output extractor exposes
`prefer_score` — grep the API/frontend before dropping the value from output.

**Derived-data redundancy — don't store `*_lower` / `*_folded`.** A lowercased/accent-folded string is
*always* recoverable from its original, so storing an id for both is pure redundancy. Store one **dense**
id (into a per-field dense space that fits `u16`, not the shared global interner which can exceed `u16`)
and derive the other via a side map — or resolve the case-insensitive search into original-id space at
bind time (case-variant collisions just expand the match set slightly; exact, once per query).
Corpus-verified distinct counts (all < 65,535):

| pair | field(s) today | distinct | proposed | Δ | notes |
|---|---|---:|---|---:|---|
| flavor (`APrinting`) | `flavor_text_id` + `flavor_text_lower_id`, two `u32` = 8 B | 26,443 orig / 26,321 lower | one dense `u16` = 2 B | **−6 B / printing** | 46% of printings have no flavor (sentinel); lower is many-to-one (120 case collisions) |
| oracle (`AOracleCard`) | `oracle_text_id` + `oracle_text_lower_id`, two `u32` = 8 B | 29,088 orig = 29,088 lower (bijective) | one dense `u16` = 2 B | −6 B / card | per-*card*, so streams less, but read in the `card_pass` verify phase |
| name (`AOracleCard`) | `card_name_id` `u32` + `card_name_folded` **`InlineStr<61>`** ≈ 65 B | 31,511 names | one dense `u16` + side folded-string array | **≈ −63 B / card** | the standout: the folded name is stored *inline*; a dense id + side array frees ~63 B — but fuzzy `name:` scans read the folded string, so verify the per-candidate indirection is acceptable |

The flavor case is the direct `APrinting` win (−6 B, per printing). The `AOracleCard` cases stream less
often (per card, not per printing) but `AOracleCard` *is* touched in every query's `card_pass` verify
loop (the ~26% phase), and `card_name_folded`'s inline 61 B is a large per-card field — so the name case
is worth more than "a tiny bit." Precedent: `FlavorIndex` / `oracle_trigram` already key on **dense text
ids**, so the dense-id spaces largely exist; this just makes the stored field reference them.

### 3. Hot/cold split + columnar gather fields — the big one (~98 B out)

Push all display-only and rare-predicate fields to side arrays (indexed by pid), touched only for the
output page or when that rare predicate is present. The struct then holds only the ~46 B hot set →
broad scans stream ~46 B vs ~146 B (**~3×**). Equivalently, from the other direction, **pull the hot
gather fields into pid-indexed columns** (`artwork_group_id: Vec<u16>`, `prefer_score`, and a
*pid-indexed* `price_usd` — distinct from the value-sorted range index, which can't answer "pid X's
price"). Same principle, same endgame: the row holds only what hot loops read. Rungs 1–2 are already
the split applied to its easiest members; this is the rest of it.

**Worked example: `card_legalities` (8 B) — intern and/or evict.** The clearest case, and a distinct
technique (dictionary-encoding a high-repeat field). Corpus: **583 distinct legality words** across
97,206 printings (0.6% unique; 23 formats × 4 statuses = `legal`/`not_legal`/`restricted`/`banned`,
≈46 bits → the `u64`). Only 10 bits id them all; top-256 cover 97.9%. Two options, stackable:

- **Intern** → `u16` id into a 583-entry dictionary (~4.7 KB): 8 B → 2 B per printing (−6 B), total
  ~778 KB → ~199 KB.
- **Evict — it barely needs to be per-printing at all.** Legalities are card-invariant except for the
  ~1.8% *divergent* cards; the common case is answered by the `_EXISTS` card planes (#667), and the raw
  `u64` is read *only* during divergent repair. So drop it from the struct entirely and keep a side
  array for the ~11,329 divergent printings alone (interned `u16` → ~23 KB): **−8 B on all 97,206
  structs**, one dict indirection on the already-rare divergent path.

## The crossover: columns for wide scans, rows for narrow materialization

Columns/split win when a scan reads **few fields across many rows** (the gather loop). Rows win when
you read **many fields of few rows** — materializing the 100-row page, where one struct load beats N
column cache lines. So keep the display fields co-located in a side *row* (one lookup per output row),
not scattered across N columns. Measure the materialization crossover before committing.

## Size → cache-line ladder

The real magic number is the **64 B cache line**; **16 B** is the struct's alignment (forced by the
`u128`s), so size lands on 16-B boundaries until they're evicted. Streaming cost tracks lines-per-struct:

| layout | size | cache lines |
|---|---:|---:|
| current | 160 B | 2.5 |
| width-pack *in place* (align still 16) | 144 B | 2.25 |
| evict `u128`s (align 16→8) + width-pack | ~104 B | ~1.6 |
| hot set only (hot/cold split) | ~48–64 B | 1.0 |

Width-packing alone is ~10% (2.5→2.25 lines) — alignment eats it. The qualitative win is reaching **one
cache line** (≤64 B), which needs the hot/cold split; the `u128` eviction is the enabler.

## MEASURED (2026-07): incremental shrink is a dead end above one cache line

A prototype of rung 1 (evict both `u128`s → 128 B) was built and measured end-to-end (fresh archive,
totals byte-identical). Result: **flat — 0 speedup** on every probe (`border:black -(names)` artwork/usd
1491→1490 µs; `usd<50` artwork/usd, `year>=2015` printing/usd, `t:creature` all within noise). So the
size→line table above **overstates the incremental payoff**: streaming here is bound by *cache lines
touched per struct*, not footprint, and at >64 B reading one field pulls ~one line/struct whether it's
160 B or 128 B — the stride shrank, the line count didn't. Nothing pays until the struct crosses **≤64 B**
(structs share lines) or the hot field is pulled into a **narrow contiguous column** the scan reads
instead of the struct.

**Consequence:** rungs 1–2 (evict + width + flavor + reorder), which all keep the struct >64 B until the
very end, are **not worth doing for the gather cost** — the eviction prototype alone needed a ~20-site
test rework (`scryfall_id` is the printing-identity anchor in the differential tests) for zero measured
gain.

**And the columnar prototype was flat too — because the profile was misattributed.** A dense
`artwork_group_id: Vec<u16>` the gather reads instead of the struct also measured flat (1491→1483 µs).
A one-line probe explained it: on `border:black -(names)` / artwork the grouping loop runs with
`all_match=false`, so it calls `residual_matches(&printings[pid], …)` to verify **`border:black` per
printing** (`border` is printing-varying → can't settle at card level; not using the border plane here).
That per-printing residual reads the struct — and the earlier "925 µs readonly stage" **included it**
(the residual ran before the stage's `continue`). So the 55% was the **per-printing `border` residual
verification, not `artwork_group_id` streaming**; both layout experiments left the residual untouched,
hence flat.

**Corrected conclusion: the `APrinting`-layout thesis targets the wrong cost for this query.** The gather
cost here is per-printing residual verification of a printing-varying predicate (`border`), inherent to
`unique=artwork` needing a black-bordered *rep* per artwork group. Struct footprint / `artwork_group_id`
access are not the lever. Neither shrinking nor columnar-izing `artwork_group_id` helps. **Recommend
stopping the layout effort** (a p100 shape; real traffic is narrow + relevance-ordered). If this tail ever
matters, the real question is reducing per-printing residual verification (e.g. plane-narrowing `border`
for the artwork-rep pick, or a corrected re-profile to find the true hot field per query shape) — a
different investigation from struct layout. **Resolution:** that different investigation paid off —
the residual *is* reducible without any layout change. See
[artwork skip-repped](./done/local-engine-artwork-skip-repped.md): skipping already-repped artwork groups
before the residual gives 1.23–1.35× on the `border:black` / artwork / usd path, byte-identical, zero
archive change.

## Precedent

The engine already applies struct-of-arrays selectively — this generalizes it:
[`printing_to_card`](00690-engine-direct-projection-arrays.md) (pid-indexed column), the bit-planes
(per-value bit-columns), and the value-sorted range indexes.

## Cost, open questions, priority

- **Cost:** an rkyv archive-format change (`ARCHIVE_FORMAT_VERSION` bump); rung 3 is a broad
  field-access refactor.
- **Field set:** derive columns/hot-set from what the O(printings) loops actually read
  (`push_card_matches`, `card_match_count`, sort-key/prefer/verify), not by guessing.
- **`prefer=default` shortcut:** printings are stored default-prefer-desc, so first-seen-per-group is
  the rep — drops the `prefer_score` read for the common case, so the `gid` column alone suffices there.
- **Priority:** unproven. Surfaced by a p100 broad `unique=artwork` shape; real traffic skews narrow +
  relevance-ordered. Do rung 1 (evict — cheap, general, and it unblocks the rest) plus the trivial §2
  swaps opportunistically; gate the heavier §2 items and rung 3 on evidence that broad gather-sort /
  artwork queries matter.

## Related

- [#690](00690-engine-direct-projection-arrays.md) — direct-projection array precedent.
- #629 — artwork groups (`artwork_group_id`); #664/#724 — border/rarity printing planes.
- `exec_gathered_scan` / `push_card_matches` (`card_engine/src/lib.rs`) — the gather path this targets.
