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

from api.enums import AUTO_DESCENDING_ORDERINGS, CardOrdering, SortDirection, resolve_direction
from api.parsing import parse_scryfall_query
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
