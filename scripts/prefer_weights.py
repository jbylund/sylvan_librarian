#!/usr/bin/env python3
"""Score printings from raw component levels, so weights become data.

Today `api/sql/backfill_prefer_scores.sql` fuses feature extraction with weight
application -- each CASE emits an already-weighted number (`WHEN frame ? '2015' THEN
42`), so the weights cannot be recovered from the output and changing one costs a
full-table UPDATE plus an engine reload. That makes any kind of coefficient search
impossible.

This module extracts the *level* of each component per printing once, then applies a
weight table in Python. Re-scoring a candidate weighting is then a dot product over
rows already in memory: milliseconds, not minutes.

`validate` checks the separation is faithful by reproducing the stored prefer_score
from levels x weights. If that does not match, no diff built on it can be trusted.

Usage:
    python scripts/prefer_weights.py validate
    python scripts/prefer_weights.py swaps --set art_style=6 --scale extended_art=0.9
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import subprocess
import sys
import tempfile
from pathlib import Path

DB_CONTAINER, DB_USER, DB_NAME = "sylvan_blue-postgres-1", "foouser", "magic"

# The weights currently baked into backfill_prefer_scores.sql, lifted out verbatim.
# `art_style` is new and defaults to 0 so the baseline is bit-identical to today.
WEIGHTS: dict[str, float] = {
    "language_en": 40.0,
    "frame_2015": 42.0,
    "frame_2003": 30.0,
    "frame_1997": 25.0,
    "frame_1993": 10.0,
    "artwork_set_ok": 20.0,  # not the dbl (black-and-white) set
    "rarity_common": 16.0,
    "rarity_uncommon": 16.0,
    "rarity_rare": 11.0,
    "rarity_mythic": 0.0,
    "highres_scan": 16.0,
    "border_black": 14.0,
    "extended_art": 12.0,
    "finish_nonfoil": 10.0,
    "finish_foil": 5.0,
    "non_showcase": 10.0,
    "has_paper": 6.0,
    "legendary_frame": 5.0,
    "illustration_count": 23.0,  # coefficient of ln(1+n)/ln(40)
    # NEW: on-style bonus. Awarded when the artwork carries none of the off-style art
    # tags. Measured 153/153 -- in every labelled comparison where exactly one artwork
    # was off-style tagged, the untagged one was chosen. Expressed as a bonus for being
    # on-style rather than a penalty so every component stays non-negative, matching the
    # rest of the table.
    "art_style": 0.0,
}

# Off-style art tags. Community-curated descriptions OF THE ARTWORK, which is why these
# are used instead of a year/era term: a term on year permanently penalises all future
# art, whereas a tag generalises -- new anime-styled art gets tagged, new core-style art
# does not. This list is a hypothesis; 533 further candidates exist (art tags whose
# artworks concentrate in few sets) and are unscanned.
# On-style is the DEFAULT; a printing loses the bonus by carrying external-ip (the
# Scryfall tagger's parent for ~57 licensed franchises) or a non-IP stylistic departure.
#
# D&D and Lord of the Rings are exempt: external IP whose art matches Magic's high-fantasy
# look. Verified that exempting only these two parents is complete -- zero artworks carry a
# sibling tag (arda, hobbit, abeir-toril, dnd-multiverse) without also carrying its parent,
# so no Middle-earth or Forgotten Realms art slips through.
STYLE_EXEMPT_TAGS = ("dungeons-and-dragons", "the-lord-of-the-rings")
# Non-IP stylistic departures: not external universes, but not the core look either.
STYLE_DEPARTURE_TAGS = ("anime", "comic-style", "line-art", "word-art-title")
# A partial-credit tier for romance-of-the-three-kingdoms (Portal Three Kingdoms: external
# IP, but a 1999 Magic set rather than a modern crossover) was built and measured, then
# dropped. Across the whole 0-to-full range it moved only 5 cards, and 3 at the candidate
# weight -- most P3K cards were never reprinted, so their art has nothing to compete with.
# Not worth a third tier for a distinction that changes nothing visible.

ILLUS_NORM = math.log(40)  # the ln(40) denominator in the current SQL

# prefer_score is stored as a postgres `real`; this covers f32 rounding at these magnitudes.
VALIDATE_TOLERANCE = 1e-2
# Two scores closer than this are the same number in f32 -- the swap is a tie-break, not an
# override, and the review page reports the two differently.
SCORE_EPSILON = 1e-9
# A card needs at least this many printings to have a choice worth reviewing.
MIN_PRINTINGS = 2
# Upper bound on the binary search for a step size, in weight points. Every component sits
# well below this, so hitting it means the component cannot reach the target swap count.
STEP_SEARCH_CEILING = 512
# Fair coin for the left/right assignment in blind review.
COIN = 0.5
# Generated review pages go to the system temp dir rather than a hardcoded /tmp.
OUT_DIR = Path(tempfile.gettempdir())

# Rows the printing count should ignore, as a candidate fix to `illustration_count`.
#
# The component is meant to measure how heavily an ARTWORK has been reprinted, but its
# numerator counts every row sharing the illustration, including rows that are not real
# printing events:
#
#   memorabilia  World Championship decks, Collectors' Edition, International Collectors'
#                Edition and 30th Anniversary -- none tournament-legal, none a real set.
#                Four of Birds of Paradise's eight are BLACK-bordered (30a x2, ced, cei),
#                so a border test alone misses half of them.
#   gold/yellow  any remaining non-standard product Scryfall types as something other
#                than memorabilia. Belt-and-braces against the above.
#   non-English  the same printing event already counted in its English row (4bb, fbb).
#
# Not filtered: white borders. 2ed-6ed are white-bordered and perfectly real, and
# dropping them would penalise exactly the core-set reprints the component should reward.
COUNTED_PRINTING_SQL = """
    COALESCE(o.raw_card_blob ->> 'lang', '') = 'en'
    AND COALESCE(o.raw_card_blob ->> 'set_type', '') <> 'memorabilia'
    AND COALESCE(o.card_border, '') NOT IN ('gold', 'yellow')
