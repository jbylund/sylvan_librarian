"""Tests for the shared query sampler.

The load runner, the survey and the whole cost-model bench stack all draw from this module, so the
contract that matters is that everything it can emit actually parses — a family that produces an
unparseable predicate silently drops those samples from whichever benchmark drew it.
"""

from __future__ import annotations

import json
import random
from typing import TYPE_CHECKING

import pytest

from api.enums import CardOrdering
from api.parsing import parse_scryfall_query
from api.parsing.card_query_nodes import get_keywords_comparison_object
from api.parsing.rewrite import _DERIVED_EXPANSIONS
from client.query_sampler import (
    ENGINE_ORDERBYS,
    FALLBACK_VOCAB,
    MIN_WORD_ROWS,
    MODES,
    NUMERIC_COLUMNS,
    REALISTIC_FAMILY_WEIGHTS,
    REALISTIC_ORDERBY_WEIGHTS,
    REALISTIC_UNIQUE_WEIGHTS,
    STATIC_VALUES,
    STRUCTURES,
    QuerySampler,
    Shape,
)

if TYPE_CHECKING:
    import pathlib

# Enough draws that a 1-in-N branch inside a family (bounded ranges, name prefixing, the year/date
# split) is hit many times over, without making the suite slow.
DRAWS_PER_FAMILY = 200
DRAWS_PER_QUERY = 2000
# Shape coverage needs more draws than family coverage: the rarest shape is ~1% of realistic weight.
DRAWS_FOR_SHAPE_COVERAGE = 20000


@pytest.fixture(params=MODES)
def sampler(request: pytest.FixtureRequest) -> QuerySampler:
    """A no-corpus sampler in each mode. Corpus-backed sampling is covered by TestCorpus."""
    return QuerySampler(mode=request.param)


@pytest.fixture
def corpus(tmp_path: pathlib.Path) -> pathlib.Path:
    """A miniature corpus exercising every column the sampler reads, including the awkward values.

    Real exports carry apostrophes and spaces in names, types and artists, and those are exactly the
    values that do not survive the lexer unquoted.
    """
    rows = [
        {
            "card_name": f"Ali's Bolt {i}",
            "card_artist": f"Jane O'Connor {i % 3}",
            "card_set_code": f"s{i % 4}",
            "card_types": ["Creature", "Legendary"],
            "card_subtypes": ["First Strike Warrior"],
            "card_colors": {"B": True, "G": True} if i % 2 else {"U": True},
            "card_color_identity": {"B": True},
            "produced_mana": {"G": True, "C": True},
            "card_frame_data": {"2015": True},
            "card_border": "black",
            "card_watermark": "set" if i % 5 else None,
            "card_rarity_int": i % 4,
            "card_legalities": {"modern": "legal", "standard": "not_legal"},
            "oracle_text": "Destroy target creature. Draw a card.",
            "flavor_text": "Ancient darkness stirs beneath.",
            "price_usd": 0.1 * i,
            "price_eur": 0.2 * i,
            "price_tix": 0.05 * i,
            "collector_number_int": i,
            "released_at": f"20{10 + i % 15}-03-0{1 + i % 8}",
            "creature_power": i % 8,
            "creature_toughness": i % 9,
            "cmc": i % 7,
            "planeswalker_loyalty": i % 5,
        }
        for i in range(MIN_WORD_ROWS * 2)
    ]
    path = tmp_path / "corpus.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return path


class TestPredicates:
    @pytest.mark.parametrize(argnames=["family"], argvalues=[(f,) for f in sorted(REALISTIC_FAMILY_WEIGHTS)])
    def test_every_family_parses(self, sampler: QuerySampler, family: str) -> None:
        rng = random.Random(0)
        for _ in range(DRAWS_PER_FAMILY):
            predicate = sampler.predicate(family, rng)
            parse_scryfall_query(predicate)

    def test_numeric_families_reach_both_ends(self, sampler: QuerySampler) -> None:
        """Quantile sampling must span the column, not cluster at one point."""
        rng = random.Random(0)
        drawn = {sampler.predicate("cmc", rng) for _ in range(DRAWS_PER_FAMILY)}
        assert len(drawn) > 10, f"cmc collapsed to {drawn}"

    def test_bounded_ranges_are_ordered(self, sampler: QuerySampler) -> None:
        rng = random.Random(0)
        for _ in range(DRAWS_PER_FAMILY):
            predicate = sampler.predicate("usd", rng)
            if ">=" not in predicate or "<=" not in predicate:
                continue
            low, high = (float(part.split("=")[1]) for part in predicate.split())
            assert low <= high, predicate

    def test_values_needing_quotes_are_quoted(self) -> None:
        assert QuerySampler._quote("goblin") == "goblin"
        assert QuerySampler._quote("o'connor") == '"o\'connor"'
        assert QuerySampler._quote("first strike") == '"first strike"'


