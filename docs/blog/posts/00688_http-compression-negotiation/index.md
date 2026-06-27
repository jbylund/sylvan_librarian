---
title: "We Ignore Your Accept-Encoding q= Weights and Serve Better Compression Anyway"
date: 2027-04-24
publishDate: 2027-04-24
tags: ["python", "http", "compression", "falcon", "middleware"]
summary: "Serving compressed HTTP responses correctly is more nuanced than calling gzip.compress(). This post walks through the CompressionMiddleware in Arcane Tutor: Accept-Encoding negotiation, server-side algorithm priority, two distinct code paths for buffered versus streaming responses, the 200-byte skip threshold, Vary headers, and gzip mtime=0 for byte-identical repeated responses."
---

The first time I measured a search response, the raw JSON was 76 KB for a 100-card result set. After compression, it was 902 bytes — an 84× reduction. The compression itself took 0.01 ms. At that point, the question stopped being "should we compress?" and became "how do we do it correctly?"

Doing it correctly turns out to involve more moving parts than `gzip.compress(data)`.

## What 76 KB of Card JSON Looks Like

A search for `color:red format:modern` returns up to 100 cards, each with a scryfall ID, name, type line, oracle text, colors, legalities, mana cost breakdown, image URI, set, rarity, and artist. That is deeply repetitive JSON: the same field names on every card, the same legality object keys, the same color symbols. Repetition is what compression algorithms exploit.

The numbers, measured on a fixed JSON fixture shaped like a real `color:red format:modern` response (100 cards, all fields, 76 KB uncompressed), 100 serial single-threaded iterations on an M5 Max, Python 3.13, brotli 1.2.0, zstandard 0.25.0:

| Algorithm | Compressed size | Ratio | Median latency | P99 latency |
|-----------|----------------|-------|---------------|-------------|
| gzip (level 6) | 1,389 bytes | 55× | 0.119 ms | 0.188 ms |
| brotli (quality 4) | 857 bytes | 89× | 0.058 ms | 0.173 ms |
| zstd (level 4) | 902 bytes | 85× | 0.010 ms | 0.094 ms |

Zstd at quality 4 is nearly as compact as brotli while compressing 6× faster. That table shaped the priority order.

## How Accept-Encoding Negotiation Works (and Where It Gets Complicated)

The browser sends something like:

```
Accept-Encoding: br;q=1.0, gzip;q=0.8, *;q=0.1
```

RFC 7231 defines `q=` as a preference weight from 0.0 to 1.0. The naive reading is: the server picks the algorithm the client prefers most. Chrome, for example, consistently advertises `br` at `q=1.0` — so brotli should always win.

Except that `q=1.0` on `br` and `q=0.8` on `gzip` does not mean brotli is 25% better. The `q=` system was designed for language negotiation (`Accept-Language: en;q=1.0, fr;q=0.5`), where client preference is the dominant signal. For compression, what actually matters is the intersection of "what the client supports" and "what gives the best tradeoff on this payload."