"""

# Swap review shows the WHOLE CARD, not Scryfall's art_crop. The decision under review
# is "which printing does the site display", so the reviewer must see what the site
# would show. Art crops actively mislead here: extended_art, border and frame are frame
# properties a crop cannot show, so an extended-art printing and its normal sibling
# appear as duplicate images and every verdict comes back "no difference".
#
# Sourced from Scryfall's `normal` rather than the project CDN: the CDN 403s on
# printings the site does not carry (e.g. pw26/20), which would leave the reviewer
# comparing a broken image against a card. Scryfall covers 94,718/94,718.


def front_image(size: str, alias: str = "") -> str:
    """Build the SQL for a printing's front-face image URL at one size.

    `raw_card_blob` holds Scryfall's own card object, and where a card carries its images decides
    on layout: a single-face or split printing has `image_uris` at top level, a transform or MDFC
    one has them only under `card_faces`. Reading the front therefore has to try both, which is
    what every image read in this file goes through.

    Args:
        size: An `image_uris` key -- "normal", "art_crop", and so on.
        alias: Table alias to qualify the column with, or "" for an unqualified column.

    Returns:
        A SQL expression yielding the URL, or NULL when the printing has no image of that size.
    """
    column = f"{alias}.raw_card_blob" if alias else "raw_card_blob"
    return f"COALESCE({column} -> 'image_uris' ->> '{size}', {column} -> 'card_faces' -> 0 -> 'image_uris' ->> '{size}')"


LEVELS_EXTRA_IMAGE = front_image("normal")

LEVELS_SQL = """
SELECT c.scryfall_id::text AS sid,
       c.card_name,
       c.prefer_score,
       (SELECT count(*) FROM magic.cards o
         WHERE o.illustration_id = c.illustration_id AND o.illustration_id IS NOT NULL
           AND o.card_name = c.card_name)                              AS illus_n,
       (SELECT count(*) FROM magic.cards o
         WHERE o.illustration_id = c.illustration_id AND o.illustration_id IS NOT NULL
           AND o.card_name = c.card_name AND ({counted}))              AS illus_n_real,
       (SELECT count(DISTINCT o.card_set_code) FROM magic.cards o
         WHERE o.illustration_id = c.illustration_id AND o.illustration_id IS NOT NULL
           AND o.card_name = c.card_name AND ({counted}))              AS illus_n_set,
       COALESCE(c.raw_card_blob ->> 'set_type', '')                   AS set_type,
       c.card_rarity_int                                              AS rarity,
       COALESCE(c.card_border, '')                                    AS border,
       CASE WHEN c.card_frame_data ? '2015' THEN '2015'
            WHEN c.card_frame_data ? '2003' THEN '2003'
            WHEN c.card_frame_data ? '1997' THEN '1997'
            WHEN c.card_frame_data ? '1993' THEN '1993' ELSE '' END    AS frame,
       COALESCE(c.card_frame_data ? 'Extendedart', false)             AS extended,
       ((c.raw_card_blob ->> 'image_status') = 'highres_scan')          AS highres,
       COALESCE(c.raw_card_blob -> 'games' ? 'paper', false)           AS paper,
       ((c.raw_card_blob ->> 'lang') = 'en')                           AS en,
       COALESCE(c.raw_card_blob -> 'frame_effects' ? 'legendary', false) AS legendary,
       COALESCE(c.raw_card_blob -> 'frame_effects' ? 'showcase', false)  AS showcase,
       CASE WHEN c.raw_card_blob -> 'finishes' ? 'nonfoil' THEN 'nonfoil'
            WHEN c.raw_card_blob -> 'finishes' ? 'foil' THEN 'foil'
            ELSE 'other' END                                          AS finish,
       (c.card_set_code <> 'dbl')                                     AS artwork_set_ok,
       ((c.card_art_tags ? 'external-ip' AND NOT (c.card_art_tags ?| {exempt}))
         OR c.card_art_tags ?| {departure})                           AS off_style,
       c.card_set_code, c.collector_number,
       {art_crop_url}                                                 AS art_url,
       {normal_url}                                                   AS card_url
