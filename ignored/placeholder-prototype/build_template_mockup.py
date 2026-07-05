"""Mockup: template + tint placeholders, one column per fidelity stage.

Stages per card, left to right:
  1. bucket defaults  — class only, no per-card data (the unmeasured-printing fallback)
  2. flat art color   — measured frame tints + mean art color (PR #608 as shipped)
  3. art 4x3 / 8x6 / 16x12 — same frame tints, art layer replaced by a tiny webp data URI
  4. real             — the actual card over its best placeholder (global toggle fades it)

The art layer is a single CSS var (--art-layer) holding either a flat gradient or a
data URI, so upgrading fidelity changes bytes, not architecture.
"""

import base64
import io
import json
import pathlib
import random

import numpy as np
from PIL import Image, ImageFilter

HERE = pathlib.Path(__file__).parent
OUT = pathlib.Path("/Users/joseph.bylund/scratch/sylvan_librarian/ignored/placeholder-prototype")
CDN = "https://d1hot9ps2xugbc.cloudfront.net/img/{set}/{cn}/1/280.webp"
rng = random.Random(7)

TPL_W, TPL_H = 112, 157
BORDER_FRAC = 0.05
SAMPLES_PER_BUCKET = 6
ART_GRIDS = [(4, 3), (8, 6), (16, 12)]
ART_WEBP_QUALITY = 55  # q40 shows chroma blocking at 16x12; +q costs single-digit bytes here

ART_RECTS = {
    "old": (0.10, 0.105, 0.90, 0.525),
    "old-wb": (0.10, 0.105, 0.90, 0.525),
    "modern": (0.075, 0.11, 0.925, 0.545),
    "modern-wb": (0.075, 0.11, 0.925, 0.545),
    "m15": (0.06, 0.105, 0.94, 0.56),
    "borderless": (0.02, 0.095, 0.98, 0.555),  # art to edges below the opaque title bar
    "fullart": (0.045, 0.045, 0.955, 0.82),   # strip layout (full-art basics)
}
GEN_ORDER = ["old", "old-wb", "modern", "modern-wb", "m15", "borderless", "fullart"]
COLOR_ORDER = ["w", "u", "b", "r", "g", "gold", "artifact", "land"]
BORDER_COLORS = {"old-wb": "#e8e0d0", "modern-wb": "#e8e0d0"}
DEFAULT_BORDER = "#0d0d0d"
COLOR_TITLES = {
    "w": "White", "u": "Blue", "b": "Black", "r": "Red", "g": "Green",
    "gold": "Gold", "artifact": "Artifact", "land": "Land",
}
GEN_TITLES = {
    "old": "old frame", "old-wb": "old frame, white border",
    "modern": "modern frame (2003-14)", "modern-wb": "modern frame, white border",
    "m15": "M15 frame (2015+)",
    "borderless": "borderless (art to edges, real text box)", "fullart": "full-art strip (basics)",
}
LUM = np.array([0.299, 0.587, 0.114])

# display swatches for the five colors; gold-frame cards' text boxes are tinted by the
# card's component colors (in mana-cost order), not by the gold frame trim
SWATCHES = {"W": (248, 244, 214), "U": (30, 106, 158), "B": (59, 52, 60), "R": (211, 66, 50), "G": (0, 115, 62)}


def cost_color_pair(mana_cost: str | None, colors: list | None) -> tuple | None:
    """(left, right) component colors in mana-cost order; None when not applicable."""
    symbols = [ch for ch in (mana_cost or "") if ch in SWATCHES]
    seen = []
    for ch in symbols:
        if ch not in seen:
            seen.append(ch)
    if not seen and colors:
        seen = [c for c in colors if c in SWATCHES]
    if not seen:
        return None
    return seen[0], seen[-1]


_FRAME_GROUPS = {"1993": "old", "1997": "old", "2003": "modern"}


def gen_for(meta: dict) -> str:
    """Frame generation from real card metadata — mirrors production bucket_for.

    Scryfall's full_art flag covers both strip-layout basics and borderless cards
    that keep a text box (e.g. DMU 'inverted' lands), so nonbasics go to the
    borderless bucket and only basic lands get the strip template.
    """
    if meta.get("full_art") or meta.get("border_color") == "borderless":
        return "fullart" if "Basic" in (meta.get("type_line") or "") else "borderless"
    if meta.get("border_color") == "white":
        return "old-wb" if meta.get("frame") in ("1993", "1997") else "modern-wb"
    return _FRAME_GROUPS.get(meta.get("frame") or "", "m15")