The initial implementation of `_get_compressor` respected client `q=` weights. The [cleanup commit](https://github.com/jbylund/arcane_tutor/commit/210fa16) stripped that out entirely. The current code parses Accept-Encoding only to build a candidate list — it does not read `q=` values at all:

```python
# Simplified from api/middlewares/compression/compression_mod.py
compressor_candidates = []
for accept_encoding_item in accept_encoding_header.split(","):
    name, _, _ = accept_encoding_item.partition(";")  # strip q= and any other params
    name = name.strip().lower()
    compressor = self._compressors.get(name)
    if compressor is None:
        continue
    compressor_candidates.append(compressor)
compressor = min(compressor_candidates, key=lambda v: v.priority) if compressor_candidates else None
```

([Full source](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/middlewares/compression/compression_mod.py#L54-L81))

The `priority` attribute on each compressor sets the server order: zstd=10, brotli=20, gzip=30. Lower wins. If zstd is in the Accept-Encoding at any weight above 0.0, the server picks zstd. If only gzip and brotli are present, brotli wins.

The case against honoring `q=`: Chrome advertises `br;q=1.0` not because brotli is better for this specific payload but because brotli has historically been Chrome's preferred algorithm. If a future Chrome version adjusts those weights for reasons unrelated to JSON performance, the server's behavior changes for no reason the server controls. Letting the server pick based on measured performance characteristics is more stable.

The caveat: a client that genuinely cannot handle a specific algorithm should send `q=0.0` to exclude it. The current code strips `q=` before checking, so it would try to use that algorithm anyway. That is a real gap — but Chrome, Firefox, and Safari do not send `q=0.0` for any algorithm they advertise, so in practice the audience for this application never triggers it. A server-side proxy or API client that sends a narrow Accept-Encoding would be the case to watch.

## Two Code Paths: Buffered and Streaming

The Falcon `Response` object holds the body in one of two states: either as a fully rendered buffer (`resp.data` or `resp.text`), or as a generator attached to `resp.stream` for chunked transfer. Compression has to handle both.

For buffered responses:

```python
data = resp.render_body()
if data is None or len(data) < MIN_SIZE:
    return  # skip compression
resp.data = compressor.compress(data)
resp.text = None  # clear text field so Falcon uses resp.data
resp.content_length = None  # Falcon recomputes after we set resp.data
```

For streaming responses:

```python
resp.stream = compressor.compress_stream(resp.stream)
resp.content_length = None  # cannot know final size before compressing
```

The streaming path wraps the original generator in a new generator that feeds chunks through the compressor and yields compressed output. The gzip streaming implementation uses a `StreamingBuffer` — a `BytesIO` subclass that clears itself after each read — to avoid accumulating the full compressed output in memory before yielding:

```python
# api/middlewares/compression/compressors/util.py
class StreamingBuffer(BytesIO):
    def read(self):
        ret = self.getvalue()
        self.seek(0)
        self.truncate()
        return ret
```

([Source](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/middlewares/compression/compressors/util.py#L12-L24))

The zstd and gzip streaming paths both use `StreamingBuffer` as a sink for the compressor, drain it after each input chunk, and yield whatever came out. Brotli's streaming compressor has its own incremental API (`compressor.process()` / `compressor.finish()`) that does not need the buffer workaround.

## Why Short Responses Skip Compression

Not every response is worth compressing. The buffered path has an explicit guard:

```python
MIN_SIZE: int = 200

if data is None or len(data) < MIN_SIZE:
    return
```

([Source](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/middlewares/compression/compression_mod.py#L15))

A 200-byte JSON error response (`{"title": "Not Found", "status": 404}`) would compress to maybe 120 bytes — a saving of 80 bytes at a cost of 0.01 ms of CPU and a round-trip of Content-Encoding headers. The CDN and the browser both pay a decompression cost on the other end. For responses that small, the overhead dominates the saving.

The threshold is blunt. It does not distinguish between a 200-byte response that compresses well and one that does not. For this application, JSON responses below 200 bytes are all error payloads that would not benefit much regardless, so the approximation holds.

## Why the Vary Header Is Not Optional

After setting `Content-Encoding`, the middleware appends:

```python
resp.append_header("Vary", "Accept-Encoding")
```

This is required for correct CDN and proxy behavior. Without it, a cache sitting between the server and the client might store the zstd-encoded response and serve it to a client that only sent `gzip` in Accept-Encoding, producing a decompression error.

The `Vary` header tells any intermediate cache that the response varies by the value of Accept-Encoding — cache entries keyed only on the URL are not safe to reuse across clients with different encoding support.

The application-layer cache in `CachingMiddleware` already includes `Accept-Encoding` in its cache key, so the application cache handles this correctly. The `Vary` header extends that guarantee to any CDN or reverse proxy in front of the application.

## mtime=0 for Gzip Determinism

Gzip files embed a modification timestamp in the header. By default, `gzip.compress()` in Python 3 includes `mtime=None`, which encodes the current wall clock time. Two calls to `gzip.compress(data)` one second apart produce different bytes, even when `data` is identical.

That matters because the caching middleware stores the rendered response bytes. If a cache miss triggers a fresh compression of the same payload, the resulting bytes differ from the previously cached bytes — which is harmless for the client (both decompress identically) but breaks any byte-level cache deduplication or content-hash validation that a downstream system might do.

Setting `mtime=0` makes the gzip output deterministic:

```python
# api/middlewares/compression/compressors/gzip.py
return gzip.compress(data, compresslevel=self.compression_level, mtime=0)
```

Zstd and brotli do not have this problem — their formats do not embed a timestamp.

## Where Compression Fits in the Stack

The middleware order in `api_worker.py`:

```python
middleware=[
    TimingMiddleware(),
    QueryLogMiddleware(),
    CachingMiddleware(cache=shared_cache),
    CompressionMiddleware(),
    SecurityHeadersMiddleware(),
    CORSMiddleware(),
]
```

In Falcon, `process_response` methods run in reverse registration order: CORSMiddleware runs first on the way out, then SecurityHeaders, then CompressionMiddleware, then CachingMiddleware. This means the cache stores the already-compressed bytes for each distinct Accept-Encoding value. A cache hit bypasses CompressionMiddleware entirely — `resp.complete = True` [causes the compression step to early-return](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/middlewares/compression/compression_mod.py#L99-L101) — so the CPU cost of compression is paid at most once per unique (URL, Accept-Encoding) pair.

The practical result: for cached responses, compression cost is zero. For uncached responses, zstd costs about 0.01 ms at quality 4 — that 0.01 ms reflects a warm Python process on an M5 Max at low concurrency, and the compression middleware logs timing for every non-cached response if it ever becomes the constraint. A typical database query for this application takes 1–5 ms. Compression is not the bottleneck.

Compression does not apply when `Content-Encoding` is already set (images arrive pre-compressed), when Accept-Encoding is absent, or when no supported algorithm appears in the header. Those cases all short-circuit before the compressor is selected.

The 76 KB payload compresses to under 1 KB. The client downloads 99% less data, and the server spends 0.01 ms doing it.