FROM magic.cards c
WHERE {where}
"""


def run_query(sql: str) -> list[dict[str, str]]:
    """Run SQL in the blue postgres container and return CSV rows as dicts."""
    proc = subprocess.run(
        ["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-v", "ON_ERROR_STOP=1", "--csv", "-f", "-"],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.exit(f"psql failed:\n{proc.stderr}")
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def t(v: str) -> bool:
    """Postgres CSV renders booleans as 't'/'f'."""
    return v == "t"


def score(row: dict[str, str], w: dict[str, float], illus_key: str = "illus_n") -> float:
    """Sum of weighted component levels, mirroring backfill_prefer_scores.sql.

    `illus_key` selects which printing count feeds `illustration_count`: "illus_n" is
    what the SQL does today, "illus_n_real" applies COUNTED_PRINTING_SQL. Everything
    else is identical, so a diff between the two isolates the counting rule.
    """
    s = 0.0
    n = float(row[illus_key] or 0)
    s += round(w["illustration_count"] * math.log1p(n) / ILLUS_NORM, 4)
    rar = row["rarity"]
    s += {"0": w["rarity_common"], "1": w["rarity_uncommon"], "2": w["rarity_rare"], "3": w["rarity_mythic"]}.get(rar, 0.0)
    s += w["border_black"] if row["border"] == "black" else 0.0
    s += w.get(f"frame_{row['frame']}", 0.0) if row["frame"] else 0.0
    s += w["extended_art"] if t(row["extended"]) else 0.0
    s += w["highres_scan"] if t(row["highres"]) else 0.0
    s += w["has_paper"] if t(row["paper"]) else 0.0
    s += w["language_en"] if t(row["en"]) else 0.0
    s += w["legendary_frame"] if t(row["legendary"]) else 0.0
    s += 0.0 if t(row["showcase"]) else w["non_showcase"]
    s += {"nonfoil": w["finish_nonfoil"], "foil": w["finish_foil"]}.get(row["finish"], 0.0)
    s += w["artwork_set_ok"] if t(row["artwork_set_ok"]) else 0.0
    s += 0.0 if t(row["off_style"]) else w["art_style"]
    return s


# Target swap count for a proposed step. Small on purpose: the point is a change you
# can eyeball in seconds and judge as a net-positive or net-negative trade. A fixed
# percentage step does not do this -- extended_art at -10% moves 6 cards while
# art_style at +6 moves 53, so the same percentage is unreviewable in one case and
# uninformative in the other.
TARGET_SWAPS = (6, 9)


def level(row: dict[str, str], comp: str) -> float:
    """Multiplier that `comp`'s weight is applied to for this printing.

    Lets a single-component step be evaluated as base_score + delta * level, which is
    one multiply-add per row instead of rescoring every component.
    """
    if comp == "illustration_count":
        return round(math.log1p(float(row["illus_n"] or 0)) / ILLUS_NORM, 6)
    if comp.startswith("rarity_"):
        return (
            1.0
            if row["rarity"] == {"rarity_common": "0", "rarity_uncommon": "1", "rarity_rare": "2", "rarity_mythic": "3"}[comp]
            else 0.0
        )
    if comp.startswith("frame_"):
        return 1.0 if row["frame"] == comp.removeprefix("frame_") else 0.0
    if comp.startswith("finish_"):
        return 1.0 if row["finish"] == comp.removeprefix("finish_") else 0.0
    flags = {
        "border_black": row["border"] == "black",
        "extended_art": t(row["extended"]),
        "highres_scan": t(row["highres"]),
        "has_paper": t(row["paper"]),
        "language_en": t(row["en"]),
        "legendary_frame": t(row["legendary"]),
        "non_showcase": not t(row["showcase"]),
        "artwork_set_ok": t(row["artwork_set_ok"]),
        "art_style": not t(row["off_style"]),
    }
    return 1.0 if flags[comp] else 0.0


def side(r: dict[str, str]) -> dict[str, str]:
    """One side of a swap: the full card image plus its set/collector for provenance."""
    loc = f"{r['card_set_code']}/{r['collector_number']}"
    return {"img": r["card_url"] or r["art_url"] or "", "art": r["art_url"] or "", "loc": loc}


def pg_arr(tags: tuple[str, ...]) -> str:
    """Render a tuple as a postgres ARRAY[...] literal."""
    return "ARRAY[" + ", ".join("'" + t + "'" for t in tags) + "]"


def load(where: str) -> list[dict[str, str]]:
    """Fetch component levels for every printing matching `where`."""
    return run_query(
        LEVELS_SQL.format(
            exempt=pg_arr(STYLE_EXEMPT_TAGS),
            departure=pg_arr(STYLE_DEPARTURE_TAGS),
            counted=COUNTED_PRINTING_SQL,
            where=where,
            art_crop_url=front_image("art_crop", "c"),
            normal_url=front_image("normal", "c"),
        )
    )


def cmd_validate() -> None:
    """Reproduce the stored prefer_score from levels x weights."""
    rows = load("c.raw_card_blob ->> 'lang' = 'en' AND c.prefer_score IS NOT NULL")
    worst = 0.0
    bad = 0
    for r in rows:
        got, want = score(r, WEIGHTS), float(r["prefer_score"])
        d = abs(got - want)
        worst = max(worst, d)
        # prefer_score is a postgres `real`; ~1e-3 covers f32 rounding at these magnitudes
        bad += d > VALIDATE_TOLERANCE
    print(f"{len(rows)} printings checked against the stored prefer_score")
    print(f"  max absolute difference : {worst:.6f}")
    print(f"  rows differing > 0.01   : {bad}")
    print(
        "\n  "
        + (
            "PASS - extraction is faithful, so weight diffs can be trusted."
            if bad == 0
            else "FAIL - the level extraction does not match the SQL; do not trust diffs."
        )
    )


def apply_overrides(sets: list[str], scales: list[str]) -> dict[str, float]:
    """Build a weight table from WEIGHTS plus `--set COMP=VALUE` / `--scale COMP=FACTOR`."""
    w = dict(WEIGHTS)
    for spec in sets:
        k, v = spec.split("=", 1)
        if k not in w:
            sys.exit(f"unknown component {k!r}")
        w[k] = float(v)
    for spec in scales:
        k, v = spec.split("=", 1)
        if k not in w:
            sys.exit(f"unknown component {k!r}")
        w[k] *= float(v)
    return w


def cmd_swaps(sets: list[str], scales: list[str], out: Path, limit: int) -> None:
    """Show which card the site would display differently under a proposed weighting."""
    proposed = apply_overrides(sets, scales)
    changed = [(k, WEIGHTS[k], proposed[k]) for k in WEIGHTS if WEIGHTS[k] != proposed[k]]
    if not changed:
        sys.exit("no weight actually changed")
    print("proposed change:")
    for k, a, b in changed:
        print(f"  {k:20s} {a:7.2f} -> {b:7.2f}")

    rows = load(f"c.raw_card_blob ->> 'lang' = 'en' AND c.prefer_score IS NOT NULL AND {front_image('art_crop', 'c')} IS NOT NULL")
    by_card: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_card.setdefault(r["card_name"], []).append(r)

    swaps = []
    multi = 0
    for name, printings in by_card.items():
        if len(printings) < MIN_PRINTINGS:
            continue
        multi += 1
        old = max(printings, key=lambda r: (score(r, WEIGHTS), r["sid"]))
        new = max(printings, key=lambda r: (score(r, proposed), r["sid"]))
        if old["sid"] != new["sid"]:
            swaps.append((name, old, new))
    print(f"\n  multi-printing cards : {multi}")
    print(f"  representative swaps : {len(swaps)}  ({100 * len(swaps) / multi:.2f}%)")

    swaps.sort(key=lambda s: s[0])
    payload = [{"card": n, "old": side(o), "new": side(w2)} for n, o, w2 in swaps[:limit]]
    out.write_text(
        REVIEW_PAGE.replace("__SWAPS__", json.dumps(payload, separators=(",", ":")))
        .replace("__DESC__", json.dumps(", ".join(f"{k} {a:g}->{b:g}" for k, a, b in changed)))
        .replace("__SLUG__", json.dumps("-".join(f"{k}{b:g}" for k, _, b in changed)[:60]))
    )
    print(f"  review page ({len(payload)} of {len(swaps)}) -> {out}")


REVIEW_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Swap review</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.4 system-ui, sans-serif; display:flex; flex-direction:column; min-height:100vh; }
  header { padding:.6rem 1rem; border-bottom:1px solid #8885; display:flex; gap:1rem; align-items:baseline; }
  #chg { font:12px monospace; opacity:.75; }
  #prog { margin-left:auto; font-variant-numeric:tabular-nums; opacity:.7; }
  main { flex:1; display:grid; grid-template-columns:1fr 1fr; gap:1rem; padding:1rem; align-content:start; }
  .side { display:grid; gap:.4rem; justify-items:center; }
  .side img { width:100%; max-width:400px; border-radius:10px; display:block; }
  .tag { font:12px monospace; opacity:.7; }
  button.act { font:inherit; padding:.5rem .9rem; border-radius:8px; cursor:pointer; background:none;
               border:1px solid #8888; }
  footer { display:flex; gap:.75rem; justify-content:center; padding:.75rem; border-top:1px solid #8885; }
  #done { display:none; padding:2rem; text-align:center; }
</style></head><body>
<header><strong id="card"></strong><span id="chg"></span><span id="prog"></span></header>
<main id="stage">
  <div class="side"><img id="leftImg"><span class="tag">LEFT &mdash; <span id="leftLoc"></span></span></div>
  <div class="side"><img id="rightImg"><span class="tag">RIGHT &mdash; <span id="rightLoc"></span></span></div>
</main>
<div id="done"><p><strong>All swaps reviewed.</strong> Download the verdicts below.</p></div>
<footer>
  <button class="act" id="pickLeft">Left is better (&larr;)</button>
  <button class="act" id="same">No real difference (space)</button>
  <button class="act" id="pickRight">Right is better (&rarr;)</button>
  <button class="act" id="undo">Undo (u)</button>
  <button class="act" id="toggle">Show art crop (a)</button>
  <button class="act" id="dl">Download verdicts</button>
</footer>
<script>
const SWAPS = __SWAPS__, DESC = __DESC__, SLUG = __SLUG__;
const KEY = 'prefer-swap-review-' + SLUG;
let st = JSON.parse(localStorage.getItem(KEY) || '{"v":{},"order":[]}');
if (!st.order) st = {v: st, order: Object.keys(st)};   // migrate older saved state
let v = st.v, order = st.order;
const save = () => localStorage.setItem(KEY, JSON.stringify({v, order}));
const $ = i => document.getElementById(i);
$('chg').textContent = DESC;
function todo() { return SWAPS.filter(s => !(s.card in v)); }
let showArt = false;
function render() {
  const q = todo();
  $('prog').textContent = `${SWAPS.length - q.length} / ${SWAPS.length}`;
  if (!q.length) { $('stage').style.display='none'; $('done').style.display='block'; $('card').textContent=''; return; }
  const s = q[0];
  $('card').textContent = s.card;
  // s.flip decides which physical side the PROPOSED printing occupies. Assigned per card
  // in Python, so the reviewer cannot learn "right is always the proposal" and score the
  // change rather than the card. rec() maps the side back to a verdict.
  const L = s.flip ? s.new : s.old, R = s.flip ? s.old : s.new;
  $('leftImg').src  = showArt && L.art ? L.art : L.img;
  $('rightImg').src = showArt && R.art ? R.art : R.img;
  $('leftLoc').textContent = L.loc; $('rightLoc').textContent = R.loc;
  const n = q[1]; if (n) { [n.old.img, n.new.img].forEach(u => { const i=new Image(); i.src=u; }); }
}
function rec(verdict) { const q = todo(); if (!q.length) return;
  v[q[0].card] = verdict; order.push(q[0].card); save(); render(); }
function pick(sideChosen) { const q = todo(); if (!q.length) return;
  const proposedIsLeft = !!q[0].flip;
  rec(sideChosen === (proposedIsLeft ? 'left' : 'right') ? 'better' : 'worse'); }
function undo() { const c = order.pop(); if (c === undefined) return; delete v[c]; save(); render(); }
$('undo').onclick = undo;
$('toggle').onclick = () => { showArt = !showArt;
  $('toggle').textContent = showArt ? 'Show whole card (a)' : 'Show art crop (a)'; render(); };
$('pickLeft').onclick  = () => pick('left');
$('pickRight').onclick = () => pick('right');
$('same').onclick      = () => rec('same');
$('dl').onclick = () => {
  const meta = Object.fromEntries(SWAPS.map(s => [s.card, {changes: s.changes || [DESC], kind: s.kind || null}]));
  // Destinations are recorded so a later batch can tell whether a previously-judged card is
  // being re-asked about the SAME printing (skip it) or a different one (must ask again).
  const locs = Object.fromEntries(SWAPS.map(s => [s.card, {from: s.old.loc, to: s.new.loc}]));
  const body = Object.entries(v).map(([c,d]) =>
      JSON.stringify({card:c, verdict:d, from:(locs[c]||{}).from, to:(locs[c]||{}).to,
                      changes:(meta[c]||{}).changes, kind:(meta[c]||{}).kind})).join('\\n')+'\\n';
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([body], {type:'application/x-ndjson'}));
  a.download = 'swap-verdicts-' + SLUG + '.jsonl'; a.click();
};
addEventListener('keydown', e => {
  if (e.key === 'ArrowRight') pick('right');
  else if (e.key === 'ArrowLeft') pick('left');
  else if (e.key === ' ') { e.preventDefault(); rec('same'); }
  else if (e.key === 'a') $('toggle').click();
  else if (e.key === 'u') undo();
});
render();
</script></body></html>
"""


