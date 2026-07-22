# Metacharacter-free regex lowered to substring search (#734)

An unanchored regex with no metacharacters — `o:/sacrifice a/` — is exactly a substring search, and
now parses to the same AST as `o:"sacrifice a"`. The rewrite is a **Python post-parse pass**
(`lower_literal_regexes`, `api/parsing/rewrite.py`), not an engine change, so both consumers of the
AST benefit: the SQL path gains `gin_trgm_ops`, and the Rust engine gains trigram / oracle-word
narrowing — where an arbitrary regex has no index path and scans every card.

The win is the **access path**, not the per-row constant. A regex leaf has no narrowing arm, so it
scans the whole corpus; the lowered substring narrows to a trigram-candidate superset first. For
`o:/sacrifice a/` (1,391 matches) that superset is 2,425 cards — 7.7% of the corpus:

| `o:/sacrifice a/` / card | match phase |
|---|---:|
| before (full regex scan) | ~1100 μs |
| after (trigram-narrowed substring) | ~35 μs |

End-to-end it drops to the substring form's **117 μs** (byte-identical results). Anchored (`^`/`$`),
metacharacter, and character-class patterns stay a real regex. Both multiples are query-dependent
(rarest-trigram selectivity; needle).

Two supporting changes:

- **memmem for the residual `contains` scans.** The once-per-query scans that build
  `OracleMatch`/`NameMatch`/`ArtistMatch`/`FlavorMatch` from a substring needle now reuse a single
  `memchr::memmem::Finder` instead of rebuilding `str::contains`'s searcher per candidate — 1.26×
  (10.5 vs 13 ns/candidate).
- **One rewrite pipeline.** All post-parse AST rewrites (`expand_derived_predicates`,
  `lower_literal_regexes`) now compose through a single `rewrite_query` both parsers call, so future
  passes land in one place with guaranteed cross-parser parity.

1,537 parsing / 2,121 unit tests pass; 127 engine tests pass (release + debug).
