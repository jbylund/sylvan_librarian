"""Process-level registry of set release groups, for the `g:` / `group:` keyword.

Scryfall's `g:<set>` answers a set's RELEASE GROUP. Which sets that is turns out to be a function
of one field of the Set object, `parent_set_code`, and of nothing a card carries. Measured on
api.scryfall.com 2026-10-08 over 58 sets in 19 families -- every structural shape that day's `/sets`
holds -- each proved by `g:<code>` and its `e:` list having one `total_cards` and
`g:<code> -(<the list>)` being a 404:

    g:<code>  =  the set, its CHILDREN, its PARENT, and its parent's other children

One step each way, not the family. A grandchild is not a child and a grandparent is not a parent,
so the group depends on which member is named:

    g:ecl   ecl and its five children             tecc is not in it (a grandchild)
    g:tecl  the same six                          a sibling's child is no sibling
    g:ecc   all seven -- ecc, its child tecc, its parent ecl, ecl's other four children
    g:tecc  tecc and ecc                          the grandparent ecl is not in it
    g:pbig  pbig, big, tbig                       not otj, and not its cousin totc
    g:lea   lea alone                             no parent, no child

The parser has no database access, so the lookup lives here as plain dicts the app fills from
`magic.sets` (the mirrored `/sets` list) and the post-parse rewrite
(`rewrite.expand_release_groups`) reads. Membership is the catalog's and never the card table's: a
set this server imported no card of is still a member, and simply contributes no rows.

A value names a set by its code, or by its whole name with case and the separators Scryfall drops
(spaces, `'`, `.`, `-`, `_`) ignored: `g:"Lorwyn Eclipsed Commander"` is `g:ecc`. A name is only
read when it is no set's code and names exactly one set.

Refresh cadence: `AppContext.ensure_set_groups` reloads once per `last_import_time` per worker
process, and the import path reloads explicitly once it has refreshed `magic.sets`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# One row per set: its code, its parent's code (NULL for most) and its name.
SET_GROUPS_SQL = "SELECT code, set_object->>'parent_set_code' AS parent_set_code, set_object->>'name' AS name FROM magic.sets"

# What Scryfall drops from a set name written as a value.
_NAME_SEPARATORS_RE = re.compile(r"[\s'._-]")
_NAME_KEY_RE = re.compile(r"[a-z0-9]+")

# Lower-cased set code -> the OTHER sets of its group, sorted; and folded set name -> code. Both are
# replaced wholesale, never mutated in place, so a reader on another thread sees either the old
# mappings or the new ones and never a half-built dict.
_GROUP_OTHERS: dict[str, tuple[str, ...]] = {}
_NAME_TO_CODE: dict[str, str] = {}


def _name_key(name: str) -> str | None:
    """Return *name* as it is compared, or None when it holds a character no set name is read with."""
    key = _NAME_SEPARATORS_RE.sub("", name.lower())
    return key if _NAME_KEY_RE.fullmatch(key) else None


def replace_set_groups(sets: Iterable[Mapping[str, str | None]]) -> None:
    """Rebuild the registry from *sets*: mappings with `code`, `parent_set_code` and `name`."""
    global _GROUP_OTHERS, _NAME_TO_CODE  # noqa: PLW0603
    parents: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    names: dict[str, str | None] = {}
    for entry in sets:
        code = (entry.get("code") or "").lower()
        if not code:
            continue
        parent = (entry.get("parent_set_code") or "").lower()
        parents[code] = parent
        if parent:
            children.setdefault(parent, []).append(code)
        key = _name_key(entry.get("name") or "")
        if key is not None:
            # A name two sets share names neither.
            names[key] = None if key in names else code

    others: dict[str, tuple[str, ...]] = {}
    for code, parent in parents.items():
        group = set(children.get(code, ()))
        if parent:
            group.add(parent)
            group.update(children.get(parent, ()))
        group.discard(code)
        others[code] = tuple(sorted(group))

    _GROUP_OTHERS = others
    _NAME_TO_CODE = {key: code for key, code in names.items() if code is not None and key not in parents}


def release_group(value: str) -> tuple[str, tuple[str, ...]] | None:
    """Return `(code, other member codes)` for the set *value* names, or None if no listed set does.

    The others are the set's children, its parent and its parent's other children, sorted; empty
    for a set with neither a parent nor a child.
    """
    code = value.lower()
    others = _GROUP_OTHERS.get(code)
    if others is None:
        key = _name_key(value)
        code = _NAME_TO_CODE.get(key, "") if key is not None else ""
        others = _GROUP_OTHERS.get(code)
        if others is None:
            return None
    return code, others