def count_swaps(cards: list[list[tuple[float, float, str]]], delta: float) -> tuple[int, int]:
    """(tie_breaks, overrides) induced by shifting one component's weight by `delta`.

    The split matters. 46.9% of multi-printing cards have an exact top score, so today
    their representative is decided by store order, arbitrarily. Any epsilon on a
    component that differs between those tied printings flips all of them at once --
    which is why swap count jumps discontinuously from 0 and a small target band can be
    unreachable.

    Those are TIE BREAKS: the previous pick was arbitrary, so almost any principled
    change is an improvement and they need little scrutiny. An OVERRIDE reverses a
    strictly-ranked decision and is the thing actually worth eyeballing.
    """
    ties = over = 0
    for pr in cards:
        old = max(pr, key=lambda x: (x[0], x[2]))
        new = max(pr, key=lambda x: (x[0] + delta * x[1], x[2]))
        if old[2] == new[2]:
            continue
        if abs(old[0] - new[0]) < SCORE_EPSILON:
            ties += 1
        else:
            over += 1
    return ties, over


def cmd_step(comp: str, direction: str, out: Path) -> None:
    """Find the smallest change to one component that moves TARGET_SWAPS cards."""
    if comp not in WEIGHTS:
        sys.exit(f"unknown component {comp!r}; known: {', '.join(sorted(WEIGHTS))}")
    rows = load(f"c.raw_card_blob ->> 'lang' = 'en' AND c.prefer_score IS NOT NULL AND {front_image('art_crop', 'c')} IS NOT NULL")
    by: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by.setdefault(r["card_name"], []).append(r)
    multi = {k: v for k, v in by.items() if len(v) > 1}
    cards = [[(score(r, WEIGHTS), level(r, comp), r["sid"]) for r in v] for v in multi.values()]

    lo, hi = TARGET_SWAPS
    sign = 1.0 if direction == "up" else -1.0
    # Expand until the target band is reached or exceeded, then bisect for the
    # smallest step that lands in it. Swap count is a step function of delta, so an
    # exact hit is not guaranteed -- take the closest.
    step, best = 0.25, None
    while step <= STEP_SEARCH_CEILING:
        if count_swaps(cards, sign * step)[1] >= lo:
            break
        step *= 2
    a, b = 0.0, step
    for _ in range(40):
        mid = (a + b) / 2
        _, c = count_swaps(cards, sign * mid)
        if best is None or abs(c - (lo + hi) / 2) < abs(best[1] - (lo + hi) / 2):
            best = (mid, c)
        if c < lo:
            a = mid
        elif c > hi:
            b = mid
        else:
            best = (mid, c)
            break
    delta, got = best
    ties, _ = count_swaps(cards, sign * delta)
    new_w = WEIGHTS[comp] + sign * delta
    print(f"{comp}: {WEIGHTS[comp]:.4g} -> {new_w:.4g}   ({direction} {sign * delta:+.4g})")
    print(f"  overrides : {got}   (target {lo}-{hi}) -- reversals of a strict ranking, review these")
    print(f"  tie-breaks: {ties}        -- previously decided by store order, essentially free")

    w = dict(WEIGHTS)
    w[comp] = new_w
    swaps = []
    for name, v in multi.items():
        old = max(v, key=lambda r: (score(r, WEIGHTS), r["sid"]))
        new = max(v, key=lambda r: (score(r, w), r["sid"]))
        if old["sid"] != new["sid"]:
            swaps.append((name, old, new))
    payload = [{"card": n, "old": side(o), "new": side(x)} for n, o, x in sorted(swaps, key=lambda s: s[0])]
    out.write_text(
        REVIEW_PAGE.replace("__SWAPS__", json.dumps(payload, separators=(",", ":")))
        .replace("__DESC__", json.dumps(f"{comp} {WEIGHTS[comp]:g} -> {new_w:g}"))
        .replace("__SLUG__", json.dumps(f"{comp}{new_w:g}"))
    )
    print(f"  review page -> {out}")


