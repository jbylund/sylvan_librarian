"""Distill the Common Crawl scryfall.com/search harvest into a wild-query corpus.

Input:  benchmarks/wild-queries/cc-raw.jsonl  (one {"url": ...} per line, from the
        index.commoncrawl.org URL-index harvest)
Output: benchmarks/wild-queries/wild-corpus.jsonl — one row per distinct query that
        our parser accepts: {"q", "weight", "unique", "order"}. weight is the raw
        URL-occurrence count across crawls; unique/order are the modal accompanying
        params (falling back to card/edhrec, the API defaults we benchmark with).

Cleaning is deliberately minimal: HTML-unescape, Unicode quote/dash/nbsp
normalization (encoding artifacts of how the URLs were embedded), and a spam
filter. Queries are never rewritten — anything our parser rejects is dropped and
tallied in the gap census printed at the end, which doubles as a list of
real-world syntax we don't support.

    .venv/bin/python scripts/build_wild_corpus.py
"""

from __future__ import annotations

import html
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict
from urllib.parse import parse_qs, urlparse

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.parsing import parse_scryfall_query  # noqa: E402

RAW = REPO_ROOT / "benchmarks/wild-queries/cc-raw.jsonl"
OUT = REPO_ROOT / "benchmarks/wild-queries/wild-corpus.jsonl"

# Encoding artifacts only — never touches query semantics.
_TRANSLATE = str.maketrans(
    {
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u00a0": " ",  # no-break space
    }
)
MAX_Q_LEN = 200
_SPAM_RE = re.compile(r"[Ѐ-ӿ฀-๿一-鿿가-힯]")
_OP_RE = re.compile(r"[a-z]+[:<>=]", re.I)
_VALID_UNIQUE = {"card", "cards", "prints", "art"}
_VALID_ORDER = {"name", "released", "set", "edhrec", "color", "cmc", "usd", "rarity", "power", "toughness", "artist", "random"}


def clean(q: str) -> str | None:
    """Normalize one raw q value; None means drop (spam / oversized / empty)."""
    q = html.unescape(html.unescape(q)).translate(_TRANSLATE)
    q = " ".join(q.split())
    if not q or len(q) > MAX_Q_LEN or _SPAM_RE.search(q):
        return None
    return q


def main() -> None:
    """Read the raw harvest, parse-check every distinct query, write the corpus."""
    weights: Counter[str] = Counter()
    params: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    with RAW.open() as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            qs = parse_qs(urlparse(row["url"]).query)
            raw_q = qs.get("q", [None])[0]
            if raw_q is None:
                continue
            q = clean(raw_q)
            if q is None:
                continue
            weights[q] += 1
            for key in ("unique", "order"):
                val = qs.get(key, [None])[0]
                if val:
                    params[q][(key, val.lower())] += 1

    kept: list[dict] = []
    gap_census: Counter[str] = Counter()
    rejected_ops = 0
    for q, weight in weights.items():
        try:
            parse_scryfall_query(q)
        except Exception:  # noqa: BLE001 -- any parser rejection goes to the census
            ops = _OP_RE.findall(q.lower())
            if ops:
                rejected_ops += 1
                for op in set(ops):
                    gap_census[op] += 1
            continue
        modal = params.get(q, Counter())
        unique = next((v for (k, v), _ in modal.most_common() if k == "unique" and v in _VALID_UNIQUE), "card")
        order = next((v for (k, v), _ in modal.most_common() if k == "order" and v in _VALID_ORDER), "edhrec")
        kept.append({"q": q, "weight": weight, "unique": "card" if unique == "cards" else unique, "order": order})

    kept.sort(key=lambda r: (-r["weight"], r["q"]))
    with OUT.open("w") as fh:
        for row in kept:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    n_ops = sum(1 for r in kept if _OP_RE.search(r["q"]))
    print(f"kept {len(kept):,} distinct queries ({n_ops:,} with operators, {len(kept) - n_ops:,} name lookups)")
    print(f"rejected {rejected_ops:,} operator queries our parser does not accept")
    print("gap census (operators present in rejected queries):")
    for op, n in gap_census.most_common(20):
        print(f"  {op:<14} {n:>5}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