class TestQueries:
    def test_flat_queries_parse(self, sampler: QuerySampler) -> None:
        rng = random.Random(0)
        for _ in range(DRAWS_PER_QUERY):
            parse_scryfall_query(sampler.query(rng))

    def test_shaped_queries_parse(self, sampler: QuerySampler) -> None:
        rng = random.Random(0)
        for _ in range(DRAWS_PER_QUERY):
            parse_scryfall_query(sampler.structured_query(rng)["query"])

    def test_flat_queries_are_conjunctions(self, sampler: QuerySampler) -> None:
        """`query` is what the cost-model baselines assume: no connectives, no parens."""
        rng = random.Random(0)
        for _ in range(DRAWS_PER_QUERY):
            query = sampler.query(rng)
            assert " or " not in query, query
            assert "(" not in query, query
            assert not query.startswith("-"), query

    def test_seed_is_reproducible(self, sampler: QuerySampler) -> None:
        first = [sampler.query(random.Random(11)) for _ in range(5)]
        second = [sampler.query(random.Random(11)) for _ in range(5)]
        assert first == second

    def test_every_family_is_reachable(self, sampler: QuerySampler) -> None:
        rng = random.Random(0)
        seen: set[str] = set()
        for _ in range(DRAWS_FOR_SHAPE_COVERAGE):
            seen.update(sampler.structured_query(rng)["families"].split("+"))
        assert not set(REALISTIC_FAMILY_WEIGHTS) - seen

    def test_every_shape_is_reachable(self, sampler: QuerySampler) -> None:
        rng = random.Random(0)
        seen = {sampler.structured_query(rng)["structure"] for _ in range(DRAWS_FOR_SHAPE_COVERAGE)}
        assert not set(STRUCTURES) - seen


class TestExclude:
    def test_excluded_family_never_appears(self) -> None:
        """The survey's exclusion, written as `Shape`'s positive complement."""
        sampler = QuerySampler(mode="uniform")
        shape = Shape(families=frozenset(REALISTIC_FAMILY_WEIGHTS) - {"tix", "eur"})
        rng = random.Random(0)
        for _ in range(DRAWS_PER_QUERY):
            query = sampler.structured_query(rng, shape)["query"]
            assert "tix" not in query, query
            assert "eur" not in query, query

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode must be one of"):
            QuerySampler(mode="nonesuch")


class TestParams:
    def test_params_are_engine_values(self, sampler: QuerySampler) -> None:
        rng = random.Random(0)
        for _ in range(DRAWS_PER_FAMILY):
            params = sampler.params(rng)
            assert params["unique"] in REALISTIC_UNIQUE_WEIGHTS
            assert params["orderby"] in ENGINE_ORDERBYS
            assert isinstance(params["offset"], int)

    def test_orderbys_match_the_api_enum(self) -> None:
        """The sampler cannot import api.enums (it ships in the client image), so assert here."""
        assert {str(o) for o in CardOrdering} == ENGINE_ORDERBYS

    def test_sampled_orderbys_are_engine_orderbys(self) -> None:
        assert set(REALISTIC_ORDERBY_WEIGHTS) == set(ENGINE_ORDERBYS)

    def test_every_keyword_survives_the_storage_round_trip(self) -> None:
        """Whatever Scryfall's casing, and whatever the user types, the lookup finds the stored key.

        Both sides lowercase (`api/card_processing.py` at ingest, `get_keywords_comparison_object`
        at query time), so the sampler can draw from the whole vocabulary rather than the subset
        Scryfall happens to spell in Title Case — which is what #825 was.
        """
        for keyword in ("Flying", "Brood Telepathy", "First strike", "Doctor's companion"):
            stored = {keyword.lower(): True}
            for typed in (keyword, keyword.lower(), keyword.upper()):
                assert get_keywords_comparison_object(typed).keys() <= stored.keys(), typed

    def test_fallback_keywords_are_in_stored_form(self) -> None:
        """Vocabulary values are emitted verbatim, so each must already be what storage holds."""
        for keyword in FALLBACK_VOCAB["keyword"]:
            assert get_keywords_comparison_object(keyword) == {keyword: True}, keyword

    def test_is_tags_match_the_rewrite_table(self) -> None:
        """An `is:` value the rewrite layer does not expand hits an empty column and matches nothing.

        Same reason as the orderby list: the sampler cannot import `api/`, so the agreement is
        asserted here rather than derived.

        The three `rewrite.ENGINE_IS_VALUES` are the exception and are deliberately OUTSIDE this
        set: they expand to nothing and are answered by the engine off a field, so the equality
        below is with the expansion table alone.
        """
        expandable = {f"is:{value}" for alias, value in _DERIVED_EXPANSIONS if alias == "is"}
        assert set(STATIC_VALUES["tag"]) == expandable