def load_arr(key: str) -> np.ndarray | None:
    try:
        with Image.open(HERE / "images" / f"{key}.webp") as img:
            return np.asarray(img.convert("RGB").resize((TPL_W, TPL_H), Image.LANCZOS), dtype=np.float32)
    except Exception:
        return None


def load_full(key: str) -> Image.Image:
    with Image.open(HERE / "images" / f"{key}.webp") as img:
        return img.convert("RGB")


def masks_for(gen: str) -> tuple:
    """(art_mask, tint_mask, left_mask, right_mask).

    tint_mask (template normalization) is frame minus border minus art. The left/right
    tint masks are stricter: only the text-box band below the art window, which is
    guaranteed art-free even if the art rect slightly underestimates the real window
    (art bleed into the side rails was painting phantom gradients on symmetric cards),
    and is where dual-frame gradients actually live. Transition band excluded as before.
    """
    x0, y0, x1, y1 = ART_RECTS[gen]
    art = np.zeros((TPL_H, TPL_W), dtype=bool)
    art[round(y0 * TPL_H):round(y1 * TPL_H), round(x0 * TPL_W):round(x1 * TPL_W)] = True
    inner = np.zeros((TPL_H, TPL_W), dtype=bool)
    bx, by = round(BORDER_FRAC * TPL_W), round(BORDER_FRAC * TPL_H)
    inner[by:-by, bx:-bx] = True
    tint = inner & ~art
    # L/R card-color tints come from the colored frame material only: exclude the
    # border (inner), a DILATED art rect (underestimated windows can't leak art),
    # and the text box INTERIOR (neutral parchment dilutes saturation — the colored
    # trim around the box still counts). Falls back to keeping the box interior when
    # the strict mask leaves too few pixels (e.g. full-art strip layouts).
    dilated = np.zeros((TPL_H, TPL_W), dtype=bool)
    dilated[
        round((y0 - 0.012) * TPL_H):round((y1 + 0.045) * TPL_H),
        max(0, round((x0 - 0.03) * TPL_W)):round((x1 + 0.03) * TPL_W),
    ] = True
    box_inset = 0.0 if gen == "borderless" else BORDER_FRAC + 0.03
    box_interior = np.zeros((TPL_H, TPL_W), dtype=bool)
    box_interior[
        round((y1 + 0.065) * TPL_H):round(0.90 * TPL_H),
        round(box_inset * TPL_W):round((1 - box_inset) * TPL_W) or TPL_W,
    ] = True
    # Borderless cards: the box is the dominant frame surface and its color is
    # per-card arbitrary (translucent dark, comic white...), while the title bar is a
    # sliver — so measure the per-card colors FROM the box. Framed cards: measure the
    # colored trim and let the fitted bucket model derive the box.
    if gen == "borderless":
        lr = inner & box_interior
    else:
        lr = inner & ~dilated & ~box_interior
    cols = np.arange(TPL_W)[None, :].repeat(TPL_H, axis=0)
    left, right = lr & (cols < 0.42 * TPL_W), lr & (cols > 0.58 * TPL_W)
    if left.sum() < 40 or right.sum() < 40:
        lr = inner & ~dilated
        left, right = lr & (cols < 0.42 * TPL_W), lr & (cols > 0.58 * TPL_W)
    return art, tint, left, right, box_interior


def solve_tint(card: np.ndarray, tpl_lum: np.ndarray, mask: np.ndarray) -> np.ndarray:
    t = tpl_lum[mask] / 255.0
    c = card[mask]
    return np.clip((t[:, None] * c).sum(axis=0) / max(float((t * t).sum()), 1e-6), 0, 255)


def hexify(rgb) -> str:
    r, g, b = np.asarray(rgb).round().astype(int)
    return f"#{r:02x}{g:02x}{b:02x}"


def art_region(full: Image.Image, gen: str) -> Image.Image:
    x0, y0, x1, y1 = ART_RECTS[gen]
    w, h = full.size
    return full.crop((round(x0 * w), round(y0 * h), round(x1 * w), round(y1 * h)))


