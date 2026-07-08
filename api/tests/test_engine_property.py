"""Differential property tests: engine totals vs an independent Python oracle.

The engine's narrowing machinery is threshold-gated (NARROW_FLOOR at 1,000
postings, bitmap promotion, memoization floors, near-total drops, bigram plane
tiers), and the curated 87-printing fixture in test_engine_unit.py never
crosses any of those lines — so this file builds a deterministic ~3,000-card
/ ~9,000-printing store whose distributions do, generates a seeded set of
queries across every composition shape (ands, ors, nested parens, negations),
and asserts engine totals against a brute-force three-valued evaluator that
shares no code with the engine.

Narrowing is advisory-by-design, so an unsound candidate set (one that drops
a real match) is exactly what these tests catch: the engine total comes out
low. The reference implements SQL-style ternary logic (a missing field is
NULL; NOT NULL = NULL; only True matches), mirroring FilterExpr::tri.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

import pytest

from api.parsing import parse_scryfall_query
from card_engine import QueryEngine

WORDS = ["ancient", "storm", "fire", "dragon", "counter", "force", "dark", "light", "path", "bolt", "angel", "wild"]
ORACLE_WORDS = ["draw", "destroy", "exile", "flying", "trample", "token", "sacrifice", "haste", "vigilance", "return"]
SUBTYPES = ["Goblin", "Elf", "Wizard", "Dragon", "Angel", "Zombie", "Human", "Merfolk"]
KEYWORDS = ["Flying", "Trample", "Haste", "Vigilance", "Deathtouch", "Lifelink"]
SETS = ["lea", "m21", "znr", "neo", "mom", "ltr", "woe", "mkm"]
COLORS = "wubrg"
N_CARDS = 3000

FRAGMENTS = [
    # colors / identity across ops (":" on id is subset; on c it is contains)
    "c:g",
    "c:u",
    "c:wu",
    "c=r",
    "c<=bg",
    "-c:g",
    "id:g",
    "id:ub",
    "id=w",
    # types (plane algebra, incl. negation and equality)
    "t:creature",
    "t:instant",
    "t:artifact",
    "-t:creature",
    "t:enchantment",
    # subtypes / keywords (tag postings; one absent value for the empty-proof path)
    "t:goblin",
    "t:elf",
    "t:wizard",
    "kw:flying",
    "kw:deathtouch",
    "t:sliver",
    # numerics with nulls (power/toughness absent on non-creatures)
    "cmc=2",
    "cmc>4",
    "cmc<=1",
    "pow>2",
    "pow<=1",
    "tou=3",
    "-pow=4",
    # price: broad and selective bands, nulls, both directions (range bitmaps)
    "usd<5",
    "usd<0.5",
    "usd>8",
    "usd>=2",
    "-usd>8",
    # sets / years (printing space)
    "set:lea",
    "set:mkm",
    "year:2015",
    "year>=2020",
    "year<2005",
    # names: trigram, bigram (2-char), and sub-bigram (unindexable)
    "name:storm",
    "name:dr",
    "name:an",
    "name:q",
    # oracle text: memoizable and short
    "o:draw",
    "o:destroy",
    "o:flying",
    "o:xy",
]


def _make_cards(rng: random.Random) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for i in range(N_CARDS):
        colors = sorted({rng.choice(COLORS).upper() for _ in range(rng.choice([0, 1, 1, 1, 2, 2, 3]))})
        # identity ⊇ colors usually, with deliberate Fallaji-style exceptions
        identity = sorted(set(colors) | ({rng.choice(COLORS).upper()} if rng.random() < 0.25 else set()))
        if rng.random() < 0.005:
            colors, identity = sorted("WUBRG"), ["G"]
        types = ["Creature"] if rng.random() < 0.55 else [rng.choice(["Instant", "Artifact", "Enchantment", "Sorcery", "Land"])]
        if rng.random() < 0.1:
            types.append("Artifact")
        is_creature = "Creature" in types
        name = f"{rng.choice(WORDS)} {rng.choice(WORDS)} {i}"
        n_printings = rng.choice([1, 2, 3, 3, 4, 5])
        oracle = " ".join(rng.sample(ORACLE_WORDS, rng.randint(1, 3))) if rng.random() < 0.9 else None
        # Field shapes mirror the production corpus exactly: colors/identity/
        # keywords are JSONB objects (the loader reads dict KEYS), types and
        # subtypes are capitalized lists (the parser capitalizes query values).
        # UUIDs start at 1 — the all-zero UUID is the loader's null sentinel.
        card = {
            "oracle_id": f"00000000-0000-0000-0000-{i + 1:012d}",
            "card_name": name,
            "oracle_text": oracle,
            "card_colors": dict.fromkeys(colors, True),
            "card_color_identity": dict.fromkeys(identity, True),
            "card_types": types,
            "card_subtypes": sorted(rng.sample(SUBTYPES, rng.randint(1, 2))) if is_creature else [],
            "card_keywords": dict.fromkeys(rng.sample(KEYWORDS, rng.randint(0, 2)), True) if is_creature else {},
            "cmc": rng.choice([0, 1, 1, 2, 2, 3, 3, 4, 5, 6, 8]) if "Land" not in types else None,
            "creature_power": rng.randint(0, 8) if is_creature else None,
            "creature_toughness": rng.randint(0, 8) if is_creature else None,
        }
        for p in range(n_printings):
            printing = dict(card)
            printing["scryfall_id"] = f"00000000-0000-0000-{p + 1:04d}-{i + 1:012d}"
            printing["card_set_code"] = rng.choice(SETS)
            # usd: ~15% null, heavy sub-$5 band so usd<5 is guard-broad
            printing["price_usd"] = (
                None if rng.random() < 0.15 else round(rng.choice([0.1, 0.3, 0.8, 1.5, 3.0, 6.0, 12.0]) * rng.uniform(0.5, 1.5), 2)
            )
            printing["released_at"] = f"{rng.randint(1995, 2026)}-06-15"
            cards.append(printing)
    return cards


# ─── The reference oracle: three-valued eval sharing no code with the engine ──


def _tri_cmp(value: float | None, op: str, rhs: float) -> bool | None:
    if value is None:
        return None
    return {"=": value == rhs, "<": value < rhs, "<=": value <= rhs, ">": value > rhs, ">=": value >= rhs}[op]


def _ref_leaf(frag: str, card: dict[str, Any]) -> bool | None:  # noqa: PLR0911, PLR0912, C901
    """Evaluate one query fragment against one printing row, SQL-ternary style."""
    field, _, rest = frag.partition(":")
    if frag.startswith(("c:", "c=", "c<=")):
        have = {c.lower() for c in card["card_colors"]}  # dict: iterates keys
        if frag.startswith("c:"):
            return set(frag[2:]) <= have
        if frag.startswith("c<="):
            return have <= set(frag[3:])
        return have == set(frag[2:])
    if frag.startswith(("id:", "id=")):
        have = {c.lower() for c in card["card_color_identity"]}
        return have <= set(frag[3:]) if frag.startswith("id:") else have == set(frag[3:])
    if field == "t":
        want = rest.capitalize()
        return want in card["card_types"] or want in card["card_subtypes"]
    if field == "kw":
        return rest.capitalize() in card["card_keywords"]
    if frag.startswith("cmc"):
        m = _split_num(frag, "cmc")
        return _tri_cmp(card["cmc"], *m)
    if frag.startswith("pow"):
        m = _split_num(frag, "pow")
        return _tri_cmp(card["creature_power"], *m)
    if frag.startswith("tou"):
        m = _split_num(frag, "tou")
        return _tri_cmp(card["creature_toughness"], *m)
    if frag.startswith("usd"):
        m = _split_num(frag, "usd")
        return _tri_cmp(card["price_usd"], *m)
    if field == "set":
        return card["card_set_code"] == rest
    if frag.startswith("year"):
        year = int(card["released_at"][:4])
        if frag.startswith("year:"):
            return year == int(rest)
        m = _split_num(frag, "year")
        return _tri_cmp(year, *m)
    if field == "name":
        return rest in card["card_name"].lower()
    if field == "o":
        text = card["oracle_text"]
        # missing oracle text is interned as "" at load: contains is False
        return rest in (text or "").lower()
    msg = f"reference has no rule for {frag!r}"
    raise AssertionError(msg)


def _split_num(frag: str, prefix: str) -> tuple[str, float]:
    rest = frag[len(prefix) :]
    op = rest[:2] if rest[:2] in ("<=", ">=") else rest[0]
    val = rest[len(op) :]
    return ("=" if op == ":" else op, float(val))


def _ref_eval(node: Any, card: dict[str, Any]) -> bool | None:
    """Kleene three-valued And/Or/Not over parsed shape tuples."""
    kind = node[0]
    if kind == "leaf":
        return _ref_leaf(node[1], card)
    if kind == "not":
        inner = _ref_eval(node[1], card)
        return None if inner is None else not inner
    vals = [_ref_eval(c, card) for c in node[1]]
    if kind == "and":
        if any(v is False for v in vals):
            return False
        return None if any(v is None for v in vals) else True
    if any(v is True for v in vals):
        return True
    return None if any(v is None for v in vals) else False


def _ref_totals(shape: Any, cards: list[dict[str, Any]]) -> tuple[int, int]:
    """(unique=card, unique=printing) totals: only True matches."""
    by_oracle: dict[str, list[dict[str, Any]]] = {}
    for c in cards:
        by_oracle.setdefault(c["oracle_id"], []).append(c)
    card_total = printing_total = 0
    for printings in by_oracle.values():
        hits = sum(_ref_eval(shape, p) is True for p in printings)
        printing_total += hits
        card_total += hits > 0
    return card_total, printing_total


# ─── Query generation: fragments composed across every shape ─────────────────


def _gen_queries(rng: random.Random, count: int) -> list[tuple[str, Any]]:
    """(query string, reference shape) pairs across single/and/or/nested/neg."""
    out: list[tuple[str, Any]] = []
    leaf = lambda f: ("leaf", f.lstrip("-")) if not f.startswith("-") else ("not", ("leaf", f[1:]))  # noqa: E731
    while len(out) < count:
        k = rng.random()
        picks = rng.sample(FRAGMENTS, 4)
        if k < 0.2:
            q, shape = picks[0], leaf(picks[0])
        elif k < 0.45:
            n = rng.choice([2, 2, 3])
            q = " ".join(picks[:n])
            shape = ("and", [leaf(p) for p in picks[:n]])
        elif k < 0.65:
            q = f"{picks[0]} or {picks[1]}"
            shape = ("or", [leaf(picks[0]), leaf(picks[1])])
        elif k < 0.8:
            q = f"({picks[0]} {picks[1]}) or ({picks[2]} {picks[3]})"
            shape = ("or", [("and", [leaf(picks[0]), leaf(picks[1])]), ("and", [leaf(picks[2]), leaf(picks[3])])])
        elif k < 0.92:
            q = f"{picks[0]} ({picks[1]} or {picks[2]})"
            shape = ("and", [leaf(picks[0]), ("or", [leaf(picks[1]), leaf(picks[2])])])
        else:
            q = f"{picks[0]} -({picks[1]} or {picks[2]})"
            shape = ("and", [leaf(picks[0]), ("not", ("or", [leaf(picks[1]), leaf(picks[2])]))])
        out.append((q, shape))
    return out


@pytest.fixture(scope="module", name="property_setup")
def property_setup_fixture(tmp_path_factory: pytest.TempPathFactory) -> Generator[tuple[QueryEngine, list[dict[str, Any]]]]:
    rng = random.Random(20260708)
    cards = _make_cards(rng)
    engine = QueryEngine(str(tmp_path_factory.mktemp("prop") / "prop.store"))
    assert engine.reload_begin()
    for i in range(0, len(cards), 2000):
        engine.add_batch(cards[i : i + 2000])
    engine.reload_commit()
    return engine, cards


def _engine_total(engine: QueryEngine, query: str, unique: str) -> int:
    total, _ = engine.query(
        filters=parse_scryfall_query(query),
        unique=unique,
        prefer="default",
        orderby="edhrec",
        direction="asc",
        limit=1,
        offset=0,
    )
    return total


class TestEnginePropertyParity:
    """Engine totals equal the reference oracle across 250 seeded queries."""

    def test_store_is_large_enough_to_cross_thresholds(self, property_setup: tuple[QueryEngine, list[dict[str, Any]]]) -> None:
        engine, cards = property_setup
        assert engine.size() == len(cards) >= 6000, "the whole point is crossing the size-gated paths"
        broad = _engine_total(engine, "usd<5", "printing")
        assert broad > 1000, "usd<5 must be broad enough to trip NARROW_FLOOR and the bitmap paths"

    def test_totals_match_reference_across_shapes(self, property_setup: tuple[QueryEngine, list[dict[str, Any]]]) -> None:
        engine, cards = property_setup
        queries = _gen_queries(random.Random(42), 250)
        failures = []
        for q, shape in queries:
            want_card, want_printing = _ref_totals(shape, cards)
            got_card = _engine_total(engine, q, "card")
            got_printing = _engine_total(engine, q, "printing")
            if (got_card, got_printing) != (want_card, want_printing):
                failures.append(f"{q!r}: engine=({got_card},{got_printing}) reference=({want_card},{want_printing})")
        assert not failures, "engine/reference divergence:\n" + "\n".join(failures[:15])

    def test_every_fragment_matches_reference_standalone(self, property_setup: tuple[QueryEngine, list[dict[str, Any]]]) -> None:
        engine, cards = property_setup
        failures = []
        for frag in FRAGMENTS:
            shape = ("not", ("leaf", frag[1:])) if frag.startswith("-") else ("leaf", frag)
            want = _ref_totals(shape, cards)
            got = (_engine_total(engine, frag, "card"), _engine_total(engine, frag, "printing"))
            if got != want:
                failures.append(f"{frag!r}: engine={got} reference={want}")
        assert not failures, "fragment divergence:\n" + "\n".join(failures)