class TestModes:
    def test_modes_share_one_universe(self) -> None:
        """Realistic and uniform differ only in weights, so neither can reach a value the other cannot."""
        realistic, uniform = (QuerySampler(mode=m) for m in ("realistic", "uniform"))
        assert set(realistic.vocab) == set(uniform.vocab)
        for family in realistic.vocab:
            assert set(realistic.vocab[family][0]) == set(uniform.vocab[family][0])

    def test_uniform_weights_are_flat(self) -> None:
        sampler = QuerySampler(mode="uniform")
        assert set(sampler.families[1]) == {1.0}

    def test_fallback_covers_every_numeric_column(self) -> None:
        sampler = QuerySampler()
        assert set(sampler.sorted) == set(NUMERIC_COLUMNS.values())

    def test_fallback_quantiles_are_monotonic(self) -> None:
        sampler = QuerySampler()
        for column in NUMERIC_COLUMNS.values():
            values = [sampler.quantile(column, p / 100) for p in range(101)]
            assert values == sorted(values), column


class TestCorpus:
    @pytest.mark.parametrize(argnames=["mode"], argvalues=[(m,) for m in MODES])
    def test_corpus_queries_parse(self, corpus: pathlib.Path, mode: str) -> None:
        """Corpus values carry punctuation the fallback vocabulary does not; they must still parse."""
        sampler = QuerySampler(corpus, mode)
        rng = random.Random(0)
        for _ in range(DRAWS_PER_QUERY):
            parse_scryfall_query(sampler.structured_query(rng)["query"])

    def test_values_come_from_the_corpus(self, corpus: pathlib.Path) -> None:
        sampler = QuerySampler(corpus)
        assert set(sampler.vocab["set"][0]) == {"s0", "s1", "s2", "s3"}
        assert set(sampler.vocab["identity"][0]) == {"b"}
        assert set(sampler.vocab["legality"][0]) == {"modern"}, "not_legal formats must be dropped"

    def test_missing_numeric_column_drops_its_family(self, tmp_path: pathlib.Path) -> None:
        """A numeric column the corpus lacks is not offered, rather than sampled off built-in deciles.

        The deciles describe the full corpus; on a trimmed export they would place thresholds the
        data cannot honour. A field that is absent from the pool is visible; one sampled against the
        wrong distribution is not.
        """
        path = tmp_path / "thin.jsonl"
        path.write_text(json.dumps({"card_name": "Bolt", "cmc": 3}))
        sampler = QuerySampler(path)
        assert {"usd", "eur", "tix"} <= sampler.missing_numeric
        assert "cmc" not in sampler.missing_numeric
        assert "usd" not in sampler.families[0]
        assert "cmc" in sampler.families[0]

    def test_missing_vocabulary_keeps_its_fallback(self, tmp_path: pathlib.Path) -> None:
        """Closed vocabularies DO fall back: a curated value set is valid whatever the corpus holds."""
        path = tmp_path / "thin.jsonl"
        path.write_text(json.dumps({"card_name": "Bolt", "cmc": 3}))
        sampler = QuerySampler(path)
        assert set(sampler.vocab["watermark"][0]) == set(FALLBACK_VOCAB["watermark"])

    def test_realistic_weights_track_corpus_frequency(self, corpus: pathlib.Path) -> None:
        values, weights = QuerySampler(corpus, "realistic").vocab["type"]
        by_value = dict(zip(values, weights, strict=True))
        assert by_value["creature"] > 0
        assert by_value["creature"] == by_value["legendary"], "both appear on every row"
