"""One query universe for every generator in the repo, with two weightings over it.

Every script that needs synthetic queries — the load runner, the survey, and the whole cost-model
bench stack — draws from this module. Before it existed as the single source, the same weighted
"pick a field, pick a value" logic lived in three places with three different field lists, and the
gaps between them were invisible: the cost-model benches never emitted `produces:` or `devotion:`
because that generator had never grown them, while the load runner never emitted `cmc` or any
arithmetic because *its* list had never grown those.

## Values come from the corpus when there is one

Pass a corpus and every value is drawn from real data — card names, types and subtypes, artists, set
codes, oracle and flavor vocabulary, the real colour/border/frame/watermark/legality value sets, and
real distributions for every numeric column. `t:` alone spans 437 distinct values from `Creature`
(45,976 printings) down to one-offs, which is a far better selectivity ladder than any hand-written
list.

Without a corpus (`corpus=None`) the sampler falls back to `FALLBACK_VOCAB` and `FALLBACK_DECILES`:
compact, curated stand-ins that keep the *shape* of the distribution without needing the data on
disk. That is what lets the load runner — which ships in a container holding nothing but `client/` —
use this module instead of carrying its own parallel copy of the field table.

## Two modes over the same universe

- **`realistic`** weights toward traffic we expect. This applies at BOTH levels: which family a
  predicate comes from (name/oracle/type/numeric over flavor text) and which value within it
  (`t:creature` far more often than `t:vronos`, by corpus frequency).
- **`uniform`** weights families evenly and values flat over the distinct vocabulary. Use it to
  explore the space and catch regressions — `artwork` is 5% of realistic traffic but was where a 12x
  routing regression hid, precisely because nothing sampled it hard enough to notice.

They differ ONLY in weights, never in what can be produced, so a finding under one is reproducible
under the other with enough samples.

## One family per FIELD, because families are the dedupe unit

A query never draws twice from the same family, so anything merged into one family can never appear
alongside itself. That is right for `t:` (types and subtypes share an operator) and wrong for
everything else: colour and colour identity were one family, which silently made `c:u id:wu`
unreachable, and `pow`/`tou`/`cmc` were one family, which made `pow>2 tou<4` unreachable. Fields get
their own family unless they genuinely share an operator.

## Sampling is uniform in QUANTILE, not in value

Drawing a price uniformly from [0.05, 400] produces almost nothing but "matches nearly everything",
because real prices are heavily skewed. Drawing the p-th percentile for uniform p spreads
*selectivity* evenly across (0, 1), which is the axis the cost model varies along. This applies to
every numeric field, `pow` and `cmc` as much as `usd`.

The bounded-range shape (`usd>=a usd<=b`) matters more than it looks: it is absent from every older
generator, and the first paired A/B run with it found a 12x routing regression that those generators
could not see, concentrated entirely in bounded ranges at artwork granularity.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pathlib
    import random

MODES = ("realistic", "uniform")

# ─── Numeric fields ───────────────────────────────────────────────────────────
# Field name in query syntax → the corpus column its thresholds are drawn from. Each is its own
# family, so `usd<5 cn>100` and `pow>2 tou<4` are both reachable.
NUMERIC_COLUMNS: dict[str, str] = {
    "usd": "price_usd",
    "eur": "price_eur",
    "tix": "price_tix",
    "cn": "collector_number_int",
    "released": "released_at",
    "pow": "creature_power",
    "tou": "creature_toughness",
    "cmc": "cmc",
    "loyalty": "planeswalker_loyalty",
}
RANGE_OPS = ("<", "<=", ">", ">=", ":")
# `pow:3` parses, but `pow=3` is what people write and what the extended-arithmetic grammar uses.
NUMERIC_OPS = ("<", "<=", ">", ">=", "=")
# Chance a numeric family emits `f>=a f<=b` instead of a single-sided bound. Boundedness is a
# property of the draw rather than its own family: as a separate family it could co-occur with a
# one-sided draw on the same field and emit the nonsense `usd<5 usd>=1 usd<=20`.
BOUNDED_FRACTION = 0.25
# `released` is one column with two spellings; `year:2019` is the common one, `date>=…` the precise.
YEAR_FRACTION = 0.6
# `creature_power` stores `*` as a negative sentinel. Thresholds below this are never interesting
# and `pow>=-1` is a parse the grammar need not carry.
MIN_NUMERIC_THRESHOLD = 0

# ─── Family weights ───────────────────────────────────────────────────────────
# Relative weight in `realistic`; `uniform` uses all-ones. Composite families that were split kept
# their old combined weight, divided by the load runner's relative weights within the composite, so
# realistic-mode behaviour stays close to what the cost-model baselines were taken under.
REALISTIC_FAMILY_WEIGHTS: dict[str, float] = {
    "name": 20,
    "oracle": 18,
    "type": 14,  # t:, over merged types + subtypes
    "legality": 8,
    "identity": 6,
    "color": 6,
    "set": 5,
    "rarity": 4,
    # Deliberately overlaps the oracle family: `keyword:flying` is a JSONB key lookup and
    # `o:flying` a trigram substring match, so the same user intent takes two different index
    # paths and both are worth sampling.
    "keyword": 4,
    "pow": 3.5,
    "tou": 3,
    "cmc": 3,
    "released": 3,
    "usd": 2.5,
    "tag": 1.5,
    "artist": 1.5,
    "arith": 1.5,  # the extended syntax: power+toughness<6, cmc+1<pow
    "produces": 1.5,
    "loyalty": 1,
    "eur": 1,
    "tix": 1,
    "cn": 0.5,
    "border": 0.5,
    "frame": 0.5,
    "watermark": 0.5,
    "devotion": 0.5,
    "flavor": 0.5,
}
REALISTIC_UNIQUE_WEIGHTS: dict[str, float] = {"card": 75, "printing": 20, "artwork": 5}
# Every orderby the engine has a sort column for. Mirrors api.enums.CardOrdering, which is the
# authority; test_query_sampler asserts they agree. Not imported from there because this module
# ships in the client image, which contains no `api/`. Anything outside this set is accepted by the
# engine and silently sorted by edhrec, so callers mapping external orderbys should fall back
# explicitly rather than pass an unknown value through and mislabel the result.
ENGINE_ORDERBYS = frozenset({"cmc", "cubecobra", "edhrec", "name", "power", "rarity", "toughness", "usd"})
REALISTIC_ORDERBY_WEIGHTS: dict[str, float] = {
    "edhrec": 40,
    "name": 15,
    "cmc": 10,
    "usd": 10,
    "rarity": 8,
    "power": 6,
    "toughness": 5,
    "cubecobra": 6,
}
# Non-default result parameters skew hard to their defaults but are sampled so the paths that only
# run off-default stay on the radar.
REALISTIC_PREFER_WEIGHTS: dict[str, float] = {"default": 85, "newest": 6, "oldest": 4, "usd_low": 3, "usd_high": 2}
REALISTIC_DIRECTION_WEIGHTS: dict[str, float] = {"asc": 85, "desc": 15}
REALISTIC_OFFSET_WEIGHTS: dict[int, float] = {0: 90, 100: 7, 700: 3}
# How many predicates a flat `query` gets. Deeper conjunctions narrow to nothing and stop
# exercising plan choice at all, which is the thing being measured.
PREDICATE_COUNT_WEIGHTS: dict[int, float] = {1: 45, 2: 40, 3: 15}

# ─── Closed vocabularies ──────────────────────────────────────────────────────
# Small fixed sets the corpus cannot improve on: `is:` predicates are computed rather than stored,
# devotion is a colour crossed with a pip count, and arithmetic is our own syntax extension.
STATIC_VALUES: dict[str, list[str]] = {
    # Exactly the `is:` values the rewrite layer expands (api/parsing/rewrite.py's
    # _DERIVED_EXPANSIONS; test_query_sampler asserts they agree). Not imported from there because
    # this module ships in the client image, which contains no `api/`.
    #
    # Anything outside this set parses but falls through to a `card_is_tags` lookup, and that column
    # carries only the booleans the import syncs from the bulk blob (db_info.BOOLEAN_IS_TAGS) —
    # `is:reserved` and `is:reprint` are the ones to reach for. Values with no key there match zero
    # cards AND now raise a warning, as `is:token` does. All 84 are kept rather
    # than a token few: the family's share of traffic is set by its weight, not by how many values
    # it holds, and each expands to a genuinely different shape — layout lookups, type unions, an
    # oracle-text heuristic, a numeric conjunction.
    "tag": [
        "is:adventure",
        "is:alchemy",
        "is:arenaleague",
        "is:artseries",
        "is:augmentation",
        "is:battleland",
        "is:bear",
        "is:bicycleland",
        "is:bikeland",
        "is:bondland",
        "is:borderless",
        "is:bounceland",
        "is:brawler",
        "is:canland",
        "is:canopyland",
        "is:checkland",
        "is:class",
        "is:colorshifted",
        "is:commander",
        "is:companion",
        "is:creatureland",
        "is:cycleland",
        "is:dfc",
        "is:dual",
        "is:duelcommander",
        "is:extendedart",
        "is:fastland",
        "is:fetchland",
        "is:filterland",
        "is:firstprint",
        "is:firstprinting",
        "is:flip",
        "is:frenchvanilla",
        "is:full",
        "is:funny",
        "is:gainland",
        "is:historic",
        "is:hybrid",
        "is:host",
        "is:intropack",
        "is:judge",
        "is:judgegift",
        "is:karoo",
        "is:leveler",
        "is:manland",
        "is:masterpiece",
        "is:mdfc",
        "is:mediainsert",
        "is:meld",
        "is:modal",
        "is:new",
        "is:oathbreaker",
        "is:old",
        "is:outlaw",
        "is:painland",
        "is:party",
        "is:pathway",
        "is:permanent",
        "is:phyrexian",
        "is:planar",
        "is:planeswalkerdeck",
        "is:promostamped",
        "is:rainbow",
        "is:reversible",
        "is:scryland",
        "is:setpromo",
        "is:shadowland",
        "is:shockland",
        "is:showcase",
        "is:slowland",
        "is:snarl",
        "is:spell",
        "is:split",
        "is:storageland",
        "is:surveilland",
        "is:tangoland",
        "is:tdfc",
        "is:token",
        "is:tombstone",
        "is:transform",
        "is:tricycleland",
        "is:triland",
        "is:triome",
        # NO `is:vanilla`: it stopped expanding and became an engine leaf, so it belongs to the same
        # class as `is:localizedname`, `is:unique` and `is:flavorname`, which are likewise absent.
        # See `rewrite.ENGINE_IS_VALUES`.
        "is:watermark",
    ],
    "devotion": [
        "devotion:w",
        "devotion:u",
        "devotion:b",
        "devotion:r",
        "devotion:g",
        "devotion:www",
        "devotion:uuu",
        "devotion:rrr",
    ],
    "arith": ["power+toughness<6", "power+toughness>8", "cmc+1<pow", "pow>=tou", "cmc>=power", "toughness>power"],
}
# Field prefix for families whose predicate is a plain `prefix:value` over a sampled vocabulary.
VOCAB_PREFIXES: dict[str, str] = {
    "oracle": "o",
    "flavor": "ft",
    "keyword": "keyword",
    "artist": "a",
    "set": "set",
    "type": "t",
    "color": "c",
    "identity": "id",
    "legality": "f",
    "produces": "produces",
    "border": "border",
    "frame": "frame",
    "watermark": "watermark",
}
# Indexed by the corpus's `card_rarity_int`; mirrors magic.rarity_int_to_text in the schema.
RARITIES = ("common", "uncommon", "rare", "mythic", "special", "bonus")
# Rarity is ordered, so it gets the comparison forms too — `r>=rare` is a different plan from `r:rare`.
RARITY_OPS = (":", ">=", "<=")
# Corpus values carry apostrophes, parentheses and spaces ("O'Connor", "First Strike"), none of
# which survive the lexer bare. Quoting is semantically inert — `t:goblin` and `t:"goblin"` parse to
# the same node — so it is applied to any value that is not plain alphanumeric.
BARE_VALUE_RE = re.compile(r"^[a-z0-9]+$")

# A word must be this long and appear in this many rows to be worth querying: rarer words match
# nothing and only produce degenerate empty results.
MIN_WORD_LEN = 4
MIN_WORD_ROWS = 30
MAX_VOCAB = 4000
# Name predicates take a word from a real card name; this often is shortened to a prefix of this
# length, which is what makes broad `name:` searches (`name:bo`) appear alongside selective ones.
NAME_PREFIX_LEN = (2, 6)
NAME_PREFIX_FRACTION = 0.5
WORD_RE = re.compile(rf"[a-z]{{{MIN_WORD_LEN},}}")


# ─── Query structures ─────────────────────────────────────────────────────────
# Structure name → (relative weight, template). The template's placeholder count is the arity;
# placeholders are filled with predicates from that many distinct families. Used by
# `structured_query` only — `query` always emits a flat conjunction, which is what the cost-model
# baselines assume.
#
# This is the CONNECTIVE dimension: how predicates are joined. `Shape` below is the orthogonal one:
# which predicates may be drawn at all. A query has both.
STRUCTURES: dict[str, tuple[float, str]] = {
    "single": (23, "{0}"),
    "and2": (18, "{0} {1}"),
    "and3": (12, "{0} {1} {2}"),
    "and4": (6, "{0} {1} {2} {3}"),
    "or2": (9, "{0} or {1}"),
    "or3": (4, "{0} or {1} or {2}"),
    "paren-or": (9, "({0} {1}) or ({2} {3})"),
    "and-of-ors": (3, "({0} or {1}) ({2} or {3})"),
    "and-or": (7, "{0} ({1} or {2})"),
    "neg-and": (4, "-{0} {1}"),
    "neg-or": (2, "{0} -({1} or {2})"),
    # Regex is a distinct engine path with no field of its own, so it is a structure, not a family. The
    # second placeholder is an ordinary predicate anchoring it; see REGEX_ANCHOR_FRACTION.
    "regex": (1, "{0} {1}"),
}
REGEX_FRAGMENTS = ["name:/^gob/", "o:/draw .* cards?/", "name:/dragon$/", "o:/^flying$/", "name:/^[aeiou]/", "o:/sacrifice a/"]
# Chance a regex fragment gets that anchoring predicate rather than standing alone.
REGEX_ANCHOR_FRACTION = 0.5

# ─── Shape: which predicates may be drawn ─────────────────────────────────────
# Attempts per requested predicate before `query()` gives up trying to land another distinct family.
# Only reachable under a `Shape` whose family pool is small relative to the requested count.
MAX_FAMILY_DRAWS = 8
#: The families `bare_range_bounds` resolves to a RANGE INDEX: the three price columns and collector
#: number each have their own, and `year:`/`date:` both map onto `released_at`. Named because "a
#: range query" is a thing targeted benchmarks ask for, and the one-family-per-field split — needed
#: so `usd<5 cn>100` is reachable at all — would otherwise make it a literal at every call site.
RANGE_FAMILIES = frozenset({"usd", "eur", "tix", "cn", "released"})


@dataclasses.dataclass(frozen=True)
class Shape:
    """A constraint on what `query()` / `structured_query()` / `unique()` / `orderby()` may draw.

    Targeted benchmarks exist because they need ONE query shape — a bare range under
    `unique=printing`, a compose leaf, a two-sided bound — and before this they each hand-rolled a
    generator to get it. Those generators picked values off hardcoded lists, which is precisely what
    this module's header argues against: a cost model is a function of selectivity, and a benchmark
    that samples six values of it cannot say whether the model is right. A shape narrows WHICH
    predicates appear without giving up corpus-derived values or quantile-placed thresholds.

    Every field is a restriction on the default weighted draw; `None` means "no restriction", and
    the mode's weights still apply across whatever survives.

    Shape is orthogonal to `STRUCTURES`, which is how the drawn predicates get JOINED (AND, OR,
    parens, negation). A shape says what may appear; a structure says how it is written.

    Note what this deliberately cannot express: matched algebraic pairs (`-usd<c usd<d` against its
    direct equivalent), controls chosen by knowing what a diff touches, or a value picked because it
    has a known posting count. Those are human judgements and belong in a curated list.

        Shape(families=RANGE_FAMILIES, predicates=1, unique=frozenset({"printing"}))
        Shape(families=RANGE_FAMILIES, bounded=True)  # two-sided bounds only

    Raises:
        ValueError: If a field names something unknown, or `predicates` is below 1.
    """

    families: frozenset[str] | None = None
    predicates: int | None = None
    unique: frozenset[str] | None = None
    orderby: frozenset[str] | None = None
    structures: frozenset[str] | None = None
    # Force two-sided (`True`) or one-sided (`False`) numeric bounds. `None` leaves it to
    # BOUNDED_FRACTION. Boundedness is a property of the draw rather than a family of its own — as a
    # family it could co-occur with a one-sided draw on the same field — so it is pinned here.
    bounded: bool | None = None

    def __post_init__(self) -> None:
        """Reject a shape that can never produce a query, rather than looping forever later."""
        for field, known in (
            ("families", set(REALISTIC_FAMILY_WEIGHTS)),
            ("unique", set(REALISTIC_UNIQUE_WEIGHTS)),
            ("orderby", set(REALISTIC_ORDERBY_WEIGHTS)),
            ("structures", set(STRUCTURES)),
        ):
            value = getattr(self, field)
            if value is not None and (unknown := set(value) - known):
                msg = f"Shape.{field} has unknown {sorted(unknown)}; known are {sorted(known)}"
                raise ValueError(msg)
        if self.predicates is not None and self.predicates < 1:
            msg = f"Shape.predicates must be >= 1, got {self.predicates}"
            raise ValueError(msg)


#: No restriction — what every caller got before `Shape` existed.
ANY_SHAPE = Shape()

# ─── No-corpus fallbacks ──────────────────────────────────────────────────────
# Deciles (p0, p10, … p100) measured over the full printing corpus, interpolated to answer any
# quantile. Sampling stays uniform-in-quantile without the corpus on disk; the cost is decile
# granularity, which no caller without a corpus is measuring finely enough to notice.
FALLBACK_DECILES: dict[str, list[float]] = {
    "price_usd": [0.01, 0.11, 0.16, 0.22, 0.27, 0.33, 0.44, 0.92, 2.39, 6.63, 5142.02],
    "price_eur": [0.02, 0.07, 0.10, 0.15, 0.20, 0.27, 0.42, 0.74, 1.67, 5.16, 30975.14],
    "price_tix": [0.01, 0.02, 0.02, 0.03, 0.03, 0.03, 0.04, 0.06, 0.18, 0.83, 491.06],
    "collector_number_int": [0, 23, 50, 80, 113, 148, 190, 236, 298, 426, 202617],
    "creature_power": [0, 1, 1, 2, 2, 2, 3, 3, 4, 5, 20],
    "creature_toughness": [0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 30],
    "cmc": [0, 0, 1, 2, 2, 3, 3, 4, 5, 6, 16],
    "planeswalker_loyalty": [0, 3, 3, 4, 4, 4, 4, 5, 5, 5, 7],
    # Ordinals, so the same interpolation works; rendered back to a date on the way out.
    "released_at": [
        dt.date(y, m, d).toordinal()
        for y, m, d in [
            (1993, 8, 5),
            (1999, 10, 4),
            (2007, 7, 13),
            (2014, 5, 2),
            (2018, 4, 6),
            (2020, 8, 7),
            (2022, 4, 12),
            (2023, 4, 21),
            (2024, 3, 8),
            (2025, 2, 14),
            (2026, 11, 13),
        ]
    ],
}
# Closed-value families are the real value sets, most-common first. Open families are curated
# search terms rather than the corpus's own most-common words: the top oracle words by frequency are
# "this", "that", "with", which match nearly everything and make a useless load generator.
FALLBACK_VOCAB: dict[str, list[str]] = {
    "name": ["bolt", "angel", "dragon", "counter", "force", "fire", "dark", "ancient", "storm", "path", "bo", "an", "dr", "fi"],
    "oracle": [
        "flying",
        "haste",
        "trample",
        "deathtouch",
        "lifelink",
        "vigilance",
        "draw",
        "counter",
        "destroy",
        "exile",
        "token",
        "sacrifice",
    ],
    "flavor": ["death", "fire", "light", "darkness", "power", "ancient"],
    # The evergreen keywords, plus a handful of distinctive non-evergreen mechanics for the
    # narrow end — `keyword:infect` matches 80 printings against `keyword:flying`'s 9,060, and a
    # vocabulary of only common values would never exercise the selective side of this index.
    "keyword": [
        "deathtouch",
        "defender",
        "double strike",
        "enchant",
        "equip",
        "first strike",
        "flash",
        "flying",
        "haste",
        "hexproof",
        "indestructible",
        "lifelink",
        "menace",
        "protection",
        "prowess",
        "reach",
        "trample",
        "vigilance",
        "ward",
        "scry",
        "mill",
        "cycling",
        "flashback",
        "kicker",
        "landfall",
        "infect",
        "exalted",
        "cascade",
        "storm",
        "delve",
    ],
    "type": [
        "creature",
        "instant",
        "sorcery",
        "enchantment",
        "artifact",
        "planeswalker",
        "land",
        "dragon",
        "wizard",
        "goblin",
        "zombie",
        "elf",
        "equipment",
        "aura",
    ],
    "set": ["m21", "znr", "khm", "stx", "mid", "neo", "snc", "dmu", "bro", "mom", "ltr", "woe", "mkm", "otj"],
    "artist": ["tedin", "rahn", "avon", "burns", "thomas", "foglio", "walker", "staples", "daarken", "paquette"],
    "color": ["g", "w", "b", "r", "u", "gu", "uw", "gw", "bu", "br", "rw", "wub"],
    "identity": ["g", "w", "b", "r", "u", "bu", "br", "uw", "gw", "gu", "rw", "brg"],
    "legality": ["commander", "vintage", "legacy", "modern", "pioneer", "standard", "pauper", "duel", "historic", "timeless"],
    "produces": ["g", "r", "b", "u", "w", "c"],
    "border": ["black", "borderless", "white", "gold", "yellow"],
    "frame": ["2015", "2003", "1997", "legendary", "inverted", "1993", "extendedart", "showcase"],
    "watermark": ["wotc", "set", "phyrexian", "mirran", "golgari", "selesnya", "dimir", "izzet"],
}


class QuerySampler:
    """The query universe, sampled under one of the two `MODES`.

    With a corpus, values come from the data; without one, from `FALLBACK_VOCAB` and
    `FALLBACK_DECILES`. The two paths differ only in where values come from — families, weights,
    structures and predicate syntax are shared, so a query the runner can emit is one a bench can emit.
    """

    def __init__(self, corpus: pathlib.Path | None = None, mode: str = "uniform") -> None:
        """Build the value universe and this mode's weight tables.

        Args:
            corpus: Printing-corpus JSONL to draw values from, or None for the built-in fallbacks.
            mode: One of `MODES`.

        Raises:
            ValueError: If `mode` is not one of `MODES`.
        """
        if mode not in MODES:
            msg = f"mode must be one of {MODES}, got {mode!r}"
            raise ValueError(msg)
        self.mode = mode
        self.realistic = mode == "realistic"
        self.corpus = corpus
        if corpus is None:
            self._load_fallbacks()
        else:
            self._read_corpus(corpus)
        self.families = self._weights({k: v for k, v in REALISTIC_FAMILY_WEIGHTS.items() if k not in self.missing_numeric})
        if not self.families[0]:
            msg = f"corpus {corpus} supports no query family at all"
            raise ValueError(msg)
        self.uniques = self._weights(REALISTIC_UNIQUE_WEIGHTS)
        self.orderbys = self._weights(REALISTIC_ORDERBY_WEIGHTS)
        self.prefers = self._weights(REALISTIC_PREFER_WEIGHTS)
        self.directions = self._weights(REALISTIC_DIRECTION_WEIGHTS)
        self.offsets = self._weights(REALISTIC_OFFSET_WEIGHTS)
        self.structure_names = self._weights({name: weight for name, (weight, _) in STRUCTURES.items()})
        # Predicate count is a structural knob, not a traffic weight, so `uniform` does not flatten it.
        self.predicate_counts = (list(PREDICATE_COUNT_WEIGHTS), list(PREDICATE_COUNT_WEIGHTS.values()))

    def _weights[T](self, realistic_table: dict[T, float]) -> tuple[list[T], list[float]]:
        """Keys with their weights — the realistic table, or all-ones for uniform."""
        keys = list(realistic_table)
        return keys, ([realistic_table[k] for k in keys] if self.realistic else [1.0] * len(keys))

    @staticmethod
    def _choose[T](table: tuple[list[T], list[float]], rng: random.Random) -> T:
        """One key from a `_weights` table."""
        keys, weights = table
        return rng.choices(keys, weights=weights)[0]

    def _vocab(self, counts: collections.Counter[str], *, floor: int = 1, cap: int | None = None) -> tuple[list[str], list[float]]:
        """A corpus vocabulary as (values, weights).

        Realistic mode weights by corpus frequency, so `t:creature` dominates `t:vronos` the way
        real traffic does. Uniform mode goes flat over the distinct values, which is what makes it
        reach the rare tail — and the rare tail is where selectivity extremes, and the plans that
        only appear at them, actually live.
        """
        items = [(w, n) for w, n in (counts.most_common(cap) if cap else counts.most_common()) if n >= floor]
        if not items:
            return ["the"], [1.0]
        values = [w for w, _ in items]
        return values, ([float(n) for _, n in items] if self.realistic else [1.0] * len(items))

    def _load_fallbacks(self) -> None:
        """Populate the universe from the built-in tables instead of a corpus."""
        self.sorted = {col: list(vals) for col, vals in FALLBACK_DECILES.items()}
        self.interpolated = set(FALLBACK_DECILES)
        self.missing_numeric: set[str] = set()
        self.vocab = {fam: (list(vals), [1.0] * len(vals)) for fam, vals in FALLBACK_VOCAB.items()}
        self.rarities = list(RARITIES)

    @staticmethod
    def _count_row(row: dict, counters: dict[str, collections.Counter[str]]) -> None:
        """Fold one corpus row into every vocabulary it contributes to."""
        # Plain scalars. Artists are keyed on surname, which is how people search them.
        for family, column in (
            ("name", "card_name"),
            ("set", "card_set_code"),
            ("border", "card_border"),
            ("watermark", "card_watermark"),
        ):
            if value := row.get(column):
                counters[family][value.lower()] += 1
        if artist := row.get("card_artist"):
            counters["artist"][artist.lower().split()[-1]] += 1
        # Types and subtypes share the `t:` operator (`t:creature` and `t:human` are both valid), so
        # they are one vocabulary, not two.
        for column in ("card_types", "card_subtypes"):
            counters["type"].update(value.lower() for value in row.get(column) or [])
        # Colour columns are pip → True maps and the query syntax wants the combination, so
        # `{"B": True, "G": True}` becomes the single value `bg`. Colourless is the empty
        # combination, which `c:` cannot express, so it is skipped.
        for family, column in (("color", "card_colors"), ("identity", "card_color_identity")):
            if combo := "".join(sorted(row.get(column) or {})).lower():
                counters[family][combo] += 1
        # Key-set maps, where the presence of a key is the value being queried.
        for family, column in (("produces", "produced_mana"), ("frame", "card_frame_data")):
            counters[family].update(pip.lower() for pip in row.get(column) or {})
        # Keywords have a long tail (770 distinct, down to one-offs) which is left whole on purpose:
        # uniform mode reaching `keyword:"brood telepathy"` is how the selectivity extremes get
        # sampled at all.
        counters["keyword"].update(k.lower() for k in row.get("card_keywords") or {})
        # Only formats a card can actually be legal in; `f:` on a format nothing is legal in is a
        # guaranteed-empty query, which measures nothing.
        counters["legality"].update(fmt.lower() for fmt, status in (row.get("card_legalities") or {}).items() if status == "legal")
        # Counter over the SET of words per row, so a word repeated within one card counts once and
        # MIN_WORD_ROWS means "appears in N rows", not "appears N times".
        for family, column in (("oracle", "oracle_text"), ("flavor", "flavor_text")):
            counters[family].update(set(WORD_RE.findall((row.get(column) or "").lower())))

    def _read_corpus(self, corpus: pathlib.Path) -> None:
        """One pass: sorted values per numeric column, plus every corpus-derived vocabulary."""
        cols: dict[str, list[float]] = {c: [] for c in set(NUMERIC_COLUMNS.values())}
        counters: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
        rarities: set[int] = set()
        with corpus.open() as handle:
            for line in handle:
                row = json.loads(line)
                for col, out in cols.items():
                    value = row.get(col)
                    if value is None:
                        continue
                    out.append(dt.date.fromisoformat(value[:10]).toordinal() if col == "released_at" else float(value))
                if (rarity := row.get("card_rarity_int")) is not None:
                    rarities.add(int(rarity))
                self._count_row(row, counters)

        # With a corpus, a numeric column it does not carry is DROPPED rather than falling back to
        # the built-in deciles: those deciles describe the full corpus, so on a trimmed export they
        # would place thresholds the data cannot honour and quietly emit match-everything or
        # match-nothing queries. `eur`/`tix` are the realistic case — sparse, and an export can omit
        # them. A field that is not offered is visible in the family pool; a field sampled against
        # the wrong distribution is not. (Without a corpus at all, the deciles ARE the universe.)
        self.sorted: dict[str, list[float]] = {c: sorted(v) for c, v in cols.items() if v}
        self.interpolated: set[str] = set()
        self.missing_numeric = {f for f, col in NUMERIC_COLUMNS.items() if col not in self.sorted}
        # Free text is capped and floored; every other vocabulary is a closed value set worth
        # keeping whole, however long its tail.
        self.vocab: dict[str, tuple[list[str], list[float]]] = {}
        for family in (*VOCAB_PREFIXES, "name"):
            if not counters[family]:
                self.vocab[family] = (list(FALLBACK_VOCAB[family]), [1.0] * len(FALLBACK_VOCAB[family]))
            elif family in ("oracle", "flavor"):
                self.vocab[family] = self._vocab(counters[family], floor=MIN_WORD_ROWS, cap=MAX_VOCAB)
            else:
                self.vocab[family] = self._vocab(counters[family])
        self.rarities = [RARITIES[r] for r in sorted(rarities) if r < len(RARITIES)] or list(RARITIES)

    # ─── Value drawing ────────────────────────────────────────────────────────

    def _pick(self, family: str, rng: random.Random) -> str:
        """One value from a vocabulary, weighted by mode."""
        values, weights = self.vocab[family]
        return rng.choices(values, weights=weights)[0]

    def quantile(self, column: str, p: float) -> float:
        """The value at quantile `p`, so a threshold placed here splits the column p / (1-p).

        A column measured from a corpus is indexed directly. A column served by the fallback
        deciles is interpolated between the two bracketing deciles, which preserves uniformity in
        quantile at decile resolution.
        """
        values = self.sorted[column]
        if column not in self.interpolated:
            return values[min(int(p * len(values)), len(values) - 1)]
        span = len(values) - 1
        lo = min(int(p * span), span - 1)
        return values[lo] + (values[lo + 1] - values[lo]) * (p * span - lo)

    def _render(self, field: str, raw: float) -> str:
        """A sampled column value back into query syntax for `field`."""
        if field in ("usd", "eur", "tix"):
            return f"{raw:.2f}"
        if field == "released":
            return dt.date.fromordinal(int(raw)).isoformat()
        return str(max(MIN_NUMERIC_THRESHOLD, int(raw)))

    def numeric_predicate(self, field: str, rng: random.Random, shape: Shape = ANY_SHAPE) -> str:
        """A range predicate on `field` whose threshold(s) sit at uniformly-drawn quantiles."""
        column = NUMERIC_COLUMNS[field]
        # `released` is the column name, not a query field: it is spelled `year:2019` (what people
        # type) or `date>=2019-08-05` (the form that exercises full-precision comparison).
        as_year = field == "released" and rng.random() < YEAR_FRACTION
        name = {"released": "year" if as_year else "date"}.get(field, field)

        def threshold(p: float) -> str:
            rendered = self._render(field, self.quantile(column, p))
            return rendered[:4] if as_year else rendered

        bounded = rng.random() < BOUNDED_FRACTION if shape.bounded is None else shape.bounded
        if bounded:
            lo_p, hi_p = sorted((rng.random(), rng.random()))
            return f"{name}>={threshold(lo_p)} {name}<={threshold(hi_p)}"
        ops = NUMERIC_OPS if field in ("pow", "tou", "cmc", "loyalty") else RANGE_OPS
        return f"{name}{rng.choice(ops)}{threshold(rng.random())}"

    @staticmethod
    def _quote(value: str) -> str:
        """A vocabulary value in a form the lexer accepts — quoted only when it has to be."""
        return value if BARE_VALUE_RE.match(value) else f'"{value}"'

    def predicate(self, family: str, rng: random.Random, shape: Shape = ANY_SHAPE) -> str:
        """One predicate from `family`, drawn from the corpus universe where there is one."""
        if family in STATIC_VALUES:
            return rng.choice(STATIC_VALUES[family])
        if family in NUMERIC_COLUMNS:
            return self.numeric_predicate(family, rng, shape)
        if family == "rarity":
            return f"r{rng.choice(RARITY_OPS)}{rng.choice(self.rarities)}"
        if family == "name":
            # `name:` is a SUBSTRING match, and people search the distinctive word — "bolt", not
            # "ligh". Taking only a leading prefix of the full name would never produce `name:bolt`
            # for "Lightning Bolt", so pick a word from anywhere in the name, then sometimes shorten
            # it to a prefix. Full words are selective, short prefixes are broad; both are real.
            words = [w for w in re.split(r"[^a-z0-9]+", self._pick("name", rng)) if w] or ["a"]
            word = rng.choice(words)
            if rng.random() < NAME_PREFIX_FRACTION:
                word = word[: rng.randint(*NAME_PREFIX_LEN)]
            return f"name:{word}"
        return f"{VOCAB_PREFIXES[family]}:{self._quote(self._pick(family, rng))}"

    # ─── Query assembly ───────────────────────────────────────────────────────

    @staticmethod
    def _restrict[T](table: tuple[list[T], list[float]], allowed: frozenset[T] | None) -> tuple[list[T], list[float]]:
        """Drop keys a shape excludes, keeping the mode's relative weights over what is left."""
        keys, weights = table
        if allowed is None:
            return keys, weights
        kept = [(k, w) for k, w in zip(keys, weights, strict=True) if k in allowed]
        return [k for k, _ in kept], [w for _, w in kept]

    def _draw_families(self, rng: random.Random, count: int, shape: Shape = ANY_SHAPE) -> list[str]:
        """Up to `count` distinct families, weighted by mode, in draw order.

        Draws with rejection rather than `k=count` sampling because the weights are the point: a
        without-replacement draw would renormalise them after each pick. Retries up to
        MAX_FAMILY_DRAWS times per requested predicate, so a narrow shape pool still lands its
        count instead of silently returning fewer; it stops early once the pool is exhausted.
        """
        keys, weights = self._restrict(self.families, shape.families)
        drawn: list[str] = []
        for _ in range(count * MAX_FAMILY_DRAWS):
            if len(drawn) == count or len(drawn) == len(keys):
                break
            family = rng.choices(keys, weights=weights)[0]
            if family not in drawn:
                drawn.append(family)
        return drawn

    def query(self, rng: random.Random, shape: Shape = ANY_SHAPE) -> str:
        """One flat conjunction: a few predicates from distinct families, weighted by mode.

        A `shape` narrows the family pool and can pin the predicate count. Because families are
        drawn without replacement, a pinned count larger than the pool yields the pool.
        """
        count = shape.predicates if shape.predicates is not None else self._choose(self.predicate_counts, rng)
        return " ".join(self.predicate(f, rng, shape) for f in self._draw_families(rng, count, shape))

    def structured_query(self, rng: random.Random, shape: Shape = ANY_SHAPE) -> dict[str, str]:
        """One query drawn across connective structures, not just flat conjunctions.

        Returns `query`, `structure`, and `families` (a `+`-joined key for aggregating results by which
        fields were involved). Use this where the point is engine coverage — ORs, nested parens and
        negations are separate code paths. Use `query` where the point is cost-model calibration,
        whose baselines were taken over flat conjunctions.
        """
        structure = self._choose(self._restrict(self.structure_names, shape.structures), rng)
        template = STRUCTURES[structure][1]
        # A pinned predicate count only narrows a structure that can honour it; the template's own
        # arity is what the connective needs (an `or2` with one predicate is not an `or2`).
        arity = template.count("{")
        if structure == "regex":
            # Either the bare fragment or the fragment anchored by one ordinary predicate.
            parts = [rng.choice(REGEX_FRAGMENTS)]
            families = self._draw_families(rng, 1, shape) if rng.random() < REGEX_ANCHOR_FRACTION else []
            parts += [self.predicate(f, rng, shape) for f in families]
            return {"query": " ".join(parts), "structure": structure, "families": "+".join(["regex", *families])}

        families = self._draw_families(rng, arity, shape)
        parts = [self.predicate(f, rng, shape) for f in families]
        if len(parts) < arity:
            # Short a family: the shape's pool is smaller than this structure's arity. Degrade to
            # the conjunction of what we got rather than looping for a family that cannot come.
            structure, template = f"and{len(parts)}", " ".join(f"{{{i}}}" for i in range(len(parts)))
        return {"query": template.format(*parts), "structure": structure, "families": "+".join(sorted(families))}

    # ─── Result parameters ────────────────────────────────────────────────────

    def unique(self, rng: random.Random, shape: Shape = ANY_SHAPE) -> str:
        """A distinct-on, weighted by mode and narrowed by `shape`."""
        return self._choose(self._restrict(self.uniques, shape.unique), rng)

    def orderby(self, rng: random.Random, shape: Shape = ANY_SHAPE) -> str:
        """An orderby, weighted by mode. Which one gates StreamedSelect/PlanePopcountOrder."""
        return self._choose(self._restrict(self.orderbys, shape.orderby), rng)

    def prefer(self, rng: random.Random) -> str:
        """A printing-preference, weighted by mode.

        Exposed alongside `unique`/`orderby` because it is not merely a result-shaping knob: the
        card- and artwork-mode match kernels may stop at the first qualifying printing under
        `default` (printings are stored in prefer-desc order, so the first match IS the pick) and
        must score every printing under any other value. A harness that pins this to `default`
        measures only the short-circuiting path.
        """
        return self._choose(self.prefers, rng)

    def params(self, rng: random.Random, shape: Shape = ANY_SHAPE) -> dict[str, str | int]:
        """Every result-shaping parameter at once: unique, orderby, prefer, direction, offset."""
        return {
            "unique": self.unique(rng, shape),
            "orderby": self.orderby(rng, shape),
            "prefer": self._choose(self.prefers, rng),
            "direction": self._choose(self.directions, rng),
            "offset": self._choose(self.offsets, rng),
        }
