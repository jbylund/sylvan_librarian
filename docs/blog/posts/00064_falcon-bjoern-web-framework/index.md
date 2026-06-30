---
title: "13× the Throughput of FastAPI: Why I Use Falcon + Bjoern"
date: 2026-06-27
publishDate: 2026-06-27
tags: ["python", "falcon", "bjoern", "fastapi", "performance"]
summary: "Why Sylvan Librarian uses Falcon and Bjoern instead of the FastAPI + uvicorn default: a preference for explicit, close-to-vanilla Python over framework magic."
---

{{< sitename >}} caches search results, so most requests return without touching the database.
When a response is already computed, the framework serializing it is all that stands between the request and the answer.
On that path — pure serialization, no query, no business logic — Falcon + Bjoern handles 13× the throughput of FastAPI + uvicorn.
For a read-heavy API where cache hits dominate, the framework is not an irrelevant detail.

## Benchmark Setup

Each server ran in its own Docker container, built from the same base image with only the framework changed.
[`wrk`](https://github.com/wg/wrk) ran in a second container on the same Docker Compose network, so the measurement captured framework overhead without cross-machine jitter or real network latency.
The endpoint returned a pre-loaded list of 100 cards — no database query, no business logic.
Parameters: 4 `wrk` threads, 100 concurrent connections, 30-second run, on a MacBook Pro M5 Max (18 cores, 128 GB).

Falcon uses [orjson](https://github.com/ijl/orjson); FastAPI uses `response_model=list[Card]` — the idiomatic pattern that triggers Pydantic validation on every outgoing response:


|             | Falcon + Bjoern + orjson | FastAPI + Uvicorn |
| ----------- | ------------------------ | ----------------- |
| Req/sec     | 155,758                  | 11,712            |
| Avg latency | 635µs                    | 8.6ms             |


13× difference on an endpoint that does nothing except serialize a pre-loaded result —
which reflects the framework overhead, not the application work.
On cache-miss requests that do reach the database, query time dominates and the relative gap narrows.
{{< sitename >}} caches search results, so a large fraction of requests are cache hits that return immediately.
The faster the framework processes a hit, the more headroom is left for the requests that actually need the database.

## The Appeal of FastAPI

FastAPI is built around developer experience: declare your types as Python hints and get input validation, serialization, and auto-generated OpenAPI docs for free.
For APIs with many endpoints or complex response schemas, that is a genuine productivity win.

```python
from fastapi import FastAPI
from pydantic import BaseModel


class Card(BaseModel):
    name: str
    set_code: str
    collector_number: str
    power: str | None = None
    toughness: str | None = None
    mana_cost: str | None = None
    oracle_text: str | None = None
    set_name: str
    type_line: str


app = FastAPI()


@app.get("/search", response_model=list[Card])
def search():
    return cards
```

## The Case for Explicit Over Automatic

FastAPI and Pydantic introduce a layer of conventions you have to learn:
how decorators wire up routes, how response models work, when validation fires and when it doesn't,
what happens when you return a dict vs a Pydantic model.
At a previous job, response validation overhead became a real problem at scale —
Pydantic was re-validating every outgoing response body on its way out the door, even when the data was already correct, and at a few thousand requests per second that added enough CPU time that it showed up in profiles.
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

## Why Bjoern Wins: C, libev, No Python in the Hot Path

[Bjoern](https://github.com/jonashaag/bjoern) is a C WSGI server built on libev.
Its selling point is minimal per-request overhead — it stays out of the way.

Holding the Falcon app constant and swapping only the server:

| | Req/sec | Avg latency |
|---|---|---|
| Bjoern | 155,758 | 635µs |
| Granian (ASGI) | 89,081 | 1.12ms |
| Granian (WSGI mode) | 76,871 | 3.96ms |
| Uvicorn (WSGI mode) | 47,638 | 2.1ms |
| Gunicorn (sync workers) | 7,929 | 11.7ms |

![Requests per second by server](server-throughput.svg)

Gunicorn's sync workers handle one request per worker at a time — each worker blocks for the full serialization cycle before accepting the next connection, so 4 workers means at most 4 requests in flight.
Uvicorn and Granian are faster because their event loops can interleave connections, but running in WSGI mode adds a compatibility layer over their native async runtimes.
Granian in native ASGI mode closes the gap somewhat — the WSGI shim accounts for the difference between its two rows.
Bjoern uses libev directly in C, with no Python event loop in the hot path.

For a read-heavy API where throughput matters, it is a strong option.

Bjoern does require compiling a C extension, and it has no hot-reload support.
In a Docker Compose setup neither limitation matters much:
the container rebuilds on deploy anyway, and `--reload` workflows don't survive container restarts regardless of the server.
The tradeoffs land differently if you are running directly on a developer machine where framework install friction and live-reload matter more.

Bjoern's release cadence is low — the last tagged release was in 2021. For a production HTTP server that is worth knowing. The practical counterpoint is that the protocol surface Bjoern covers (WSGI over HTTP/1.1) is stable enough that an old release is less concerning than it would be for a higher-level library tracking a moving API.

## The Multi-Process Model

Bjoern is single-threaded, so concurrency comes from running multiple worker processes.
Each worker binds to the same port using `SO_REUSEPORT`,
and the OS load-balances incoming connections across them:

```python
import multiprocessing
import os

import bjoern
import falcon
import falcon.media
import orjson


class SearchResource:
    def on_get(self, req, resp):
        resp.media = _CARDS


def make_app() -> falcon.App:
    app = falcon.App()
    json_handler = falcon.media.JSONHandler(
        dumps=orjson.dumps,
        loads=orjson.loads,
    )
    extra_handlers = {"application/json": json_handler}
    app.req_options.media_handlers.update(extra_handlers)
    app.resp_options.media_handlers.update(extra_handlers)
    app.add_route("/search", SearchResource())
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

FastAPI's ecosystem is an engineering achievement — validation, serialization, OpenAPI docs, all wired together.
But like a Mercedes, all that engineering has weight, and you pay for it whether or not you use it.
I wanted a Lotus: light and fast.
