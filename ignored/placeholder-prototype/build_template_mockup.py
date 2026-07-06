"""Mockup: template + tint placeholders, one column per fidelity stage.

Stages per card, left to right:
  1. bucket defaults  — class only, no per-card data (the unmeasured-printing fallback)
  2. flat art color   — measured frame tints + mean art color (PR #608 as shipped)
  3. art 4x3 / 8x6 / 16x12 — same frame tints, art layer replaced by a tiny webp data URI
  4. real             — the actual card over its best placeholder (global toggle fades it)

The art layer is a single CSS var (--art-layer) holding either a flat gradient or a
data URI, so upgrading fidelity changes bytes, not architecture.

Design revisions vs the first mockup (all measured on the era/border analyses):
  - White border is a MODIFIER, not a generation: wb cards share their black-border
    bucket's template (interior |Δ| 6-15, below the 93/97 era delta) and get their
    border from one `.ph-wb` background-color rule. Templates are ring-trimmed
    (transparent outside the inner rect) so background-color paints the border.
  - 1993/1997 split as trim CONSTANTS on a shared old template: the measured era
    error (signed trim residual up to ±16 RGB) lives in the fitted (p, base), not
    the template pixels, so each old bucket emits `.b-<c>-o93` / `.b-<c>-o97` rules
    that share one --tpl URI and differ only in --trim.
  - Templates use an ink-rejecting per-pixel vote: each card abstains where it has
    high-frequency content (text ink of either polarity), so glyph ghosts don't get
    averaged into the template; no-quorum pixels (aligned text) fall back to a
    blurred trimmed mean. Measured: box high-pass energy 25-40% below plain mean.
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
# Measured per-gen border ring (left, top, right, bottom fractions) from stack
# luminance profiles: old/modern ~5% uniform; m15 has thin 3.2% sides but a
# ~7.5% black collector strip at the bottom. A uniform 5% both swallowed m15's
# thin colored rails and let the black bottom strip leak into the template.
BORDER_INSETS = {
    "old": (0.05, 0.05, 0.05, 0.05),
    "modern": (0.05, 0.05, 0.05, 0.055),
    "m15": (0.032, 0.032, 0.032, 0.075),
    "borderless": (0.02, 0.02, 0.02, 0.02),
    "fullart": (0.03, 0.03, 0.03, 0.03),
}


def inner_mask(gen: str) -> np.ndarray:
    left, top, right, bottom = BORDER_INSETS[gen]
    m = np.zeros((TPL_H, TPL_W), dtype=bool)
    m[round(top * TPL_H):TPL_H - round(bottom * TPL_H), round(left * TPL_W):TPL_W - round(right * TPL_W)] = True
    return m
SAMPLES_PER_BUCKET = 6
FIT_PER_ERA = 80  # side-wise fit observations per era (or per bucket when unsplit)
TRIM_FRAC = 0.15  # per-pixel trimmed-mean fraction, each tail
ART_GRIDS = [(4, 3), (8, 6), (16, 12)]
ART_WEBP_QUALITY = 55  # q40 shows chroma blocking at 16x12; +q costs single-digit bytes here

ART_RECTS = {
    "old": (0.10, 0.105, 0.90, 0.525),
    "modern": (0.075, 0.11, 0.925, 0.545),
    "m15": (0.06, 0.105, 0.94, 0.56),
    "borderless": (0.02, 0.095, 0.98, 0.555),  # art to edges below the opaque title bar
    "fullart": (0.045, 0.045, 0.955, 0.82),   # strip layout (full-art basics)
}
# gens whose border ring is real border ink -> ring-trimmed template + background-color
RING_TRIMMED = {"old", "modern", "m15"}
GEN_ORDER = ["old", "modern", "m15", "borderless", "fullart"]
COLOR_ORDER = ["w", "u", "b", "r", "g", "gold", "artifact", "land"]
DEFAULT_BORDER = "#0d0d0d"
WB_BORDER = "#e8e0d0"
COLOR_TITLES = {
    "w": "White", "u": "Blue", "b": "Black", "r": "Red", "g": "Green",
    "gold": "Gold", "artifact": "Artifact", "land": "Land",
}
GEN_TITLES = {
    "old": "old frame (93/97 trim constants, shared template)",
    "modern": "modern frame (2003-14)",
    "m15": "M15 frame (2015+)",
    "borderless": "borderless (art to edges, real text box)",
    "fullart": "full-art strip (basics)",
}
LUM = np.array([0.299, 0.587, 0.114])

_FRAME_GROUPS = {"1993": "old", "1997": "old", "2003": "modern"}


def gen_for(meta: dict) -> str:
    """Frame generation from real card metadata — mirrors production bucket_for.

    Scryfall's full_art flag covers both strip-layout basics and borderless cards
    that keep a text box (e.g. DMU 'inverted' lands), so nonbasics go to the
    borderless bucket and only basic lands get the strip template. White border is
    NOT a generation — it's the `.ph-wb` modifier over the same bucket.
    """
    if meta.get("full_art") or meta.get("border_color") == "borderless":
        return "fullart" if "Basic" in (meta.get("type_line") or "") else "borderless"
    return _FRAME_GROUPS.get(meta.get("frame") or "", "m15")


def era_for(meta: dict) -> str | None:
    """'93'/'97' for old-frame cards (selects the trim-constant rule), else None."""
    return {"1993": "93", "1997": "97"}.get(meta.get("frame") or "")


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
    """(art_mask, tint_mask, left_mask, right_mask, box_interior).

    tint_mask (template normalization) is frame minus border minus art. The left/right
    tint masks are stricter: only the text-box band below the art window, which is
    guaranteed art-free even if the art rect slightly underestimates the real window
    (art bleed into the side rails was painting phantom gradients on symmetric cards),
    and is where dual-frame gradients actually live. Transition band excluded as before.
    """
    x0, y0, x1, y1 = ART_RECTS[gen]
    art = np.zeros((TPL_H, TPL_W), dtype=bool)
    art[round(y0 * TPL_H):round(y1 * TPL_H), round(x0 * TPL_W):round(x1 * TPL_W)] = True
    inner = inner_mask(gen)
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
    box_inset = 0.0 if gen == "borderless" else BORDER_INSETS[gen][0] + 0.03
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
    """Rail tint = least squares template*tint ~= card, over CHROMA-SELECTED pixels.

    The full-mask solve averaged rail color with black text, bevel shadows, and
    parchment margins — red M15 rails measured as muddy #a98276. The rail's color
    identity lives in its saturated pixels: keep the top-chroma quartile (text and
    shadows are near-achromatic and drop out), then solve the usual LSQ on those,
    which stays self-consistent with the multiply render.
    """
    px = card[mask]
    tl = tpl_lum[mask]
    chroma = px.max(axis=1) - px.min(axis=1)
    sel = chroma >= np.percentile(chroma, 75)
    t = tl[sel] / 255.0
    c = px[sel]
    return np.clip((t[:, None] * c).sum(axis=0) / max(float((t * t).sum()), 1e-6), 0, 255)


def fit_trim_model(T: np.ndarray, B: np.ndarray) -> tuple[float, str]:
    """(p, base) for trim ~= p*box + (1-p)*base, least squares over side-wise obs.

    p is fitted on CHROMA (each observation's mean brightness removed). Scan
    brightness is common-mode — bright scans brighten box AND trim — and a raw
    RGB fit reads that as "box predicts trim", pinning p at the clamp; the trim
    then renders as the box color (red M15 frames came out parchment-pink, gold
    trim came out #fff600-corrected pink). Chroma-p asks the real question: does
    box HUE predict trim HUE? Yes for dual-land gradients (rails mirror the box),
    no for red/gold frames — there base carries the rail color as a constant.
    """
    Tc = T - T.mean(axis=1, keepdims=True)
    Bc = B - B.mean(axis=1, keepdims=True)
    Tc, Bc = Tc - Tc.mean(axis=0), Bc - Bc.mean(axis=0)
    # Ridge shrinkage: when the box barely varies across the bucket (all red-M15
    # boxes are the same parchment) the raw slope divides by ~zero variance and
    # pins the clamp; shrinking toward 0 makes "no box signal" mean "trim is a
    # bucket constant" (carried by base), which is the correct degenerate case.
    lam = 6.0  # box-chroma std (luminance units) below which p shrinks hard
    p = float(np.clip((Bc * Tc).sum() / ((Bc * Bc).sum() + Bc.size * lam * lam), 0.05, 0.95))
    # Gamut cap: base = (T̄ - p·B̄)/(1-p) must be a real color. When trim and box
    # hues differ (gold rails, gray-tinted box), a large p forces a channel of base
    # negative — it clips and the render collapses to the box color (gold-modern
    # emitted base #720000 and rendered silver). The largest in-gamut p is the
    # per-channel ratio min(T̄/B̄); hue-tracking buckets (dual lands) have ratios
    # near 1 so their high p survives.
    p_max = float(np.clip((T.mean(axis=0) / np.maximum(B.mean(axis=0), 1e-6)).min(), 0.05, 0.95))
    p = min(p, p_max)
    base = hexify(np.clip((T.mean(axis=0) - p * B.mean(axis=0)) / (1 - p), 0, 255))
    return p, base


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


INK_THRESH = 18  # luminance deviation from a card's own local blur that marks ink
MIN_VOTE_FRAC = 0.25  # below this vote share a pixel is "aligned text" -> smoothed fallback


def ink_vote_lum(keys: list[str], tint_mask: np.ndarray) -> np.ndarray:
    """Bucket template luminance via ink-rejecting per-pixel vote.

    Text ghosts can't be averaged away — glyph rows align across cards, so any
    all-cards estimator (mean, median, trimmed mean) converges to smudged
    pseudo-text. Instead each card ABSTAINS wherever it deviates sharply from its
    own local blur (ink, pips, collector line — high-frequency of either polarity),
    and the clean cards vote. Pixels where ink aligns on nearly every card (title
    bar, basic lands' big rules text) have no quorum and fall back to a blurred
    trimmed mean: real shading survives, glyphs don't, and no positional text
    masks are needed. Trimmed fallback also keeps a small clique of odd scans from
    dragging low-quorum pixels (split-half measured vs mean/median).
    """
    stack = np.stack([arr @ LUM for arr in map(load_arr, keys) if arr is not None])
    votes = np.empty(stack.shape, dtype=bool)
    for i, card in enumerate(stack):
        local = np.asarray(
            Image.fromarray(card.astype(np.uint8)).filter(ImageFilter.GaussianBlur(2.5)), dtype=np.float32
        )
        votes[i] = np.abs(card - local) < INK_THRESH
    n_votes = votes.sum(axis=0)
    lum = np.divide((stack * votes).sum(axis=0), np.maximum(n_votes, 1))
    thin = n_votes < MIN_VOTE_FRAC * len(stack)

    # No-quorum pixels split two ways. Where the cards AGREE (low cross-card spread:
    # box outlines, frame bevels — real structure every card abstains on), the
    # trimmed mean is accurate and stays crisp. Where they DISAGREE (aligned text
    # rows: every card has ink, each card's glyphs differ), no estimator over the
    # stack is ghost-free, so hole-fill from the vote result's clean neighbors
    # (normalized convolution) — blurring the trimmed mean here just smudges the
    # ghost instead of removing it.
    k = int(len(stack) * TRIM_FRAC)
    tstack = np.sort(stack, axis=0)[k:-k] if k else stack
    trimmed, tspread = tstack.mean(axis=0), tstack.std(axis=0)
    consistent = thin & (tspread < 12)
    lum[consistent] = trimmed[consistent]
    holes = thin & ~consistent
    if holes.any():
        w = (~holes).astype(np.float64)
        blur = ImageFilter.GaussianBlur(4)
        num = np.asarray(Image.fromarray((lum * w).astype(np.uint8)).filter(blur), dtype=np.float64)
        den = np.asarray(Image.fromarray((w * 255).astype(np.uint8)).filter(blur), dtype=np.float64) / 255.0
        lum[holes] = (num / np.maximum(den, 1e-3))[holes]
    return np.clip(lum * (235.0 / max(float(lum[tint_mask].mean()), 1.0)), 0, 255)


def template_uri(lum: np.ndarray, art_mask: np.ndarray, gen: str) -> str:
    """Grayscale template with transparent art window; ring-trimmed gens also get a
    transparent border ring (background-color paints the border: black or `.ph-wb`)."""
    alpha = np.full((TPL_H, TPL_W), 255, dtype=np.uint8)
    alpha[art_mask] = 0
    lum = lum.copy()
    lum[art_mask] = 128
    if gen in RING_TRIMMED:
        inner = inner_mask(gen)
        alpha[~inner] = 0
        lum[~inner] = 128  # flatten hidden pixels for the encoder
    alpha_img = Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(1.2))
    tpl_img = Image.merge("LA", (Image.fromarray(lum.astype(np.uint8)), alpha_img)).convert("RGBA")
    buf = io.BytesIO()
    tpl_img.save(buf, "WEBP", quality=55, method=6)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()


def trim_grad(p: float, base: str) -> str:
    pm = round(p * 100)
    return (
        f"linear-gradient(90deg,color-mix(in srgb,var(--frame-l) {pm}%,{base}) 0 40%,"
        f"color-mix(in srgb,var(--frame-r) {pm}%,{base}) 60% 100%)"
    )


def main() -> None:
    corpus = json.loads((HERE / "frame_corpus.json").read_text())
    corpus_meta = json.loads((HERE / "corpus_meta.json").read_text())

    keys_by_bucket = {}
    for key, info in corpus.items():
        meta = corpus_meta.get(key)
        if meta is None or not (HERE / "images" / f"{key}.webp").exists():
            continue
        gen = gen_for(meta)
        keys_by_bucket.setdefault((info["color"], gen), []).append(key)

    sections, tpl_css = [], []
    HDRS_TOKEN = "<!--STAGE-HDRS-->"
    uri_chars = {g: [] for g in ART_GRIDS}

    for color in COLOR_ORDER:
        for gen in GEN_ORDER:
            keys = keys_by_bucket.get((color, gen), [])
            if len(keys) < 15:
                continue
            art_mask, tint_mask, left_mask, right_mask, box_mask = masks_for(gen)
            lum = ink_vote_lum(keys, tint_mask)
            # Residual low-frequency mottle in the box (scan glare, shading around
            # text) survives the ink vote — it isn't high-frequency, so nobody
            # abstains on it. The box carries no template signal beyond its average
            # shading, so smooth it box-pixels-only (normalized convolution keeps
            # the outline from bleeding in).
            wb_ = box_mask.astype(np.float64)
            blur6 = ImageFilter.GaussianBlur(6)
            num = np.asarray(Image.fromarray((lum * wb_).astype(np.uint8)).filter(blur6), dtype=np.float64)
            den = np.asarray(Image.fromarray((wb_ * 255).astype(np.uint8)).filter(blur6), dtype=np.float64) / 255.0
            lum[box_mask] = (num / np.maximum(den, 1e-3))[box_mask]
            # Per-REGION normalization: multiply can only darken, so a region's render
            # can never exceed its template luminance — dark template rails were
            # capping saturated rail colors (m15 red rendered washed-out pink, the
            # 'saturation ceiling' residual). Each region has its own color layer, so
            # absolute levels belong to the colors; the template keeps only
            # within-region texture, normalized bright for headroom.
            for region in (tint_mask & ~box_mask, box_mask):
                lum[region] = np.clip(lum[region] * (235.0 / max(float(lum[region].mean()), 1.0)), 0, 255)
            tpl_uri = template_uri(lum, art_mask, gen)

            # Group members for the trim fit: old splits by era (the measured ±5-16
            # signed residual lives in the constants), everything else fits once.
            wb_keys = {k for k in keys if corpus_meta[k].get("border_color") == "white"}
            if gen == "old":
                era_groups = {e: [k for k in keys if era_for(corpus_meta[k]) == e] for e in ("93", "97")}
                era_groups = {e: g for e, g in era_groups.items() if len(g) >= 15}
            else:
                era_groups = {None: keys}

            mid = TPL_W // 2
            cols_idx = np.arange(TPL_W)[None, :]
            box_l_mask, box_r_mask = box_mask & (cols_idx < mid), box_mask & (cols_idx >= mid)

            # Fit on PER-SIDE observations — (left trim, left box), (right trim, right box).
            # Per-card averages cancel the dual-land gradient out of both variables and
            # made the fitted mix ratio underestimate two-color boxes.
            era_css = []
            all_boxes = []
            for era, members in sorted(era_groups.items(), key=lambda kv: kv[0] or ""):
                fit_keys = members[:: max(1, len(members) // FIT_PER_ERA)]
                tints, boxes = [], []
                for key in fit_keys:
                    card = load_arr(key)
                    if card is None:
                        continue
                    tints.append(solve_tint(card, lum, left_mask))
                    boxes.append(card[box_l_mask].reshape(-1, 3).mean(axis=0))
                    tints.append(solve_tint(card, lum, right_mask))
                    boxes.append(card[box_r_mask].reshape(-1, 3).mean(axis=0))
                p, base = fit_trim_model(np.array(tints), np.array(boxes))
                all_boxes += boxes
                era_css.append((era, trim_grad(p, base)))

            # bucket-default per-card colors: median box halves + art over a sample
            samples = rng.sample(keys, min(3 * SAMPLES_PER_BUCKET, len(keys)))
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
            # trim layer fills the inner rect (per-gen, per-side border insets)
            il, it, ir, ib = BORDER_INSETS[gen]
            fw_w, fw_h = (1 - il - ir) * 100, (1 - it - ib) * 100
            fpx = il / max(il + ir, 1e-6) * 100
            fpy = it / max(it + ib, 1e-6) * 100
            # box layer geometry (matches the box-interior measurement rect)
            by0, by1 = y1 + 0.065, 0.90
            bxf = il + 0.03
            bw, bh = (1 - 2 * bxf) * 100, (by1 - by0) * 100
            bpy = by0 / (1 - (by1 - by0)) * 100
            box_grad = "linear-gradient(90deg,var(--frame-l) 0 40%,var(--frame-r) 60% 100%)"

            # one template rule shared by all era classes; era rules override --trim only
            classes = {era: (f"b-{color}-o{era}" if era else f"b-{color}-{gen}") for era, _ in era_css}
            selector = ",".join(f".{c}" for c in classes.values())
            tpl_css.append(
                f"{selector}{{--tpl:url({tpl_uri});"
                f"--frame-l:{d_fl};--frame-r:{d_fr};--art-layer:linear-gradient({d_ac},{d_ac});"
                f"--box-layer:{box_grad};"
                f"background-color:{DEFAULT_BORDER};"
                f"background-size:100% 100%,{aw:.1f}% {ah:.1f}%,{bw:.1f}% {bh:.1f}%,{fw_w:.1f}% {fw_h:.1f}%;"
                f"background-position:center,{px:.1f}% {py:.1f}%,50% {bpy:.1f}%,{fpx:.1f}% {fpy:.1f}%}}"
            )
            for era, grad in era_css:
                tpl_css.append(f".{classes[era]}{{--trim:{grad}}}")

            def row_class(key: str, era_to_class=classes, wb=wb_keys, meta=corpus_meta) -> str:
                era = era_for(meta[key]) if len(era_to_class) > 1 else None
                cls = era_to_class.get(era) or next(iter(era_to_class.values()))
                return f"{cls} ph-wb" if key in wb else cls

            # stratified sample rows: cover each era and include a wb example when present
            def pick_rows() -> list[str]:
                chosen, seen = [], set()
                pools = [[k for k, *_ in measured if era_for(corpus_meta[k]) == e or e is None] for e, _ in era_css]
                want_wb = [k for k, *_ in measured if k in wb_keys][:1]
                for pool in pools:
                    for k in pool[: SAMPLES_PER_BUCKET // len(pools)]:
                        chosen.append(k)
                        seen.add(k)
                for k in want_wb + [k for k, *_ in measured]:
                    if len(chosen) >= SAMPLES_PER_BUCKET:
                        break
                    if k not in seen:
                        chosen.append(k)
                        seen.add(k)
                return chosen[:SAMPLES_PER_BUCKET]

            by_key = {k: (fl, fr, ac) for k, fl, fr, ac in measured}
            rows = []
            for key in pick_rows():
                fl, fr, ac = by_key[key]
                full = load_full(key)
                set_code, cn = key.split("__", 1)
                cls = row_class(key)
                tag = " · wb" if key in wb_keys else ""
                era = era_for(corpus_meta[key])
                tag += f" · {era}" if era and gen == "old" else ""
                tint = f"--frame-l:{fl};--frame-r:{fr}"
                cells = [
                    f'<div class="rowcap">{set_code}<br>#{cn}{tag}</div>',
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
            n93 = sum(1 for k in keys if era_for(corpus_meta[k]) == "93")
            meta_bits = [f"n={len(keys)}"]
            if gen == "old":
                meta_bits.append(f"93/97={n93}/{len(keys) - n93}")
            if wb_keys:
                meta_bits.append(f"wb={len(wb_keys)}")
            sections.append(
                f'<section><h2>{COLOR_TITLES[color]} — {GEN_TITLES[gen]} '
                f'<span class="meta">{" · ".join(meta_bits)}</span></h2><div class="stage-grid">'
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
    background-image: var(--tpl), var(--art-layer), var(--box-layer), var(--trim);
    background-repeat: no-repeat;
    background-blend-mode: multiply, normal, normal, normal;
  }}
  .ph-wb {{ background-color: {WB_BORDER} !important; }}
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
The art layer is one CSS var; each stage only changes its value. White-border rows use the shared
black-border template + the `.ph-wb` background-color modifier; old-frame rows use per-era trim constants.</p>
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
    n_uris = sum(1 for r in tpl_css if "--tpl:url(" in r)
    print(f"wrote {OUT / 'template-mockup.html'} ({n_uris} templates, {len(tpl_css)} rules); {stats}")


if __name__ == "__main__":
    main()
