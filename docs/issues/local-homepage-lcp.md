# Homepage LCP: SSR the grid, preload the first image

**Target metric:** LCP (lab 3.1s local Lighthouse mobile sim / PSI ~similar; FCP is
already at the 1.8s "good" threshold and is ~600ms handshake physics — see analysis
in this doc's History section). TBT is 0ms; Speed Index rides along with LCP.

## The chain today (measured, local Lighthouse mobile sim 2026-07-06)

```
0ms    HTML (4-5KB, TTFB 10ms, done ~47ms)
52ms   app.min.js downloads (deferred)         ← card work can't start yet
64ms   /random_search?num_cards=12&shape=columnar (JS-initiated)
81ms   first card image requests begin         ← LCP clock effectively starts here
~300ms LCP image complete (real); ~3.1s simulated
```

Every hop is re-priced by the simulator (150ms RTT, 1.6Mbps). The JS-boot + API
round trip in front of the images is the recoverable budget: ~0.8–1.2s simulated.

## Plan

1. **SSR the homepage grid** through the existing noscript renderer (the
   `<!-- SERVER_SIDE_RESULTS -->` splice anchor on `/` is currently unfilled).
   Cards in HTML mean the preload scanner discovers image URLs at parse time —
   no JS boot, no API round trip before the fetch starts.
2. **Caching resolution — don't give it up:** random content doesn't need
   per-visitor uniqueness. Cache the SSR'd homepage with a short TTL (30–60s);
   everyone in a window shares one draw. Engine random sampling is ~1–2ms, so even
   uncached would be fine at origin; TTL is load insurance. Side benefit: a shared
   window makes the same 12 images CDN-hot for every visitor after the first —
   the homepage LCP image is ~never a cold CloudFront miss.
3. **Preload the LCP candidate:** `<link rel=preload as=image imagesrcset=...>`
   for the first-row images (or at minimum the first card), `fetchpriority=high`
   already exists on first-row `<img>`s.
4. **Mind the bandwidth budget:** 12 × ~18KB webp ≈ 216KB ≈ ~1.1s at simulated
   1.6Mbps. LCP needs only the first image — priority hints matter more than
   count, but consider whether 12 above-the-fold-eager images is right for mobile.

Expected: LCP 3.1s → ~1.9–2.2s simulated. FCP regresses slightly (+15–30KB HTML ≈
+100–150ms transfer) — accepted trade, documented here so nobody "fixes" it back.

## Image bytes: quality ladder + fidelity budget (designed 2026-07-06)

Today: WEBP_QUALITY=75 flat across all four sizes, and real phones (DPR 2.5–3)
fetch 745s for grid cells (~133KB/card) because srcset maximizes fidelity within
the offered candidates. Two multiplying changes, validated visually:

**Quality ladder** (native-1:1 crop sheets + hover-compare demo in
`ignored/placeholder-prototype/{quality_sheet,native_quality_sheet}.png` and
`quality-demo/index.html`, built from worst-compressing card per color group):
280 q30 (11KB) · 388 q40 (21KB) · 538 q45 (~48KB) · 745 q60 (125KB — modal is the
only true 1:1 surface, keep it high). Re-encode from existing S3 745s (add a mode
to copy_images_to_s3.py; avoids a Scryfall recrawl); needs a CloudFront
invalidation story (immutable cache).

**Fidelity budget via srcset, not against it**: effective-DPR cap ~1.5 expressed
as resolution-conditional `sizes` clauses (5 layout × 3 density tiers = 15
clauses, one shared generated string) — keeps all 4 candidates so huge slots
still earn 745 (crossover ≈ 495 CSS px); phones move 745→538 (−64% grid bytes
with quality stacked). A flat "DPR cap 2" would NOT move phones off 745
(350px × 2 = 700 → still rounds up to 745); ~1.5 is the working constant.

**sizes="auto" hybrid**: auto (needs loading=lazy) = Chrome/Edge 126+, FF 150+,
Safari NOT shipped as of 2026-07 (BCD lists Safari 27 = announced, GA ~fall
2026; caniuse ~71% global). Plan: eager LCP row = full candidates + budgeted
15-clause sizes (auto can never apply there); lazy below-fold = candidates
capped at 538 + `sizes="auto, <explicit clauses>"`. Safari fallback: per-entry
parse-error skipping drops `auto` and uses the explicit clauses = today's exact
behavior; self-upgrading when Safari 27 adopts. Verify per-entry skipping in
Safari via the lab (worst case would be whole-attribute invalid → 100vw → 745s).
**Hard requirement (reproduced in the lab):** `sizes="auto"` images MUST have
explicit dimensions (width/height attrs or CSS `aspect-ratio`) — the spec makes
their intrinsic size unavailable for layout to break circular sizing, so without
it the img renders at the 300×150 default object size (squashed 2:1 cards).
Production `.card-image` already sets `aspect-ratio: 745/1041`, satisfying this;
keep it true anywhere auto is added.
**Second hard finding (lab discriminator probes, Chrome, 2026-07-06):** `auto` on
an EAGER image does NOT fall through to the fallback clauses — Chrome treats it
as a 100vw guess and fetches the LARGEST candidate in the menu (preload scanner
must pick before layout exists). So never ship `auto` on the eager/LCP row: it
is worst-case overfetch, not "today's behavior". And `auto` cannot be combined
with budget clauses (it overrides them in supporting browsers), so it's auto XOR
budget per image — and auto alone keeps phones on 745 (layout-accurate ≠ thrifty).
**Decision (2026-07-06): ideal behavior via the 15-clause budget, no `auto`** —
phones land on 538/388, low-DPR giant displays still earn 745, full menu kept.
Shipped as PR #616: single source of truth `api/static/card_grid_sizes.json`
(5 layout × 3 density tables); noscript_helpers.py generates from it, app.js
holds a mirrored copy with jest asserting equality, and test_noscript_parity.py
checks the layout table against the real grid breakpoints in styles.css.

**Testing workflow**: `ignored/placeholder-prototype/srcset-lab.html` — live
`currentSrc` probes for current/budgeted/auto sizes (use DevTools DPR emulation
+ real browsers), and press-and-hold 538 q45 vs 745 q75 pairs at grid-slot size
(judge on a real phone via `python3 -m http.server`).

Bytes summary: quality alone ≈ −18% fleet; + candidate/budget cap ≈ −64% mobile
grid; staged 280-first makes LCP-critical bytes −92% (total/card ~59KB).

## Explicitly not FCP levers (measured/argued 2026-07-05/06)

- CDN-fronting the HTML: simulator charges flat RTT; helps real users/CrUX only
- HTTP/3: alt-svc discovery means cold runs still handshake h2
- Placeholders: Chrome excludes low-entropy paints from FCP/LCP by design —
  placeholder work (PRs #607/#608/#613) targets Speed Index / perceived quality
- Comment stripping / catalog sort+defer (PR #614): bytes and chains, not FCP;
  the catalog defer does free ~9KB of critical-window bandwidth which helps LCP
  marginally
- Remaining FCP budget is pre-paint CPU only (~100–300ms simulated): slimmer
  critical CSS, less top-level work in app.min.js eval — optional polish

## History / evidence

- FCP decomposition: ~600ms DNS+TCP+TLS (irreducible cold-visit), ~50ms
  request+doc (TTFB 10ms, 4-5KB doc), remainder = 4×-throttled pre-paint CPU
  (Script Eval 58ms, Style & Layout 57ms real). Main-thread and waterfall from
  local `npx lighthouse` run; PSI API was quota-blocked.
- `/get_catalog` was the largest critical-window resource (8.9KB br > app.min.js
  7.6KB); PR #614 defers it to idle/focus and sorts its keys (−5.6% payload).
- Lab vs field: rankings use CrUX field data; lab-invisible improvements (edge
  HTML, h3) still help real users.
