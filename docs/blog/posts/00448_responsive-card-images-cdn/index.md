---
title: "Keeping a CDN in Sync with Scryfall Using Set-Based Diffing"
date: 2027-01-02
publishDate: 2027-01-02
tags: ["frontend", "cdn", "images", "javascript"]
summary: "Serving card images via CloudFront with responsive srcset, native lazy loading, and a sync script that uses Python set subtraction to find exactly which images are missing."
---

Serving card images efficiently has two halves: getting the right files into S3 before they are needed, and letting the browser request the right size without JavaScript help.
This post covers how both halves evolved — and how a Python set subtraction replaced a tangled loop.

The first image delivery approach was the simplest one possible: build a Scryfall URL directly from the card's UUID and let Scryfall's own CDN do the work.

```javascript
// original approach — just ask Scryfall
buildImageUrl(image_location_uuid, size, face_index = 0) {
  const idPrefix = image_location_uuid.substring(0, 1);
  const idSuffix = image_location_uuid.substring(1, 2);
  const cache_timestamp = Math.floor(Date.now() / 1000 / (3 * 86400)) * (3 * 86400);
  const facePath = face_index === 0 ? 'front' : 'back';
  return `https://cards.scryfall.io/${size}/${facePath}/${idPrefix}/${idSuffix}/${image_location_uuid}.jpg?${cache_timestamp}`;
}
```

That worked fine at first.
But it meant every image request left the application's control: Scryfall's rate limits applied, the cache invalidation was opaque, and the URL shape was coupled to Scryfall's internal UUID structure.
When [PR #264](https://github.com/jbylund/arcane_tutor/pull/264) switched to a self-hosted CloudFront distribution, that whole function collapsed to three lines:

```javascript
buildImageUrl(card, size) {
  const face = card.face_idx || 1;
  return `https://d1hot9ps2xugbc.cloudfront.net/img/${card.set_code}/${card.collector_number}/${face}/${size}.webp`;
}
```

The URL is now built from card metadata the application already owns — set code, collector number, face index — rather than from an opaque UUID.
The CloudFront distribution caches at the edge; the backend never proxies image bytes.

## Why the Images Are Already There When the CDN Needs Them

A CDN is only as useful as what is behind it.
If a request reaches CloudFront and the image is not in S3, the CDN passes the miss through and returns a 404.
The sync script [`scripts/copy_images_to_s3.py`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/scripts/copy_images_to_s3.py) pre-warms the bucket: it downloads PNG images from Scryfall, converts them to WebP at four widths using `cwebp` at quality 75 with `-sharp_yuv` for sharpness preservation during downscaling, and uploads them to S3 before anyone requests them.
A typical Scryfall PNG runs 130–250 KB at full resolution; the 280px WebP thumbnail comes out to roughly 12–25 KB, and the 745px WebP is around 60–110 KB — a 2–4× file size reduction at every size before CloudFront even adds edge caching.

The initial version of the script ([PR #254](https://github.com/jbylund/arcane_tutor/pull/254)) had a straightforward loop:

```python
for card in db_cards:
    for size in [SMALL_KEY, MEDIUM_KEY, LARGE_KEY]:
        key = (set_code, collector_number, size)
        if key not in s3_cards:
            missing_for_card.append(size)
```

`s3_cards` was a `set[tuple[str, str, str]]`.
Membership testing was O(1), which is fine, but the structure was ad-hoc: raw tuples with no validation and no shared key generation between the DB side and the S3 side.

[PR #438](https://github.com/jbylund/arcane_tutor/pull/438) replaced the tuple approach with a `CardImage` class that implements `__hash__` and `__eq__`.
The missing-image detection became Python set subtraction:

```python
missing_cards = db_cards - s3_cards
```

`db_cards` is the set of `CardImage` objects the database says should exist.
`s3_cards` is the set already in the bucket.
Their difference is exactly what needs to be uploaded.
The class also centralizes key generation in one place:

```python
def get_s3_key(self) -> str:
    return f"img/{self.set_code}/{self.collector_number}/{self.face_idx}/{self.size}.webp"
