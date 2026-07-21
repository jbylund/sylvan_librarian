# Recovering the `is:` / derived-predicate namespace

[#713](https://github.com/jbylund/sylvan_librarian/issues/713)

Grew out of the `frame:` synonym investigation
(`frame:modern` returned 0 after a full scan — see [Frame synonyms](#related)); the `is:`
namespace is the same failure mode at much larger scale.

## The failure mode

`is:` queries **parse correctly** and lower to a `card_is_tags` collection lookup — but
`card_is_tags` is **empty for every card**. Scryfall provides no bulk dump for that
collection (it's the one ingest with no bulk source), so it was never populated. Result:
**every `is:` predicate silently returns 0** after a full-corpus scan — a wrong answer that
looks like a working query, plus the wasted scan (the `frame:modern` pathology, namespace-wide).

Most of these are recoverable *without* the missing tag source, because the facts are either
derivable from data we already ingest or present as ordinary fields in the bulk `default-cards`
file we already download. Only a minority are genuinely blocked.

## The validation discipline (non-negotiable)

Measured `is:bear` against its "obvious" expansion on Scryfall's live API (`unique=cards`):

| query | count |
|---|---|
| `is:bear` | 1038 |
| `t:creature pow=2 tou=2 mv=2` | 1048 |
| `pow=2 tou=2 mv=2` (no `t:creature`) | 1052 |
| `pow=2 tou=2 mv=2 -is:dfc` | 1056 |

The naive `t:creature` rewrite is wrong in **both** directions. Diffing the sets:
`is:bear` *includes* 4 non-creatures (Vehicles/Spacecraft — it's any 2/2-for-2 *permanent*, not
just creatures) and *excludes* 14 double-faced cards (all `//`). So the concept is "single-faced
2/2-for-2 permanent" — but even `pow=2 tou=2 mv=2 -is:dfc` (1056) doesn't reconcile to 1038:
Scryfall's multi-face + `unique=cards` evaluation (which face's P/T counts, how a negated `is:`
interacts with the default extras filter) is undocumented and not cleanly reproducible by set
algebra.

Lesson: **"derivable" does not mean "the naive expansion is exact," and some exact counts aren't
worth reverse-engineering.** Policy: **exact parity required and achievable for common predicates**
(`is:reprint` — a clean build-time derivation, no face games); **documented ~1–2% divergence
acceptable for niche ones** (`is:bear` → `pow=2 tou=2 mv=2 -is:dfc`, close enough). Every rewrite
carries an API-validated count test regardless, so the divergence is *known* rather than silent.

**Where the residual lives (validated on `is:bear` and `is:vanilla`).** The correct primitive-level
rewrite lands ~97–99%; the entire gap is consistently (a) multi-faced/Adventure **face evaluation**
(Scryfall judges the relevant face; a whole-card text/stat test disagrees) plus (b) a few **type
edge cases** (Vehicles/Spacecraft for `is:bear`; Dryad Arbor for `is:vanilla`). The gaps cluster
rather than scatter, so the one lever that would tighten *all* rewrites is per-face evaluation —
not per-predicate patching. And since this engine has its own multi-face model, those residuals may
not even align with Scryfall's, which is a further argument for "clean rewrite + documented
divergence" over chasing exact counts. Also: `o:""`/`o:/^$/` is a **trap** — the empty-match regex
matches every card; "no text" must be written as the negation `-o:/./`.

## Four recovery paths

| bucket | needs | one-line mechanism |
|---|---|---|
| **A. Query-rewrite** | *nothing new* | bind-time macro → AST over fields we already ingest |
| **B. Build-time bit** | one ingest/build pass, no external source | derive a per-card/printing bit from ingested data |
| **C. Bulk field (dropped)** | extend existing bulk ingest | store a `default-cards` field we currently discard |
| **D. No bulk source** | scrape / curated list | Tagger-derived or hand-curated, genuinely blocked |

The rewrite layer (A) is the same mechanism as the `frame:` synonyms: expand at the parser seam
that feeds *both* SQL and engine, so no engine change and correct-by-construction (given the
primitives). B and C need ingest work but no external source. D is the only truly blocked set.

## Classification

Confidence: ✓ = definition doc-confirmed / measured; ~ = approximate, **validate before shipping**.

### A — Query-rewrite (no new data; fields already ingested)

| predicate | expansion | conf |
|---|---|---|
| `frame:modern` | `frame:2003` | ✓ |
| `is:old` / `frame:old` | `(frame:1993 or frame:1997)` | ✓ |
| `is:new` | `frame:2015` | ✓ |
| `frame:new` | `frame:2003 or frame:2015 or frame:future` (undocumented alias; positive union — matches the validated Scryfall count and is safe against any frameless printing, unlike a `-(old)` complement) | ✓ |
| `is:historic` | `t:legendary or t:artifact or t:saga` | ✓ **exact** (7881=7881) |
| `is:permanent` | `t:creature or t:artifact or t:enchantment or t:land or t:planeswalker or t:battle` | ✓ near-exact (+2 / 25954) |
| `is:split`/`is:flip`/`is:transform`/`is:mdfc`/`is:meld`/`is:leveler` | `layout:<value>` (`card_layout` is ingested + `layout:` is queryable; exact by field correspondence) | ✓ |
| `is:spell` | `-t:land` | ~ (+85 / 32069 — not every non-land is castable) |
| `is:party` | `t:creature (t:cleric or t:rogue or t:warrior or t:wizard or kw:changeling)` | ✓ **exact** (3820) — `kw:changeling` (→ `card_keywords`) recovers the all-type creatures |
| `is:outlaw` | `(t:assassin or t:mercenary or t:pirate or t:rogue or t:warlock or kw:changeling)` — **no** `t:creature` (unlike party: includes Kindred non-creatures) | ✓ **exact** (1334) |
| `is:dfc` | `layout:transform or layout:modal_dfc or layout:meld` — gameplay DFCs. Scryfall's `is:dfc` also counts `art_series`/`reversible_card`/`double_faced_token` (~2394 art/token entries not in gameplay data), so the layout union is correct for our corpus | ✓ |
| `is:bear` | `t:creature pow=2 tou=2 cmc=2` — the intuitive "2/2 for 2"; deliberately *not* Scryfall-exact (+~14 DFC creatures, −4 Vehicles/Spacecraft; their exact count isn't cross-verifiable) | ~ |
| `is:colorshifted` | `frame:colorshifted` (frame-effect in `card_frame_data`) | ✓ **exact** (45) |
| `is:vanilla` | our engine: `t:creature o=""` (empty-string equality — clean; the `o:/^$/` empty-match regex is a Scryfall-only trap that matches *all* creatures); −11 subset vs 359 = Adventure/DFC textless faces + Dryad Arbor | ~ |
| `has:watermark` | `card_watermark` present | ✓ |

Not cleanly rewritable (text-pattern / fuzzy — defer or approximate): `is:frenchvanilla`,
`is:manland`, `is:modal`, `is:default`/`is:atypical`.

### B — Build-time bit (derive from ingested data, no external source)

| predicate | derivation | conf |
|---|---|---|
| `is:reprint` / `not:reprint` / `is:unique` | group by `oracle_id`, order by `released_at`; earliest printing = not-reprint | ✓ |
| `prints`/`sets`/`papersets` comparisons | count per `oracle_id` at build | ✓ |
| `is:commander` | legendary creature ∨ "can be your commander" text | ~ |
| `is:newinpauper` | first pauper-rarity printing per card | ~ |
| `is:hybrid` / `is:phyrexian` | ingest flag: any hybrid / Phyrexian symbol in the raw mana cost. *Not* a rewrite — the DSL only does exact-symbol containment (`m:{g/w}`), so a rewrite would be a brittle ~15-term OR over an open, growing symbol set; trivial to set at ingest instead | ✓ |

`is:reprint` is the priority here — common (pairs with `f:modern` workflows), currently silently
empty, and cleanly derivable.

### C — Bulk `default-cards` field we currently drop (verify field names against the bulk schema)

Not in the store today (confirmed absent from the ingested corpus keys), but present on each card
object in the bulk file we already fetch — so recoverable by storing the field, **not** blocked:

| predicate(s) | source field |
|---|---|
| `is:promo` + promo types (`is:prerelease`/`is:buyabox`/`is:fnm`/`is:judge_gift`/…) | `promo`, `promo_types` |
| `is:reserved` | `reserved` |
| `is:digital` | `digital` |
| `is:foil`/`is:nonfoil`/`is:etched`/`is:glossy` | `finishes` |
| `is:full` (full art) | `full_art` |
| `is:booster` | `booster` |
| `is:spotlight` | `story_spotlight` |
| `is:hires` | `highres_image` |
| `has:indicator` | `color_indicator` |
| `game:paper`/`mtgo`/`arena`, `in:<game>` | `games` |
| `stamp:oval`/`acorn`/`triangle`/`arena` | `security_stamp` |

### D — No bulk source (Tagger-derived / curated — deferred)

The land-cycle shortcuts (`is:fetchland`, `is:dual`, `is:shockland`, `is:bikeland`,
`is:checkland`, `is:painland`, … ~25 of them) and curated WotC lists (`is:gamechanger`,
`is:masterpiece`). These have no `default-cards` field; Scryfall builds them from the Tagger
project or hand-maintained lists. Blocked without scraping or maintaining our own list. Niche
enough to defer.

## Recommended plan

Do buckets **A, B, C** (in that order — cheapest-first), defer D. All independent PRs off `main`,
none touching the #702 engine-routing branch:

1. **Query-rewrite layer** (A) + the `frame:` synonyms — purely parser, zero new data, rescues the
   largest definable chunk. Each rewrite validated against the live API + a differential test.
   **Landed:** `api/parsing/rewrite.py` — a post-parse transform at the shared `parse_scryfall_query`
   seam (applies to both parsers; parity-tested; rebuilds only when a synonym actually fires), with
   `frame:modern/old/new`, `is:old`/`is:new`, `is:historic`/`is:permanent`/`is:party`/`is:outlaw`/
   `is:vanilla`/`is:bear`, the layout family (`is:split/flip/transform/mdfc/meld/leveler`),
   `is:dfc`, and `is:colorshifted`, plus `test_rewrite.py`. **Bucket A is now complete** except the
   inherently-unsuitable ones, left deferred: `is:spell` (false positives), `is:modal`,
   `is:frenchvanilla`, `is:default`/`is:atypical`, `is:manland`. `is:hybrid`/`is:phyrexian` moved to
   B (ingest flag — no clean rewrite).
2. **`is:reprint` build-time bit** (B) — one derivation, common predicate, currently broken.
3. **Ingest the dropped boolean fields** (C) — recovers promo/reserved/digital/foil/etc. as normal
   lookups.

Each predicate carries an API-validated count test so a future Scryfall change (the undocumented
aliases especially) trips a tripwire.

## Related

- Frame synonyms + query-tree rewriting (this doc's parent thread) — the rewrite seam is
  `get_frame_data_comparison_object` / node-construction, feeding both SQL and engine.
- [00667-engine-legality-divergent-carveout.md](done/00667-engine-legality-divergent-carveout.md) —
  legality is the analogous per-printing existence-projection problem.
- [00702-engine-plan-selection-layer.md](00702-engine-plan-selection-layer.md) — the "observed
  values" dictionary (leaf cardinality 0 for absent values) is the same build-time artifact this
  namespace would populate; the two-spaces estimator consumes it.