def load_judged(paths: list[Path]) -> dict[str, set[str | None]]:
    """Cards already given a verdict in a previous review.

    A baseline shift alone does not stop a card being re-asked: if it swaps to the same
    printing at both the accepted weight and the proposed one, it belongs to the
    baseline's swap set and reappears identically. Filtering on prior verdicts is what
    keeps each review page to genuinely new decisions.
    """
    seen: dict[str, set[str | None]] = {}
    for p in paths:
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            # `to` is absent in verdict files downloaded before destinations were recorded.
            # None then means "judged, destination unknown" and suppresses the card entirely,
            # which is the old, conservative behaviour.
            seen.setdefault(r["card"], set()).add(r.get("to"))
    return seen


# Branch count is inherent: the command spans joint-vs-independent review, an optional
# accepted baseline, and prior-verdict filtering, and splitting them would separate code
# that has to agree on one payload shape.
def cmd_review(  # noqa: C901, PLR0912, PLR0915
    changes: list[str],
    out: Path,
    joint: bool = False,
    base_over: list[str] | None = None,
    judged: dict[str, set[str | None]] | None = None,
) -> None:
    """One review page covering several proposed changes at once.

    Each swap records which change produced it, but the page does NOT show that: a
    reviewer told "this one is the style change" would judge it differently from one
    told nothing. Blind review, attributed analysis.

    A card that swaps under two changes to the SAME printing is reviewed once and
    credited to both; to different printings, it appears once per change.
    """
    # The baseline can carry already-accepted changes. Without this every review would be
    # measured against the original config, so a second increment would re-show swaps
    # already judged and hide the ones the increment actually adds.
    base_w = dict(WEIGHTS)
    for spec in base_over or []:
        comp, val = spec.split("=", 1)
        if comp not in base_w:
            sys.exit(f"unknown component {comp!r}")
        base_w[comp] = float(val)
    if base_over:
        print("  baseline (already accepted): " + ", ".join(f"{c.split('=')[0]}={c.split('=')[1]}" for c in base_over))

    rows = load("c.raw_card_blob ->> 'lang' = 'en' AND c.prefer_score IS NOT NULL")
    # Memorabilia is already excluded from the printing COUNT, but that never stopped it
    # being displayed: 191 cards currently show one, 187 of them 30a, which Scryfall tags
    # with the 2015 frame (42 pts) while every one of its 592 printings is a lowres scan.
    by: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by.setdefault(r["card_name"], []).append(r)
    multi = {k: v for k, v in by.items() if len(v) > 1}
    if judged:
        before = len(multi)
        multi = {k: v for k, v in multi.items() if k not in judged}
        print(f"  excluding {before - len(multi)} cards already judged in a previous review")
    base = {k: max(v, key=lambda r: (score(r, base_w), r["sid"])) for k, v in multi.items()}

    items: dict[tuple[str, str], dict] = {}
    if joint:
        w = dict(base_w)
        for spec in changes:
            comp, val = spec.split("=", 1)
            if comp not in base_w:
                sys.exit(f"unknown component {comp!r}")
            w[comp] = float(val)
        desc = " + ".join(f"{c.split('=')[0]} {base_w[c.split('=')[0]]:g}->{float(c.split('=')[1]):g}" for c in changes)
        n_tie = n_over = 0
        for name, printings in multi.items():
            old = base[name]
            new = max(printings, key=lambda r: (score(r, w), r["sid"]))
            if old["sid"] == new["sid"]:
                continue
            kind = "tie" if abs(score(old, base_w) - score(new, base_w)) < SCORE_EPSILON else "override"
            n_tie += kind == "tie"
            n_over += kind == "override"
            items[(name, new["sid"])] = {"card": name, "old": side(old), "new": side(new), "kind": kind, "changes": [desc]}
        print(f"  JOINT {desc}")
        print(f"    {n_over} overrides, {n_tie} tie-breaks")
        payload = sorted(items.values(), key=lambda i: i["card"])
        slug = (
            ("from" + "-".join(c.replace("=", "") for c in (base_over or [])) + "-" if base_over else "")
            + "joint-"
            + "-".join(c.replace("=", "") for c in changes)[:40]
        )
        out.write_text(
            REVIEW_PAGE.replace("__SWAPS__", json.dumps(payload, separators=(",", ":")))
            .replace("__DESC__", json.dumps(f"{len(payload)} proposed swaps"))
            .replace("__SLUG__", json.dumps(slug))
        )
        print(f"\n  {len(payload)} swaps to review -> {out}")
        print(f"  download will be named: swap-verdicts-{slug}.jsonl")
        return
    for spec in changes:
        comp, val = spec.split("=", 1)
        if comp not in WEIGHTS:
            sys.exit(f"unknown component {comp!r}")
        w = dict(base_w)
        w[comp] = float(val)
        desc = f"{comp} {base_w[comp]:g}->{float(val):g}"
        n_tie = n_over = 0
        for name, printings in multi.items():
            old = base[name]
            new = max(printings, key=lambda r: (score(r, w), r["sid"]))
            if old["sid"] == new["sid"]:
                continue
            kind = "tie" if abs(score(old, base_w) - score(new, base_w)) < SCORE_EPSILON else "override"
            n_tie += kind == "tie"
            n_over += kind == "override"
            key = (name, new["sid"])
            if key in items:
                items[key]["changes"].append(desc)
            else:
                items[key] = {"card": name, "old": side(old), "new": side(new), "kind": kind, "changes": [desc]}
        print(f"  {desc:34s} {n_over:4d} overrides  {n_tie:4d} tie-breaks")

    payload = sorted(items.values(), key=lambda i: i["card"])
    slug = ("from" + "-".join(c.replace("=", "") for c in (base_over or [])) + "-" if base_over else "") + "-".join(
        c.replace("=", "") for c in changes
    )[:50]
    out.write_text(
        REVIEW_PAGE.replace("__SWAPS__", json.dumps(payload, separators=(",", ":")))
        .replace("__DESC__", json.dumps(f"{len(payload)} proposed swaps"))
        .replace("__SLUG__", json.dumps(slug))
    )
    print(f"\n  {len(payload)} swaps to review -> {out}")
    print(f"  download will be named: swap-verdicts-{slug}.jsonl")