def art_color(card: np.ndarray, gen: str) -> str:
    x0, y0, x1, y1 = ART_RECTS[gen]
    dx, dy = (x1 - x0) * 0.12, (y1 - y0) * 0.12
    region = card[
        round((y0 + dy) * TPL_H):round((y1 - dy) * TPL_H),
        round((x0 + dx) * TPL_W):round((x1 - dx) * TPL_W),
    ]
    return hexify(region.reshape(-1, 3).mean(axis=0))


def art_uri(full: Image.Image, gen: str, gx: int, gy: int) -> tuple[str, int]:
    """(data URI, payload bytes) for the art window downsampled to gx x gy."""
    small = art_region(full, gen).resize((gx, gy), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "WEBP", quality=ART_WEBP_QUALITY, method=6)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode(), buf.tell()


def main() -> None:
    corpus = json.loads((HERE / "frame_corpus.json").read_text())
    corpus_meta = json.loads((HERE / "corpus_meta.json").read_text())

    sums = {}
    keys_by_bucket = {}
    for key, info in corpus.items():
        arr = load_arr(key)
        meta = corpus_meta.get(key)
        if arr is None or meta is None:
            continue
        gen = gen_for(meta)
        bucket = (info["color"], gen)
        sums[bucket] = arr if bucket not in sums else sums[bucket] + arr
        keys_by_bucket.setdefault(bucket, []).append(key)

    sections, tpl_css = [], []
    HDRS_TOKEN = "<!--STAGE-HDRS-->"
    uri_chars = {g: [] for g in ART_GRIDS}

    for color in COLOR_ORDER:
        for gen in GEN_ORDER:
            bucket = (color, gen)
            keys = keys_by_bucket.get(bucket, [])
            if len(keys) < 15:
                continue
            art_mask, tint_mask, left_mask, right_mask, box_mask = masks_for(gen)
            lum = (sums[bucket] / len(keys)) @ LUM
            lum = np.clip(lum * (235.0 / max(float(lum[tint_mask].mean()), 1.0)), 0, 255)
            alpha = np.full((TPL_H, TPL_W), 255, dtype=np.uint8)
            alpha[art_mask] = 0
            alpha_img = Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(1.2))
            tpl_img = Image.merge("LA", (Image.fromarray(lum.astype(np.uint8)), alpha_img)).convert("RGBA")
            buf = io.BytesIO()
            tpl_img.save(buf, "WEBP", quality=55, method=6)
            tpl_uri = "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()

            # Measure trim tints AND box halves for every fit member, then decide which
            # region carries this bucket's per-card signal: buckets whose box varies more
            # than their trim (old dual lands, gold frames, borderless) store box colors
            # and freeze the trim to the bucket constant; the rest store trim tints and
            # derive the box from the fitted mix model.
            mid = TPL_W // 2
            cols_idx = np.arange(TPL_W)[None, :]
            box_l_mask, box_r_mask = box_mask & (cols_idx < mid), box_mask & (cols_idx >= mid)
            # Fit on PER-SIDE observations — (left trim, left box), (right trim, right box).
            # Per-card averages cancel the dual-land gradient out of both variables and
            # made the fitted mix ratio underestimate two-color boxes.
            fit_keys = keys[:: max(1, len(keys) // 60)]
            tints, boxes = [], []
            for key in fit_keys:
                card = load_arr(key)
                if card is None:
                    continue
                tints.append(solve_tint(card, lum, left_mask))
                boxes.append(card[box_l_mask].reshape(-1, 3).mean(axis=0))
                tints.append(solve_tint(card, lum, right_mask))
                boxes.append(card[box_r_mask].reshape(-1, 3).mean(axis=0))
            # compare CHROMA spread (hue variation, brightness removed): raw std is
            # dominated by scan-brightness noise and hides that e.g. old-land trim is
            # always brown while its boxes span blue-black to green-white
            def chroma_spread(obs: list) -> float:
                arr = np.array(obs)
                return float((arr - arr.mean(axis=1, keepdims=True)).std(axis=0).mean())

            trim_spread = chroma_spread(tints)
            box_spread = chroma_spread(boxes)
            trim_med = np.median(np.array(tints), axis=0)
            # Unified model: per-card colors ARE the box colors (largest, most variable
            # region — dual-land gradients, gold component tints, parchment vs colored
            # boxes). The trim is derived: bucket-constant hue mixed with the measured
            # box color, so per-card brightness (bright CED prints) still flows through.
            # Fit trim ~= p * box + (1-p) * trimbase over side-wise observations.
            T = np.array(tints)  # n x 3 side-wise trim observations
            B = np.array(boxes)  # n x 3 side-wise box observations
            Tc, Bc = T - T.mean(axis=0), B - B.mean(axis=0)
            p_mix = float(np.clip((Bc * Tc).sum() / max((Bc * Bc).sum(), 1e-6), 0.05, 0.95))
            base = hexify(np.clip((T.mean(axis=0) - p_mix * B.mean(axis=0)) / (1 - p_mix), 0, 255))

            samples = rng.sample(keys, min(SAMPLES_PER_BUCKET, len(keys)))
            measured = []
            for key in samples:
                card = load_arr(key)
                fl = hexify(card[box_l_mask].reshape(-1, 3).mean(axis=0))
                fr = hexify(card[box_r_mask].reshape(-1, 3).mean(axis=0))
                measured.append((key, fl, fr, art_color(card, gen)))
            channels = np.array([
                [int(v[i:i + 2], 16) for v in (fl, fr, ac) for i in (1, 3, 5)]
                for _, fl, fr, ac in measured
            ])
            med = np.median(channels, axis=0)
            d_fl, d_fr, d_ac = hexify(med[0:3]), hexify(med[3:6]), hexify(med[6:9])

            x0, y0, x1, y1 = ART_RECTS[gen]
            aw, ah = (x1 - x0) * 100, (y1 - y0) * 100
            px = x0 / (1 - (x1 - x0)) * 100
            py = y0 / (1 - (y1 - y0)) * 100
            fw = (1 - 2 * BORDER_FRAC) * 100
            # box layer geometry (matches the box-interior measurement rect)
            by0, by1 = y1 + 0.065, 0.90
            bxf = BORDER_FRAC + 0.03
            bw, bh = (1 - 2 * bxf) * 100, (by1 - by0) * 100
            bpy = by0 / (1 - (by1 - by0)) * 100
            pm = round(p_mix * 100)
            box_grad = "linear-gradient(90deg,var(--frame-l) 0 40%,var(--frame-r) 60% 100%)"
            trim_grad = (
                f"linear-gradient(90deg,color-mix(in srgb,var(--frame-l) {pm}%,{base}) 0 40%,"
                f"color-mix(in srgb,var(--frame-r) {pm}%,{base}) 60% 100%)"
            )
            cls = f"b-{color}-{gen}"
            tpl_css.append(
                f".{cls}{{--tpl:url({tpl_uri});"
                f"--frame-l:{d_fl};--frame-r:{d_fr};--art-layer:linear-gradient({d_ac},{d_ac});"
                f"--box-layer:{box_grad};"
                f"background-color:{BORDER_COLORS.get(gen, DEFAULT_BORDER)};"
                f"background-image:var(--tpl),var(--art-layer),var(--box-layer),{trim_grad};"
                f"background-size:100% 100%,{aw:.1f}% {ah:.1f}%,{bw:.1f}% {bh:.1f}%,{fw:.1f}% {fw:.1f}%;"
                f"background-position:center,{px:.1f}% {py:.1f}%,50% {bpy:.1f}%,center}}"
            )

            rows = []
            for key, fl, fr, ac in measured:
                full = load_full(key)
                set_code, cn = key.split("__", 1)
                tint = f"--frame-l:{fl};--frame-r:{fr}"
                cells = [
                    f'<div class="rowcap">{set_code}<br>#{cn}</div>',
                    f'<div class="ph-card {cls}"></div>',
                    f'<div class="ph-card {cls}" style="{tint};--art-layer:linear-gradient({ac},{ac})"></div>',
                ]
                best_uri = None
                for gx, gy in ART_GRIDS:
                    uri, nbytes = art_uri(full, gen, gx, gy)
                    uri_chars[(gx, gy)].append(-(-nbytes * 4 // 3))
                    cells.append(f'<div class="ph-card {cls}" style="{tint};--art-layer:url({uri})"></div>')
                    best_uri = uri
                cells.append(
                    f'<div class="ph-card {cls}" style="{tint};--art-layer:url({best_uri})">'
                    f'<img loading="lazy" src="{CDN.format(set=set_code, cn=cn)}" alt="{key}"></div>'
                )
                rows.append("".join(cells))
            sections.append(
                f'<section><h2>{COLOR_TITLES[color]} — {GEN_TITLES[gen]} '
                f'<span class="meta">n={len(keys)}</span></h2><div class="stage-grid">'
                + HDRS_TOKEN
                + "".join(rows)
                + "</div></section>"
            )

    # full stored-value cost per tier: "<bucket> <fl> <fr>" ~= 23ch, then the art field
    # (7ch flat color, or the b64 thumb payload replacing it — prefix added client-side)
    base = 23
    thumb_meds = {g: int(np.median(chars)) for g, chars in uri_chars.items()}
    # base = "<bucket> <frameL> <frameR>" shared by every measured tier; the art
    # field is the marginal cost (7ch flat hex color, or the b64 thumb payload)
    stage_hdrs = (
        ["card", "bucket defaults · 0ch stored", f"flat art · {base}+7ch"]
        + [f"art {gx}x{gy} · {base}+~{thumb_meds[(gx, gy)]}ch" for gx, gy in ART_GRIDS]
        + ["real · 20-40KB"]
    )
    hdr_html = "".join(f'<div class="hdr">{h}</div>' for h in stage_hdrs)
    sections = [sec.replace(HDRS_TOKEN, hdr_html) for sec in sections]
    stats = " · ".join(
        f"{gx}x{gy}: ~{base + m}ch/card, ~{(base + m) * 60 / 1024:.1f}KB per 60-card response"
        for (gx, gy), m in thumb_meds.items()
    )
    n_cols = len(stage_hdrs) - 1
    page = f"""<!doctype html>
<meta charset="utf-8">
<title>Placeholder fidelity stages</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #1c2128; color: #ccc; margin: 2em; }}
  h1 {{ font-size: 1.3em; }} h2 {{ margin: 1.4em 0 .4em; }} .meta {{ font-size: .72em; color: #888; font-weight: normal; }}
  .stage-grid {{ display: grid; grid-template-columns: 64px repeat({n_cols}, 104px);
                 gap: 8px; align-items: center; border-top: 1px solid #333; padding-top: 8px; }}
  .hdr {{ font-size: .66em; color: #999; text-align: center; }}
  .rowcap {{ font-size: .66em; color: #888; text-align: right; padding-right: 2px; }}
  .ph-card {{
    aspect-ratio: 745/1041; width: 104px; border-radius: 5.5%/4%; overflow: hidden;
    background-image: var(--tpl), var(--art-layer), var(--box-layer), linear-gradient(90deg, var(--frame-l) 0 40%, var(--frame-r) 60% 100%);
    background-repeat: no-repeat;
    background-blend-mode: multiply, normal, normal, normal;
  }}
  .ph-card img {{ width: 100%; height: 100%; object-fit: contain; display: block; transition: opacity .4s; }}
  .placeholders-only .ph-card img {{ opacity: 0; }}
  #toggle {{ position: fixed; top: 12px; right: 12px; z-index: 9; padding: 8px 14px; border-radius: 8px;
             border: 1px solid #555; background: #2d333b; color: #ddd; cursor: pointer; }}
  {"".join(tpl_css)}
</style>
<button id="toggle">Show placeholders only</button>
<h1>Placeholder fidelity stages — data-URI payload medians: {stats}</h1>
<p class="meta">Left to right: bucket defaults (no per-card data) → measured tints + flat art color (#608 as shipped)
→ art window as a tiny webp data URI at three sizes → the real card over the 16x12 placeholder.
The art layer is one CSS var; each stage only changes its value.</p>
{"".join(sections)}
<script>
  const btn = document.getElementById("toggle");
  btn.onclick = () => {{
    document.body.classList.toggle("placeholders-only");
    btn.textContent = document.body.classList.contains("placeholders-only")
      ? "Show real cards" : "Show placeholders only";
  }};
</script>
"""
    (OUT / "template-mockup.html").write_text(page)
    print(f"wrote {OUT / 'template-mockup.html'} ({len(tpl_css)} buckets); {stats}")


if __name__ == "__main__":
    main()
