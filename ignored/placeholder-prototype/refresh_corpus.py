"""Rebuild frame_corpus.json + corpus_meta.json from the local default_cards dump.

Larger, exposure-aware sample than the original ~6k corpus: m15/modern get at least
as much data as old (they were the thinnest buckets relative to traffic), old is
stratified by era (1993/1997) for the per-era trim fits, and white-border cards are
sampled as members of their black-border bucket (border is a modifier, not a
generation). Exotic layouts are excluded from the template corpus entirely — they
are still *served* by the nearest bucket, they just don't get to vote on templates.

Writes the two corpus JSONs and downloads missing images into images/.
"""

import collections
import concurrent.futures
import json
import pathlib
import random
import urllib.parse
import urllib.request

import zstandard

HERE = pathlib.Path(__file__).parent
DUMP_DIR = pathlib.Path("/Users/joseph.bylund/scratch/sylvan_librarian/data/api/blue/default_cards")
CDN = "https://d1hot9ps2xugbc.cloudfront.net/img/{set}/{cn}/1/280.webp"
IMAGES = HERE / "images"
rng = random.Random(7)

# per (color, template_gen) targets; old is additionally split 50/50 by era
TARGETS = {"old": 300, "modern": 250, "m15": 300, "borderless": 200, "fullart": 150}

_FRAME_GROUPS = {"1993": "old", "1997": "old", "2003": "modern"}


def gen_for(meta: dict) -> str:
    if meta.get("full_art") or meta.get("border_color") == "borderless":
        return "fullart" if "Basic" in (meta.get("type_line") or "") else "borderless"
    return _FRAME_GROUPS.get(meta.get("frame") or "", "m15")


def color_group(card: dict) -> str | None:
    tl = card.get("type_line") or ""
    if "Land" in tl:
        return "land"
    colors = card.get("colors")
    if colors is None:  # multi-face card: frame color follows the front face
        faces = card.get("card_faces") or []
        colors = faces[0].get("colors", []) if faces else []
    if "Artifact" in tl or not colors:
        return "artifact"
    if len(colors) >= 2:
        return "gold"
    return colors[0].lower()


def main() -> None:
    dump_path = sorted(DUMP_DIR.glob("default-cards-*.json.zstd"))[-1]
    print(f"reading {dump_path.name}")
    with open(dump_path, "rb") as fh:
        cards = json.loads(zstandard.ZstdDecompressor().stream_reader(fh).read())
    print(f"{len(cards):,} printings in dump")

    pools = collections.defaultdict(list)
    for c in cards:
        # template-corpus hygiene: standard single-face layout, real paper scan
        if c.get("layout") != "normal" or c.get("digital"):
            continue
        if c.get("image_status") not in ("highres_scan", "lowres"):
            continue
        if c.get("lang") and c["lang"] != "en":
            continue
        color = color_group(c)
        if color is None:
            continue
        meta = {
            "border_color": c.get("border_color"),
            "full_art": bool(c.get("full_art")),
            "frame": c.get("frame"),
            "frame_effects": c.get("frame_effects"),
            "type_line": c.get("type_line"),
        }
        gen = gen_for(meta)
        # Basics don't get to vote on land frame templates (fullart excepted — that
        # bucket IS basics): their watermark mana symbol and giant centered rules
        # text are aligned, low-frequency card content that survives both the
        # ink-vote and any averaging, and basics flood any land sample.
        if gen != "fullart" and "Basic" in (meta["type_line"] or ""):
            continue
        key = f"{c['set']}__{c['collector_number']}"
        # old is stratified per era so both trim fits are well fed
        stratum = (color, gen, meta["frame"]) if gen == "old" else (color, gen)
        pools[stratum].append((key, meta, c.get("name")))

    corpus, corpus_meta = {}, {}
    counts = collections.Counter()
    for stratum, members in sorted(pools.items()):
        color, gen = stratum[0], stratum[1]
        target = TARGETS[gen] // 2 if gen == "old" else TARGETS[gen]
        rng.shuffle(members)
        for key, meta, _name in members[:target]:
            corpus[key] = {"color": color, "gen": gen}
            corpus_meta[key] = meta
            counts[(color, gen)] += 1

    for (color, gen), n in sorted(counts.items()):
        print(f"  {color:9s} {gen:11s} n={n}")
    print(f"corpus: {len(corpus):,} cards")

    IMAGES.mkdir(exist_ok=True)
    todo = [k for k in corpus if not (IMAGES / f"{k}.webp").exists()]
    print(f"fetching {len(todo):,} images ({len(corpus) - len(todo):,} already present)")

    def download(key: str) -> bool:
        set_code, cn = key.split("__", 1)
        url = CDN.format(set=urllib.parse.quote(set_code), cn=urllib.parse.quote(cn))
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
            if not data.startswith(b"RIFF"):
                return False
            (IMAGES / f"{key}.webp").write_bytes(data)
            return True
        except Exception:
            return False

    with concurrent.futures.ThreadPoolExecutor(12) as pool:
        results = dict(zip(todo, pool.map(download, todo)))
    failed = [k for k, ok in results.items() if not ok]
    for k in failed:  # drop cards whose image isn't on the CDN
        corpus.pop(k, None)
        corpus_meta.pop(k, None)
    print(f"downloaded {len(results) - len(failed):,}, failed {len(failed):,}; final corpus {len(corpus):,}")

    (HERE / "frame_corpus.json").write_text(json.dumps(corpus, indent=0, sort_keys=True))
    (HERE / "corpus_meta.json").write_text(json.dumps(corpus_meta, indent=0, sort_keys=True))


if __name__ == "__main__":
    main()