# Seed for the left/right assignment in swap review. Fixed so a regenerated page puts the
# same cards on the same sides -- a reviewer who resumes must not be re-asked a card with
# the sides reversed, or their two answers would contradict each other.
REVIEW_FLIP_SEED = 20260725


# Same shape as cmd_review: one proposal can change the numerator, the candidate set and a
# weight at once, and they must be reviewed together because they interact -- dedup alone
# regressed 17 core-set foils that a finish weight masked.
def cmd_countfix(  # noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
    out: Path,
    base_over: list[str] | None = None,
    judged: dict[str, set[str | None]] | None = None,
    dedup: bool = False,
    prop_over: list[str] | None = None,
    drop_memo: bool = False,
    from_main: bool = False,
) -> None:
    """Review the swaps from changing what `illustration_count` counts.

    Not a weight change: every weight is held at its accepted value and only the
    numerator moves, from "every row sharing the illustration" to COUNTED_PRINTING_SQL.
    That isolates the counting rule -- any swap on this page is caused by fake or
    duplicate printings inflating an artwork's reprint count, nothing else.

    `--base` matters here. WEIGHTS holds art_style at 0 so `validate` can reproduce the
    un-backfilled database; without `--base art_style=14` this would measure against the
    pre-#766 config and re-ask swaps that change has already settled.
    """
    w = dict(WEIGHTS)
    for spec in base_over or []:
        comp, val = spec.split("=", 1)
        if comp not in w:
            sys.exit(f"unknown component {comp!r}")
        w[comp] = float(val)
    if base_over:
        print("  baseline (already accepted): " + ", ".join(base_over))

    rows = load("c.raw_card_blob ->> 'lang' = 'en' AND c.prefer_score IS NOT NULL")
    # Memorabilia is already excluded from the printing COUNT, but that never stopped it
    # being displayed: 191 cards currently show one, 187 of them 30a, which Scryfall tags
    # with the 2015 frame (42 pts) while every one of its 592 printings is a lowres scan.
    # Dropping it from candidacy is safe -- no card is memorabilia-only.
    by: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by.setdefault(r["card_name"], []).append(r)
    multi = {k: v for k, v in by.items() if len(v) > 1}
    # Applies to the PROPOSAL side only. Filtering both sides would hold the change constant
    # and hide its own effect -- an earlier run did exactly that and silently dropped the 219
    # cards this fixes from the review.
    prop_rows = {k: [r for r in v if r["set_type"] != "memorabilia"] for k, v in multi.items()} if drop_memo else multi
    if drop_memo:
        print(
            f"  proposal drops {sum(len(v) for v in multi.values()) - sum(len(v) for v in prop_rows.values())}"
            f" memorabilia printings from candidacy"
        )
    # dedup builds ON TOP of the already-reviewed exclusion rule: baseline counts real
    # rows, proposal counts distinct sets among them. Reviewing it against the unfixed
    # numerator instead would re-ask the 47 cards already judged.
    # from_main pins the baseline to what production scores today, so one review covers
    # the whole accumulated change rather than the delta from a config never shipped.
    old_key, new_key = ("illus_n_real", "illus_n_set") if dedup else ("illus_n", "illus_n_real")
    if from_main:
        old_key = "illus_n"
    inflated = sum(1 for r in rows if r[old_key] != r[new_key])
    print(f"  numerator: {old_key} -> {new_key}")
    print(f"  printings whose artwork count changes : {inflated} of {len(rows)}")
    print(f"  multi-printing cards                  : {len(multi)}")
    # Weights that apply to the PROPOSAL side only. A numerator change and a weight change
    # are often entangled -- dedup alone regressed 17 core-set foils because frame(+5) and
    # finish(-5) cancel exactly, leaving the count as the only separator -- so the pair has
    # to be reviewed as one proposal, not two.
    w_new = dict(w)
    for spec in prop_over or []:
        comp, val = spec.split("=", 1)
        if comp not in w_new:
            sys.exit(f"unknown component {comp!r}")
        w_new[comp] = float(val)
    if prop_over:
        print(
            "  proposal also changes: "
            + ", ".join(f"{c.split('=')[0]} {w[c.split('=')[0]]:g}->{c.split('=')[1]}" for c in prop_over)
        )

    # Exclusion is per DESTINATION, not per card. A verdict answers "is this printing better
    # than that one", so it only carries over when the proposal lands on the same printing
    # again; a card judged in an earlier batch that now moves somewhere else is a new
    # question. Filtering by card name (the original behaviour) silently dropped those.
    rng = random.Random(REVIEW_FLIP_SEED)
    swaps = []
    skipped = reasked = 0
    for name, printings in multi.items():
        cand = prop_rows.get(name) or printings
        old = max(printings, key=lambda r: (score(r, w, old_key), r["sid"]))
        new = max(cand, key=lambda r: (score(r, w_new, new_key), r["sid"]))
        if old["sid"] == new["sid"]:
            continue
        seen = (judged or {}).get(name)
        if seen is not None:
            dest = side(new)["loc"]
            if dest in seen or None in seen:  # None = legacy file with no destination recorded
                skipped += 1
                continue
            reasked += 1
        swaps.append({"card": name, "old": side(old), "new": side(new)})
    if judged:
        print(f"  skipped {skipped} cards already judged on this same destination")
        print(f"  re-asking {reasked} judged cards that now move somewhere different")
    swaps.sort(key=lambda s: s["card"])
    for s in swaps:  # after sorting, so the seed maps card -> side stably
        s["flip"] = rng.random() < COIN
    print(f"  proposal shown on the left for {sum(s['flip'] for s in swaps)} of {len(swaps)}")
    slug = ("illus-count-set-dedup" if dedup else "illus-count-real-printings") + (
        "-" + "-".join(c.replace("=", "") for c in prop_over) if prop_over else ""
    )
    out.write_text(
        REVIEW_PAGE.replace("__SWAPS__", json.dumps(swaps, separators=(",", ":")))
        .replace("__DESC__", json.dumps(f"{len(swaps)} proposed swaps"))
        .replace("__SLUG__", json.dumps(slug))
    )
    print(f"\n  {len(swaps)} swaps to review -> {out}")
    print(f"  download will be named: swap-verdicts-{slug}.jsonl")


