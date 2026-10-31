"""Process-level registry of set code -> release date, for `date:` values that name a set.

Scryfall accepts a set code wherever `date:` takes a date and compares against that set's
release date as a full day, not a window: `date>=hob` is `date>=2026-08-14`. The parser has no
database access, so the lookup lives here as a plain dict the app fills from `magic.cards` and
the post-parse rewrite (`rewrite.resolve_set_code_dates`) reads. Both search paths then see the
same literal date in the AST -- `to_sql` and the Rust engine's `to_json` alike.

What fills it: `SET_RELEASE_DATES_SQL`, the earliest `released_at` among a set's printings. This
server has no sets table on main (PR #922 adds `magic.sets`; once it lands this can read the set
object's own `released_at`, which is what Scryfall resolves against). The two agree for every set
whose cards share a release day; a set with staggered printings (`sld`, `plst`) resolves to its
first printing here. Only primary codes appear in `card_set_code`, which matches Scryfall: `e:dar`
finds Dominaria (265 cards, measured 2026-09-03) but `date>=dar` is `Invalid date or unknown set
code` there, because `dar` is the arena code and not the set's `code`.

Refresh cadence: `AppContext.ensure_set_release_dates` reloads once per `last_import_time` per
worker process, and the import path reloads explicitly when it finishes. A code from a set that
was imported after the last refresh resolves on the next one, not before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# Earliest printing per set code. `::text` so the value is already the ISO `YYYY-MM-DD` string the
# AST carries, with no date object to format on the way.
SET_RELEASE_DATES_SQL = (
    "SELECT card_set_code AS code, MIN(released_at)::text AS released_at "
    "FROM magic.cards WHERE card_set_code IS NOT NULL GROUP BY card_set_code"
)

# Lower-cased set code -> ISO date. Replaced wholesale, never mutated in place, so a reader on
# another thread sees either the old mapping or the new one and never a half-built dict.
_SET_RELEASE_DATES: dict[str, str] = {}


def replace_set_release_dates(mapping: Mapping[str, str]) -> None:
    """Swap the registry for *mapping* (codes are lower-cased on the way in)."""
    global _SET_RELEASE_DATES  # noqa: PLW0603
    _SET_RELEASE_DATES = {code.lower(): released_at for code, released_at in mapping.items()}


def set_release_date(code: str) -> str | None:
    """Return the ISO release date for *code* (any case), or None if no imported set has it."""
    return _SET_RELEASE_DATES.get(code.lower())
