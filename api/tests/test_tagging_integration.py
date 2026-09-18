"""Integration tests for tag import functions."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock, patch

from api.scryfall_bulk_data_fetcher import BulkDataKey
from api.tag_import import _build_uuid_to_slug, _union_art_tags, import_art_tags, import_oracle_tags
from api.tests.helpers import make_raw_card

if TYPE_CHECKING:
    from api.api_resource import APIResource

ORACLE_TAGS_FIXTURE = [
    {
        "id": "uuid-flying",
        "slug": "flying",
        "parent_ids": [],
        "child_ids": ["uuid-evasion"],
        "taggings": [
            {"oracle_id": "card-a", "weight": "strong"},
            {"oracle_id": "card-b", "weight": "median"},
        ],
    },
    {
        "id": "uuid-evasion",
        "slug": "evasion",
        "parent_ids": ["uuid-flying"],
        "child_ids": [],
        "taggings": [
            {"oracle_id": "card-a", "weight": "strong"},
        ],
    },
]

ART_TAGS_FIXTURE = [
    {
        "id": "uuid-dragon",
        "slug": "dragon",
        "parent_ids": [],
        "child_ids": [],
        "taggings": [
            {"illustration_id": "illus-x", "weight": "very_strong"},
        ],
    },
]


def _make_mock_conn_pool(tagged_in_db: list[dict] | None = None) -> tuple[MagicMock, MagicMock]:
    """Return a mock conn_pool whose cursor.fetchall() yields the given rows."""
    cursor = MagicMock()
    cursor.__enter__ = lambda _: cursor
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.return_value = tagged_in_db or []
    cursor.rowcount = 0

    conn = MagicMock()
    conn.__enter__ = lambda _: conn
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    pool = MagicMock()
    pool.connection.return_value.__enter__ = lambda _: conn
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool, cursor


class TestBuildUuidToSlug:
    def test_basic(self) -> None:
        assert _build_uuid_to_slug(ORACLE_TAGS_FIXTURE) == {
            "uuid-flying": "flying",
            "uuid-evasion": "evasion",
        }


class TestImportOracleTags:
    def test_calls_stream_for_oracle_tags(self) -> None:
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(ORACLE_TAGS_FIXTURE)

        import_oracle_tags(pool, fetcher)

        fetcher.stream_data_for_key.assert_called_once_with(BulkDataKey.ORACLE_TAGS)

    def test_returns_summary(self) -> None:
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(ORACLE_TAGS_FIXTURE)

        result = import_oracle_tags(pool, fetcher)

        assert result["tags_imported"] == 2
        assert result["cards_with_tags"] == 2  # card-a and card-b
        assert "duration_seconds" in result


class TestAncestorPropagation:
    """Ancestor slugs must be added to each card's tag set at import time.

    The SQL filter (card_oracle_tags @> {'dual-land': True}) only matches cards that carry the
    slug directly.  Without ancestor propagation a card tagged only with 'cycle-abu-dual-land'
    (a child of 'dual-land') would be invisible to an otag:dual-land query.
    """

    def test_child_tagged_card_gets_parent_slug(self) -> None:
        # card-c is only tagged with evasion (child of flying).
        # After import it must also carry 'flying' so otag:flying finds it.
        fixture = [
            {"id": "uuid-flying", "slug": "flying", "parent_ids": [], "child_ids": ["uuid-evasion"], "taggings": []},
            {
                "id": "uuid-evasion",
                "slug": "evasion",
                "parent_ids": ["uuid-flying"],
                "child_ids": [],
                "taggings": [{"oracle_id": "card-c", "weight": "strong"}],
            },
        ]
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(fixture)

        captured: dict = {}

        def capture(conn, id_column, tag_column, id_to_tags) -> tuple[int, int]:
            captured["id_to_tags"] = id_to_tags
            return (0, 0)

        with patch("api.tag_import._sync_card_tags", side_effect=capture), patch("api.tag_import._sync_hierarchy"):
            import_oracle_tags(pool, fetcher)

        card_tags = captured["id_to_tags"]["card-c"]
        assert card_tags.get("evasion") is True
        assert card_tags.get("flying") is True  # ancestor propagated

    def test_direct_tagged_card_unaffected(self) -> None:
        # card-b is only tagged with the root tag flying (no parent).
        # It should have exactly flying and nothing else.
        fixture = [
            {
                "id": "uuid-flying",
                "slug": "flying",
                "parent_ids": [],
                "child_ids": [],
                "taggings": [{"oracle_id": "card-b", "weight": "median"}],
            },
        ]
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(fixture)

        captured: dict = {}

        def capture(conn, id_column, tag_column, id_to_tags) -> tuple[int, int]:
            captured["id_to_tags"] = id_to_tags
            return (0, 0)

        with patch("api.tag_import._sync_card_tags", side_effect=capture), patch("api.tag_import._sync_hierarchy"):
            import_oracle_tags(pool, fetcher)

        assert captured["id_to_tags"]["card-b"] == {"flying": True}


class TestImportArtTags:
    def test_calls_stream_for_art_tags(self) -> None:
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(ART_TAGS_FIXTURE)

        import_art_tags(pool, fetcher)

        fetcher.stream_data_for_key.assert_called_once_with(BulkDataKey.ART_TAGS)

    def test_returns_summary(self) -> None:
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(ART_TAGS_FIXTURE)

        with patch("api.tag_import._fetch_illustrations_shown", return_value=[("card-1", ["illus-x"])]):
            result = import_art_tags(pool, fetcher)

        assert result["tags_imported"] == 1
        # The dump covers one illustration; one card in the corpus shows it.
        assert result["illustrations_with_tags"] == 1
        assert result["cards_with_tags"] == 1
        assert "duration_seconds" in result

    def test_syncs_card_art_tags_keyed_on_the_card(self) -> None:
        """The column a record identifies is scryfall_id, because the union is per card.

        An illustration id no longer determines a row's whole value: a double-faced card's tags
        come from two of them, so nothing keyed on one illustration could write the row.
        """
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(ART_TAGS_FIXTURE)

        captured: dict = {}

        def capture(conn, id_column, tag_column, id_to_tags) -> tuple[int, int]:
            captured.update(id_column=id_column, tag_column=tag_column, id_to_tags=id_to_tags)
            return (0, 0)

        with (
            patch("api.tag_import._sync_card_tags", side_effect=capture),
            patch("api.tag_import._sync_hierarchy"),
            patch("api.tag_import._fetch_illustrations_shown", return_value=[("card-1", ["illus-x"])]),
        ):
            import_art_tags(pool, fetcher)

        assert captured["id_column"] == "scryfall_id"
        assert captured["tag_column"] == "card_art_tags"
        assert captured["id_to_tags"] == {"card-1": {"dragon": True}}


class TestUnionArtTags:
    """A card answers for every face it shows, not just its front.

    `illustration_id` is the FRONT face's on a merged multi-face row, so joining `card_art_tags`
    on it alone made a back-face-only tag unreachable. Measured against api.scryfall.com on
    2026-08-16: `arttag:snow e:khm` is 75 there against 73 for the front-only reading (Birgi //
    Harnfel and Esika // The Prismatic Bridge carry their snow on the back art), and
    `-art:human e:khm t:creature` is 135 there against 136 (the surplus being Valki // Tibalt).
    """

    TAGS: ClassVar[dict[str, dict[str, bool]]] = {
        "illus-front": {"human": True, "window": True},
        "illus-back": {"insect": True, "window": True},
    }

    def test_a_back_face_only_tag_reaches_the_card(self) -> None:
        assert _union_art_tags([("card-dfc", ["illus-front", "illus-back"])], self.TAGS) == {
            "card-dfc": {"human": True, "window": True, "insect": True},
        }

    def test_a_single_illustration_card_gets_that_illustrations_tags(self) -> None:
        assert _union_art_tags([("card-solo", ["illus-front"])], self.TAGS) == {"card-solo": {"human": True, "window": True}}

    def test_a_single_illustration_card_shares_the_dump_dict(self) -> None:
        """No copy on the overwhelming majority of rows: the union allocates only when it unions."""
        result = _union_art_tags([("card-solo", ["illus-front"])], self.TAGS)
        assert result["card-solo"] is self.TAGS["illus-front"]

    def test_an_untagged_illustration_is_omitted_not_emptied(self) -> None:
        """_sync_card_tags reads absence as "clear this row", so `{}` would only cost payload."""
        assert _union_art_tags([("card-untagged", ["illus-unknown"])], self.TAGS) == {}

    def test_a_face_whose_art_is_untagged_does_not_suppress_the_others(self) -> None:
        assert _union_art_tags([("card-dfc", ["illus-unknown", "illus-back"])], self.TAGS) == {
            "card-dfc": {"insect": True, "window": True},
        }


def _art_tags_of(api_resource: APIResource, scryfall_id: str) -> dict:
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT card_art_tags FROM magic.cards WHERE scryfall_id = %(sid)s", {"sid": scryfall_id})
        row = cursor.fetchone()
    return row["card_art_tags"] if row else {}


class TestArtTagsAgainstPostgres:
    """The whole path against a real database: import a card, import its art tags, read the row.

    `arttag:snow e:khm` was 73 against Scryfall's 75 for exactly the shape below -- a merged
    double-faced row whose `snow` tagging names the BACK face's illustration, which is not the
    `illustration_id` column.
    """

    FRONT_ILLUSTRATION = "aaaaaaaa-0000-4000-8000-000000000001"
    BACK_ILLUSTRATION = "aaaaaaaa-0000-4000-8000-000000000002"

    @staticmethod
    def _art_dump(illustration_id: str, slug: str) -> list[dict]:
        return [
            {
                "id": str(uuid.uuid4()),
                "slug": slug,
                "parent_ids": [],
                "child_ids": [],
                "taggings": [{"illustration_id": illustration_id, "weight": "very_strong"}],
            },
        ]

    def _import_dfc(self, api_resource: APIResource) -> str:
        card = make_raw_card(name="Snowfront Tester // Snowback Tester")
        card["layout"] = "transform"
        card["card_faces"] = [
            {"name": "Snowfront Tester", "type_line": "Creature — Human", "illustration_id": self.FRONT_ILLUSTRATION},
            {"name": "Snowback Tester", "type_line": "Creature — Insect", "illustration_id": self.BACK_ILLUSTRATION},
        ]
        assert api_resource.admin._upsert_cards([card])["status"] == "success"
        return card["id"]

    def test_a_back_face_tagging_lands_on_the_card(self, api_resource: APIResource) -> None:
        scryfall_id = self._import_dfc(api_resource)
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(self._art_dump(self.BACK_ILLUSTRATION, "snow"))

        import_art_tags(api_resource.app_context.reader_pool, fetcher)

        assert _art_tags_of(api_resource, scryfall_id) == {"snow": True}

    def test_the_front_face_still_lands(self, api_resource: APIResource) -> None:
        """The union adds the back; it does not cost the front, which is what already worked."""
        scryfall_id = self._import_dfc(api_resource)
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(self._art_dump(self.FRONT_ILLUSTRATION, "human"))

        import_art_tags(api_resource.app_context.reader_pool, fetcher)

        assert _art_tags_of(api_resource, scryfall_id) == {"human": True}

    def test_a_tagless_dump_clears_the_row(self, api_resource: APIResource) -> None:
        """Keying the sync on the card, not the illustration, keeps the clear path exact."""
        scryfall_id = self._import_dfc(api_resource)
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(self._art_dump(self.BACK_ILLUSTRATION, "snow"))
        import_art_tags(api_resource.app_context.reader_pool, fetcher)
        assert _art_tags_of(api_resource, scryfall_id) == {"snow": True}

        fetcher.stream_data_for_key.return_value = iter([])
        import_art_tags(api_resource.app_context.reader_pool, fetcher)

        assert _art_tags_of(api_resource, scryfall_id) == {}