# Controlled foil-vs-nonfoil pairs: same card, same artwork, same SET, same frame data,
# same border, same rarity, same scan quality. The only difference is the finish, which is
# what makes this a test of `finish_foil` rather than a test of whatever else moved.
#
# Needed because every earlier batch confounded finish with frame: of 69 same-artwork pairs
# in the dedup+foil review, 63 also changed frame (62/62 preferred the newer frame) and only
# 6 isolated finish (6/6 "no difference"). Six is too few to justify deleting a component
# that moves 11,411 printings.
FINISH_PAIR_SQL = """
WITH e AS (
  SELECT c.card_name, c.illustration_id, c.card_set_code, c.card_frame_data, c.card_border,
         c.card_rarity_int, c.raw_card_blob ->> 'image_status' AS img,
         c.raw_card_blob ->> 'collector_number' AS cn,
         {normal_url} AS url,
         CASE WHEN c.raw_card_blob -> 'finishes' ? 'nonfoil' THEN 'nonfoil'
              WHEN c.raw_card_blob -> 'finishes' ? 'foil' THEN 'foil' ELSE 'etched' END AS fin,
         -- Sorted, so ["showcase","legendary"] and ["legendary","showcase"] compare equal --
         -- raw text comparison reported those as different treatments when they are not.
         (SELECT COALESCE(array_agg(x ORDER BY x), '{{}}')
            FROM jsonb_array_elements_text(COALESCE(c.raw_card_blob -> 'frame_effects', '[]'::jsonb)) x
         ) AS effects,
         -- The decisive one: without it the foil side is usually a *special* foil
         -- (textured / surgefoil / ripplefoil) or a different promo product, which is a
         -- visible treatment difference and not the plain foil-vs-nonfoil question.
         (SELECT COALESCE(array_agg(x ORDER BY x), '{{}}')
            FROM jsonb_array_elements_text(COALESCE(c.raw_card_blob -> 'promo_types', '[]'::jsonb)) x
         ) AS promo_types,
         COALESCE(c.raw_card_blob ->> 'security_stamp', '') AS stamp,
         COALESCE(c.raw_card_blob ->> 'full_art', 'f')      AS full_art,
         COALESCE(c.raw_card_blob ->> 'textless', 'f')      AS textless,
         COALESCE(c.raw_card_blob ->> 'promo', 'f')         AS promo
  FROM magic.cards c
  WHERE c.raw_card_blob ->> 'lang' = 'en' AND c.illustration_id IS NOT NULL
    AND {normal_url} IS NOT NULL),
-- Grouped rather than self-joined: a self-join on jsonb frame data over 94k rows does not
-- finish, while one pass bucketing by the same key does. A bucket qualifies when it holds
-- both finishes, and everything in the key is by construction identical between them.
grouped AS (
  SELECT card_name, card_set_code,
         min(cn)  FILTER (WHERE fin = 'nonfoil') AS nonfoil_cn,
         min(url) FILTER (WHERE fin = 'nonfoil') AS nonfoil_url,
         min(cn)  FILTER (WHERE fin = 'foil')    AS foil_cn,
         min(url) FILTER (WHERE fin = 'foil')    AS foil_url
  FROM e
  GROUP BY card_name, illustration_id, card_set_code,
           card_frame_data::text, COALESCE(card_border, ''), card_rarity_int, img,
           effects, promo_types, stamp, full_art, textless, promo
  HAVING count(*) FILTER (WHERE fin = 'nonfoil') > 0
     AND count(*) FILTER (WHERE fin = 'foil')    > 0)
SELECT card_name, card_set_code, nonfoil_cn, nonfoil_url, foil_cn, foil_url FROM grouped
"""


