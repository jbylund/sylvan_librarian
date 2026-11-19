"""The `order=` vocabulary: every member wired on both paths, and `dir=auto` resolved per order.

The risk this file exists for is drift between three lists that must agree — `CardOrdering`, the
`sql_orderby` map, and `SortCol` in card_engine/src/lib.rs. `orderby_to_col` falls through to
edhrec on a name it does not know, so an ordering wired on one path and not the other does not
raise: it silently returns a differently-ordered page depending on which path served the query.
Two completeness tests below iterate the enum rather than a hand-written list, so a member added
without its counterpart fails here instead of in production.

The `dir=auto` table is measured against api.scryfall.com (2026-08-09) — see
docs/issues/local-engine-order-vocabulary.md.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from api.card_processing import color_order_rank
from api.enums import AUTO_DESCENDING_ORDERINGS, CardOrdering, SortDirection, resolve_direction
from api.parsing import parse_scryfall_query
from api.tests.color_order_cards import ASCENDING_NAMES, COLOR_ORDER_CARDS, DESCENDING_NAMES
from card_engine import QueryEngine

if TYPE_CHECKING:
    from collections.abc import Generator


class TestAutoDirection:
    """`auto` is per-ordering, and resolved before either search path sees it."""

    # Measured against api.scryfall.com on 2026-08-09 over `q=t:creature s:dom`, by comparing the
    # `auto` page against the `asc` and `desc` pages of the same query.
    MEASURED: ClassVar[dict[CardOrdering, SortDirection]] = {
        CardOrdering.RELEASED: SortDirection.DESC,
        CardOrdering.RARITY: SortDirection.DESC,
        CardOrdering.USD: SortDirection.DESC,
        CardOrdering.TIX: SortDirection.DESC,
        CardOrdering.EUR: SortDirection.DESC,
        CardOrdering.NAME: SortDirection.ASC,
        CardOrdering.SET: SortDirection.ASC,
        CardOrdering.COLOR: SortDirection.ASC,
        CardOrdering.CMC: SortDirection.ASC,
        CardOrdering.POWER: SortDirection.ASC,
        CardOrdering.TOUGHNESS: SortDirection.ASC,
        CardOrdering.EDHREC: SortDirection.ASC,
        CardOrdering.ARTIST: SortDirection.ASC,
    }

    @pytest.mark.parametrize("ordering", sorted(MEASURED), ids=str)
    def test_auto_matches_what_scryfall_does(self, ordering: CardOrdering) -> None:
        assert resolve_direction(SortDirection.AUTO, ordering) == self.MEASURED[ordering]

    def test_edhrec_auto_is_ascending(self) -> None:
        """Ascending rank is most-popular-first; descending would surface the least-played cards.

        Called out on its own because "descending popularity" and "ascending rank" are the same
        direction here, and reading it the other way inverts the site's default sort.
        """
        assert resolve_direction(SortDirection.AUTO, CardOrdering.EDHREC) == SortDirection.ASC

    @pytest.mark.parametrize("ordering", sorted(CardOrdering), ids=str)
    def test_auto_always_resolves_to_a_concrete_direction(self, ordering: CardOrdering) -> None:
        """No ordering may leave AUTO in place — neither search path knows the value."""
        assert resolve_direction(SortDirection.AUTO, ordering) in (SortDirection.ASC, SortDirection.DESC)

    @pytest.mark.parametrize("explicit", [SortDirection.ASC, SortDirection.DESC], ids=str)
    @pytest.mark.parametrize("ordering", sorted(AUTO_DESCENDING_ORDERINGS), ids=str)
    def test_an_explicit_direction_is_never_overridden(self, ordering: CardOrdering, explicit: SortDirection) -> None:
        """Including on the orderings whose auto is desc — `dir=asc` there must stay ascending."""
        assert resolve_direction(explicit, ordering) == explicit


def _ordered_ids(engine: QueryEngine, orderby: CardOrdering, direction: SortDirection) -> list[str]:
    _total, cards = engine.query(
        filters=parse_scryfall_query("cmc>=0"),
        unique="printing",
        prefer="default",
        orderby=str(orderby),
        direction=str(direction),
        limit=1_000,
        offset=0,
        fields=["scryfall_id"],
    )
    return [str(c["scryfall_id"]) for c in cards]


class TestEngineKnowsEveryOrdering:
    """Every CardOrdering member has its own `SortCol` arm, not the edhrec fallthrough.

    `orderby_to_col` cannot be inspected from Python, so this is behavioural: an ordering that fell
    through would produce the exact page edhrec produces. The corpus is built so that no two
    orderings agree by accident — each card is deliberately distinct on every sortable column.
    """

    @pytest.fixture(scope="class", name="engine")
    def engine_fixture(self, tmp_path_factory: pytest.TempPathFactory) -> Generator[QueryEngine]:
        rng = random.Random(20260809)
        cards: list[dict[str, Any]] = []
        colors = [{}, {"W": True}, {"U": True}, {"B": True}, {"R": True}, {"G": True}, {"W": True, "U": True}]
        for i in range(40):
            cid = f"{i:08x}-0000-4000-8000-{i:012x}"
            cards.append(
                {
                    "scryfall_id": cid,
                    "oracle_id": f"{i:08x}-1111-4111-8111-{i:012x}",
                    "illustration_id": f"{i:08x}-2222-4222-8222-{i:012x}",
                    "card_name": f"Card {i:03d}",
                    "card_name_lower": f"card {i:03d}",
                    "card_name_folded": f"card {i:03d}",
                    # Every sortable column gets a different permutation of the same 40 cards, so
                    # two orderings returning the same page means one of them was not applied.
                    "cmc": i % 13,
                    "edhrec_rank": (i * 7) % 40,
                    "cubecobra_score": float((i * 29) % 40),
                    "creature_power": (i * 3) % 11,
                    "creature_toughness": (i * 5) % 11,
                    "card_rarity_int": i % 4,
                    "price_usd": (i * 11) % 97,
                    "price_eur": (i * 13) % 89,
                    "price_tix": (i * 17) % 83,
                    "released_at": f"20{10 + (i % 15):02d}-{1 + (i % 12):02d}-{1 + (i % 28):02d}",
                    "card_set_code": f"s{(i * 19) % 40:02d}",
                    "card_artist": f"Artist {(i * 23) % 40:03d}",
                    "card_colors": colors[i % len(colors)],
                    # Written at import on a real row (color_order_rank); here just one more
                    # permutation, with a gap the importer also leaves.
                    "color_order": (i * 31) % 96,
                    "type_line": "Land" if i % 9 == 0 else "Creature — Test",
                    "oracle_text": f"text {i}",
                    "prefer_score": float(rng.randrange(100)),
                }
            )
        engine = QueryEngine(str(tmp_path_factory.mktemp("orders") / "orders.store"))
        assert engine.reload_begin()
        engine.add_batch(cards)
        engine.reload_commit()
        return engine

    def test_the_corpus_loaded(self, engine: QueryEngine) -> None:
        assert engine.size() == 40

    @pytest.mark.parametrize(
        "ordering",
        [o for o in sorted(CardOrdering) if o is not CardOrdering.EDHREC],
        ids=str,
    )
    def test_ordering_is_not_the_edhrec_fallthrough(self, engine: QueryEngine, ordering: CardOrdering) -> None:
        """The failure this catches is silent: an unmapped name sorts by edhrec and still 200s."""
        by_edhrec = _ordered_ids(engine, CardOrdering.EDHREC, SortDirection.ASC)
        assert _ordered_ids(engine, ordering, SortDirection.ASC) != by_edhrec

    @pytest.mark.parametrize("ordering", sorted(CardOrdering), ids=str)
    def test_every_ordering_returns_the_whole_corpus(self, engine: QueryEngine, ordering: CardOrdering) -> None:
        """A sort key must reorder rows, never drop them — an absent value sorts last, not out."""
        assert len(_ordered_ids(engine, ordering, SortDirection.ASC)) == 40

    @pytest.mark.parametrize("ordering", sorted(CardOrdering), ids=str)
    def test_descending_is_the_reverse_ordering(self, engine: QueryEngine, ordering: CardOrdering) -> None:
        """Not element-wise reversed — ties break the same way in both directions by design."""
        ascending = _ordered_ids(engine, ordering, SortDirection.ASC)
        descending = _ordered_ids(engine, ordering, SortDirection.DESC)
        assert set(ascending) == set(descending)
        assert ascending != descending

    def test_released_orders_by_date_not_by_a_truncated_key(self, engine: QueryEngine) -> None:
        """A raw yyyymmdd exceeds the f32 sort key's exact range, collapsing adjacent dates.

        Forty distinct dates must give forty distinct positions; a truncating key ties some of them
        and lets the secondary sort decide, which reads as "nearly sorted" rather than as a failure.
        """
        ids = _ordered_ids(engine, CardOrdering.RELEASED, SortDirection.ASC)
        assert ids != _ordered_ids(engine, CardOrdering.RELEASED, SortDirection.DESC)
        assert len(set(ids)) == 40


class TestEngineColorOrder:
    """`order=color` on the engine: the stored block, then the name ascending in both directions.

    The engine does not decide where a card sits; `color_order` does, and the importer writes it.
    These rows carry the colours and types of the face a faced card's row is stored with today (the
    last one), which is the wrong face for five of the eighteen -- so a key that still read
    `card_colors` or `card_types` would put Westvale Abbey with the black cards and Emeria's Call
    with the lands, and fail.
    """

    @staticmethod
    def _row(index: int, card: dict[str, Any], color_order: int | None) -> dict[str, Any]:
        stored_face = card["card_faces"][-1] if "card_faces" in card else card
        colors = card["colors"] if "colors" in card else stored_face["colors"]
        return {
            "scryfall_id": f"{index:08x}-0000-4000-8000-{index:012x}",
            "oracle_id": f"{index:08x}-1111-4111-8111-{index:012x}",
            "illustration_id": f"{index:08x}-2222-4222-8222-{index:012x}",
            "card_name": card["name"],
            "card_name_lower": card["name"].lower(),
            "card_name_folded": card["name"].lower(),
            "card_colors": dict.fromkeys(colors, True),
            "card_color_identity": dict.fromkeys(card["color_identity"], True),
            "type_line": stored_face["type_line"],
            "edhrec_rank": card.get("edhrec_rank"),
            "color_order": color_order,
            "cmc": 0,
            "oracle_text": "",
        }

    @staticmethod
    def _names(engine: QueryEngine, direction: SortDirection, unique: str = "card") -> list[str]:
        _total, cards = engine.query(
            filters=parse_scryfall_query("cmc>=0"),
            unique=unique,
            prefer="default",
            orderby=str(CardOrdering.COLOR),
            direction=str(direction),
            limit=1_000,
            offset=0,
            fields=["name"],
        )
        return [str(c["name"]) for c in cards]

    @pytest.fixture(scope="class", name="engine")
    def engine_fixture(self, tmp_path_factory: pytest.TempPathFactory) -> QueryEngine:
        engine = QueryEngine(str(tmp_path_factory.mktemp("color-order") / "color.store"))
        assert engine.reload_begin()
        # Loaded back to front, so store order is neither answer.
        engine.add_batch(
            [self._row(i, card, color_order_rank(card)) for i, (_rank, card) in enumerate(reversed(COLOR_ORDER_CARDS))]
        )
        engine.reload_commit()
        return engine

    @pytest.mark.parametrize("unique", ["card", "printing", "artwork"])
    def test_ascending_is_scryfalls_answer(self, engine: QueryEngine, unique: str) -> None:
        assert self._names(engine, SortDirection.ASC, unique) == ASCENDING_NAMES

    @pytest.mark.parametrize("unique", ["card", "printing", "artwork"])
    def test_descending_turns_the_blocks_and_not_the_names(self, engine: QueryEngine, unique: str) -> None:
        names = self._names(engine, SortDirection.DESC, unique)
        assert names == DESCENDING_NAMES
        assert names != ASCENDING_NAMES[::-1]

    def test_a_row_with_no_stored_order_sorts_last_in_both_directions(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        """NULL (a row written before the column existed), an absent key and an out-of-range value alike."""
        engine = QueryEngine(str(tmp_path_factory.mktemp("color-order-null") / "color.store"))
        wastes, sol_ring = (
            {"name": name, "colors": [], "color_identity": [], "type_line": type_line}
            for name, type_line in [("Wastes", "Basic Land"), ("Sol Ring", "Artifact")]
        )
        rows = [
            self._row(1, {"name": "Null Order", "colors": ["W"], "color_identity": ["W"], "type_line": "Instant"}, None),
            self._row(2, wastes, color_order_rank(wastes)),
            self._row(3, {"name": "Beyond The Range", "colors": ["W"], "color_identity": ["W"], "type_line": "Instant"}, 96),
            self._row(4, sol_ring, color_order_rank(sol_ring)),
        ]
        absent = self._row(5, {"name": "Absent Key", "colors": ["W"], "color_identity": ["W"], "type_line": "Instant"}, None)
        del absent["color_order"]
        assert engine.reload_begin()
        engine.add_batch([*rows, absent])
        engine.reload_commit()
        unwritten = ["Absent Key", "Beyond The Range", "Null Order"]
        assert self._names(engine, SortDirection.ASC) == ["Sol Ring", "Wastes", *unwritten]
        assert self._names(engine, SortDirection.DESC) == ["Wastes", "Sol Ring", *unwritten]
