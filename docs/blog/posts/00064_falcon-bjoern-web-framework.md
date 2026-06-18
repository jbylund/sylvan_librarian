---
title: "47× the Throughput of FastAPI: Why I Use Falcon + Bjoern"
date: 2026-07-04
publishDate: 2026-07-04
tags: ["arcane-tutor", "python", "falcon", "bjoern", "fastapi", "performance"]
summary: "Why Arcane Tutor uses Falcon and Bjoern instead of the FastAPI + uvicorn default: a preference for explicit, close-to-vanilla Python over framework magic."
---

Arcane Tutor uses Falcon and Bjoern instead of FastAPI, the most popular Python web framework by a wide margin — 38% adoption in the 2025 JetBrains Python Developer Survey, 88,000 GitHub stars, roughly 474 million PyPI downloads a month.

Benchmarked with `wrk` against a trivial `/ping` endpoint, 4 workers each, 100 concurrent connections:

| | Falcon + Bjoern | FastAPI + Uvicorn |
|---|---|---|
| Req/sec | 479,407 | 10,117 |
| Avg latency | 194µs | 5.14ms |
| Timeouts | 78 | 500 |

47× throughput difference on an endpoint that does nothing —
which reflects the framework overhead, not the application work.
The gap narrows when requests do real work, but the baseline matters more than it might seem:
Arcane Tutor caches search results, so a large fraction of requests are cache hits that return immediately.
The faster the framework processes a hit, the more headroom is left for the requests that actually need the database.

## The Appeal of FastAPI

FastAPI is built around developer experience: declare your types as Python hints and get input validation, serialization, and auto-generated OpenAPI docs for free.
For APIs with many endpoints or complex response schemas, that is a genuine productivity win.

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/ping")
def ping():
    return {"ok": True}
```

## A Different Preference

FastAPI and Pydantic introduce a layer of conventions you have to learn:
how decorators wire up routes, how response models work, when validation fires and when it doesn't,
what happens when you return a dict vs a Pydantic model.
At a previous job, response validation overhead became a real problem at scale —
[fastapi/fastapi#1359](https://github.com/fastapi/fastapi/issues/1359) tells a familiar story.
That experience left me preferring to write something closer to plain Python:
explicit request handling, explicit serialization, no surprises.

[Falcon](https://github.com/falconry/falcon) is a minimalist WSGI framework.
It supports middleware, request body handling, and error serialization —
but none of those come pre-built.
There are no auto-generated OpenAPI docs, no type hint → validation magic,
no bundled CORS or auth or compression middleware.
You wire those up yourself, or you don't.

The difference in stack size reflects the difference in philosophy.
Installing FastAPI (fastapi, pydantic, starlette, uvicorn, anyio) brings in ~94,000 lines of Python
plus pydantic-core, a 4MB compiled Rust extension.
Falcon has zero Python dependencies and ~31,000 lines of source.
Less code running on every request, fewer conventions to know,
and when something is slow the framework is not a suspect.

## Why Async Wasn't Worth It Here

Async could have helped.
psycopg3 supports async, so it would have been possible to overlap DB wait time
across concurrent requests on the event loop.

The reason to skip it: the multi-process model already provides concurrency.
With N bjoern workers sharing the same port via `SO_REUSEPORT`,
the OS load-balances requests across independent processes.
Each process handles one request at a time, but N requests run in parallel.
That's enough concurrency for this workload without adding async complexity to the application code.

Once the hot path moved to the in-process Rust engine, the question became moot —
that call is synchronous, and there is no I/O to overlap.

## Bjoern

[Bjoern](https://github.com/jonashaag/bjoern) is a C WSGI server built on libev.
Its selling point is minimal per-request overhead — it stays out of the way.

I tested several other WSGI server options (gunicorn, waitress, cheroot, meinheld)
and Bjoern consistently came out ahead on throughput for this workload.
It is not the most ergonomic choice —
it has no graceful reload, limited configuration, and requires building from source —
but for a read-heavy API where raw throughput matters, it is the right tool.

## The Multi-Process Model

Bjoern is single-threaded, so concurrency comes from running multiple worker processes.
Each worker binds to the same port using `SO_REUSEPORT`,
and the OS load-balances incoming connections across them:

```python
import multiprocessing
import os

import bjoern
import falcon


class PingResource:
    def on_get(self, req, resp):
        resp.media = {"ok": True}


def make_app() -> falcon.App:
    app = falcon.App()
    app.add_route("/ping", PingResource())
    return app


def worker(port: int) -> None:
    app = make_app()
    bjoern.run(app, host="0.0.0.0", port=port, reuse_port=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    num_workers = int(os.environ.get("WORKERS", max(2, os.cpu_count() or 1)))
    processes = [multiprocessing.Process(target=worker, args=(port,)) for _ in range(num_workers)]
    for p in processes:
        p.start()
    for p in processes:
        p.join()
```

Each worker is an independent OS process, so a crash doesn't take down the others.

My preference was for a stack where each layer is a separate decision. Bjoern handles HTTP with minimal per-request overhead; Falcon handles the application layer with explicit routing and a middleware model where I control what runs on every request — no behavior happening implicitly, easy to change when I want something different. FastAPI is a reasonable choice but it brings pydantic and starlette along as part of the package — a coherent ecosystem that pays off when you need validation, serialization, and OpenAPI docs. I needed none of those things, and I preferred not to inherit the rest.