def cmd_finishtest(out: Path, n: int) -> None:
    """Blind foil-vs-nonfoil review on pairs where nothing else differs.

    Stratified one-per-set before sampling, because the unstratified population is
    dominated by the 7th-10th Edition `*` foils -- a verdict drawn only from those would
    say something about 2003-era foil stock, not about foil in general.
    """
    rows = run_query(FINISH_PAIR_SQL.format(normal_url=front_image("normal", "c")))
    by_set: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_set.setdefault(r["card_set_code"], []).append(r)
    rng = random.Random(REVIEW_FLIP_SEED)
    for v in by_set.values():
        rng.shuffle(v)
    picked: list[dict[str, str]] = []
    while len(picked) < n and any(by_set.values()):  # round-robin across sets
        for code in sorted(by_set):
            if by_set[code] and len(picked) < n:
                picked.append(by_set[code].pop())
    print(f"  {len(rows)} controlled pairs across {len(by_set)} sets -> sampling {len(picked)}")

    swaps = []
    for r in sorted(picked, key=lambda x: x["card_name"]):
        loc = f"{r['card_set_code']}/"
        swaps.append(
            {
                "card": r["card_name"],
                "old": {"img": r["nonfoil_url"], "art": "", "loc": loc + r["nonfoil_cn"]},
                "new": {"img": r["foil_url"], "art": "", "loc": loc + r["foil_cn"]},
                "flip": rng.random() < COIN,
            }
        )
    print(f"  foil shown on the left for {sum(s['flip'] for s in swaps)} of {len(swaps)}")
    slug = "finish-foil-vs-nonfoil"
    out.write_text(
        REVIEW_PAGE.replace("__SWAPS__", json.dumps(swaps, separators=(",", ":")))
        .replace("__DESC__", json.dumps("same set, same art, same frame -- only the finish differs"))
        .replace("__SLUG__", json.dumps(slug))
    )
    print(f"\n  {len(swaps)} pairs to review -> {out}")
    print("  'better' in the output means FOIL was preferred; 'worse' means nonfoil")
    print(f"  download will be named: swap-verdicts-{slug}.jsonl")


def main() -> None:
    """Parse arguments and dispatch to a subcommand."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sw = sub.add_parser("swaps")
    sw.add_argument("--set", action="append", default=[], metavar="COMP=VALUE")
    sw.add_argument("--scale", action="append", default=[], metavar="COMP=FACTOR")
    sw.add_argument("--limit", type=int, default=400, help="max swaps to put in the review page")
    sw.add_argument("-o", "--out", type=Path, default=OUT_DIR / "swaps.html")
    st = sub.add_parser("step", help="smallest change to one component that yields ~6-9 swaps")
    st.add_argument("component")
    st.add_argument("direction", choices=("up", "down"))
    st.add_argument("-o", "--out", type=Path, default=OUT_DIR / "step.html")
    rv = sub.add_parser("review", help="one blind review page covering several proposed changes")
    rv.add_argument("--change", action="append", required=True, metavar="COMP=VALUE")
    rv.add_argument(
        "--joint",
        action="store_true",
        help="apply all --change values simultaneously and review the resulting "
        "config, rather than reviewing each change independently. Not the same "
        "set: measured art_style=6 + extended_art=10 jointly moves 101 cards vs "
        "86 for the union, because 15 only move when both weights move.",
    )
    rv.add_argument(
        "--base",
        action="append",
        default=[],
        metavar="COMP=VALUE",
        help="already-accepted weights to measure the change against, so an increment shows only what it newly adds",
    )
    rv.add_argument(
        "--judged",
        type=Path,
        nargs="*",
        default=[],
        help="previous swap-verdict JSONL files; their cards are excluded so the page only asks new questions",
    )
    rv.add_argument("-o", "--out", type=Path, default=OUT_DIR / "review.html")
    cf = sub.add_parser("countfix", help="review swaps from excluding fake/duplicate rows from the illustration_count numerator")
    cf.add_argument(
        "--base",
        action="append",
        default=[],
        metavar="COMP=VALUE",
        help="already-accepted weights to measure against; pass art_style=14 "
        "so the baseline is the shipped config, not the pre-#766 one",
    )
    cf.add_argument(
        "--set",
        action="append",
        default=[],
        dest="prop",
        metavar="COMP=VALUE",
        help="weight change applied to the PROPOSAL side only, reviewed jointly with the numerator change",
    )
    cf.add_argument(
        "--from-main",
        action="store_true",
        dest="from_main",
        help="baseline is the stored production score, not an intermediate config",
    )
    cf.add_argument(
        "--no-memorabilia",
        action="store_true",
        dest="drop_memo",
        help="also stop memorabilia products being DISPLAYED, not just counted",
    )
    cf.add_argument(
        "--judged", type=Path, nargs="*", default=[], help="previous swap-verdict JSONL files; their cards are excluded"
    )
    cf.add_argument(
        "--dedup",
        action="store_true",
        help="review the NEXT step: one credit per set, on top of the exclusion "
        "rule. Bare set is deliberate -- frame/border/finish differences are "
        "already priced by their own components, so keeping them in the dedup "
        "key would count a retro-frame reprint twice.",
    )
    cf.add_argument("-o", "--out", type=Path, default=OUT_DIR / "countfix.html")
    ft = sub.add_parser("finishtest", help="blind foil-vs-nonfoil review with everything else held equal")
    ft.add_argument("-n", type=int, default=50, help="number of pairs to sample")
    ft.add_argument("-o", "--out", type=Path, default=OUT_DIR / "finishtest.html")
    a = ap.parse_args()
    if a.cmd == "validate":
        cmd_validate()
    elif a.cmd == "finishtest":
        cmd_finishtest(a.out, a.n)
    elif a.cmd == "countfix":
        cmd_countfix(a.out, a.base, load_judged(a.judged) if a.judged else None, a.dedup, a.prop, a.drop_memo, a.from_main)
    elif a.cmd == "review":
        cmd_review(a.change, a.out, a.joint, a.base, load_judged(a.judged) if a.judged else None)
    elif a.cmd == "step":
        cmd_step(a.component, a.direction, a.out)
    else:
        cmd_swaps(a.set, a.scale, a.out, a.limit)


if __name__ == "__main__":
    main()