```

Before this refactor, the key was assembled in three separate places in the script.
That scattered key generation is exactly how the face-index bug below stayed hidden.

The initial S3 path had no face dimension:

```
img/{set_code}/{collector_number}/{size}.webp
```

Double-faced cards — think Delver of Secrets // Insectile Aberration — have two images.
Without a face level in the key, the back face silently overwrote the front face on upload.
[PR #409](https://github.com/jbylund/arcane_tutor/pull/409) added the missing dimension:

```
img/{set_code}/{collector_number}/{face}/{size}.webp
```

Single-faced cards use `face = "1"`.
The database query uses `COALESCE` to handle cards without a stored `face_idx`:

```sql
COALESCE((raw_card_blob->>'face_idx')::int, 1) AS face_idx
```

With the ad-hoc tuple approach, this class of bug was easy to introduce because each place that built a key could diverge independently.
After the `CardImage` refactor, `face_idx` is validated in the constructor and `get_s3_key()` is the single source of truth — an invalid face produces an exception at insertion time rather than a silent wrong key.

## How the Browser Picks the Right Size

With images in CloudFront at four widths, the next question is which size to serve to a given client.
The naive answer is always the largest; the correct answer is the size that matches the slot on screen.

The original JavaScript implementation tried to answer this with an `IntersectionObserver`.
When a card entered the viewport, the observer fired, loaded a small placeholder, then upgraded to a normal-size image once the placeholder had loaded.
The code was about 70 lines.
It also introduced two sequential network round-trips per card — the browser had to receive the small image before it knew to fetch the normal one, so the normal image could not start until the first was done.
With `srcset`, the browser issues a single request for the right size based on the slot geometry it already knows, with no JavaScript in the loop and no intermediate image fetch.

The replacement is three HTML attributes:

```javascript
const srcset = `${image280} 280w, ${image388} 388w, ${image538} 538w, ${image745} 745w`;
const sizes  = '(max-width: 409px) calc(100vw - 3.6em), (max-width: 749px) calc(50vw - 2.6em - 7.5px), (max-width: 1369px) calc(33.33vw - 2.27em - 10px), (max-width: 2499px) calc(25vw - 2.1em - 11.25px), calc(20vw - 2em - 12px)';
const imageHtml = `<img src="${image388}" srcset="${srcset}" sizes="${sizes}" ${fetchPriorityAttr}${loadingAttr} />`;
```

`srcset` tells the browser what files exist and how wide each one is.
`sizes` tells the browser how wide the image slot will be at each breakpoint — which matches the CSS grid layout: one column below 410px, two columns to 750px, three to 1370px, four to 2500px, five beyond that.
The browser picks the smallest candidate whose width is at least as wide as the computed slot.
On a narrow phone the slot is about 260px and the browser picks 280w.
On a 1400px desktop with four columns the slot is about 330px and the browser picks 388w.
On a HiDPI screen at 2× device pixel ratio, the effective slot width doubles: a 330px slot at 2× becomes 660px, so the browser picks 745w instead.
It never downloads the 745px version on a standard desktop unless the card is shown full-width.

This is all native browser behavior.
Deleting the `IntersectionObserver` implementation — 70 lines of JavaScript — and replacing it with `srcset`/`sizes` was [PR #393](https://github.com/jbylund/arcane_tutor/pull/393).

## Lazy Loading Without JavaScript

The other thing the `IntersectionObserver` was doing was deferring the fetch for below-the-fold images.
Native lazy loading handles that too:

```javascript
const loadingAttr = isFirstRow ? '' : ' loading="lazy"';
```

Cards in the first row get `fetchpriority="high"` — the browser treats their images as high-priority fetches, which directly improves Largest Contentful Paint.
Every other card gets `loading="lazy"`.
The browser does not fetch those images until they are near the viewport.
No JavaScript required.

The `isFirstRow` determination uses the same viewport-to-column mapping as the `sizes` attribute: below 410px is one column, 410–750px is two, 750–1370px is three, 1370–2500px is four, above that is five.
A 1280px viewport gets three columns; the first three cards get `fetchpriority="high"`.
A 400px phone gets one column; only the first card gets it.

## What Pre-Warming Cannot Do

The sync script is run manually or on a schedule — it is not triggered by Scryfall bulk data updates.
If Scryfall releases a new set and the database is updated before the script runs, the CDN has a gap: cards exist in the database but their images are not yet in S3.
During that window, the `<img>` elements render with broken images.

One mitigation is to run the sync script as part of the data import pipeline.
That is not currently wired up.
The current architecture tolerates a short gap in exchange for keeping the sync script simple and independently runnable.

The script also does not delete images for cards that are removed from the database.
Orphaned WebP files accumulate in S3.
For a dataset the size of all Magic cards ever printed — roughly 30,000 cards × 4 sizes × 2 faces = ~240,000 files — the storage cost is negligible.
For a faster-moving dataset it would matter.

The `sizes` attribute has a comparable maintenance coupling on the frontend: it encodes the CSS grid breakpoints as hardcoded pixel values.
If the CSS layout changes but `sizes` is not updated, the browser will still select an image — just the wrong one for the slot.
A 410px breakpoint in `sizes` that no longer matches the grid silently serves a 388px image into a 260px slot, or vice versa.
That is not broken; it just wastes bandwidth or looks slightly soft.
There is no automated check that keeps the two in sync.

---

Both halves of the problem turned out to have the same answer: replace ad-hoc bookkeeping with a native primitive.
On the sync side, Python set subtraction replaced a nested loop full of ad-hoc tuples.
On the frontend, `srcset`/`sizes` and `loading="lazy"` replaced 70 lines of JavaScript that approximated what the browser already knew how to do.
