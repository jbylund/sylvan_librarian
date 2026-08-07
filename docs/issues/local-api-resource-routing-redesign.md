# Reworking APIResource's Routing and Parameter Handling

A plan, in independently shippable steps, for replacing `APIResource`'s hand-rolled routing and
parameter coercion. Each step stands alone; none requires the next.

The prompt for this was the admin route split, whose exposure analysis and fix design are tracked out
of tree per [the `security-` convention](./README.md#unfixed-security-findings). This doc is only the
architecture and the order of operations — it is deliberately written so it would read the same had
nothing prompted it.

## Status

Steps 1 and 2 have shipped. The problems and decisions below are kept as written — they are the
reasoning the shipped work rests on — but read them as the state of the code *before* those PRs.

| step | status |
| --- | --- |
| 1. Rewrite coercion | shipped — #787 |
| 2. `@route` and explicit registration | shipped — #789, with #791 following on |
| 3. Move the admin handlers to a child resource | shipped |
| 4. Delist | partly — the child is unadvertised; the public listing is not yet opt-in |
| 5. Auth at the mount | not started |

Three things this doc commits to that the shipped code deliberately does **not** do:

- **Falcon's router was not adopted.** See [Design decisions](#design-decisions), which has been
  revised to record why the hand-rolled dispatch stayed.
- **`DISALLOWED_QUERY_ARGS` still exists**, and handlers still declare `**_: object` — 27 of 28 do.
  Both retire with per-route unknown-parameter enforcement, which step 2 declared but did not enforce.
- **`advertise` is now read; `ignore_unknown_params` is not.** The mount passes `advertise=False`
  once for the whole child, and the listing filters on it. Making the *public* listing opt-in — so a
  forgotten flag under-advertises rather than over-advertises — is what remains of step 4.

## Current mechanics

Falcon's router is **entirely unused**. `api/api_worker.py` installs one sink —
`api.add_sink(sink._handle, prefix="/")` — and `_handle` does its own dispatch against a route table
built in `APIResource.__init__`. Path segments beyond the action word are passed positionally, bounded
by a precomputed positional capacity.

Before step 2 that table came from walking `dir(self)` and taking every public callable; it is now
built from methods carrying an `@route` marker, scanned off the class. Before step 1, query parameters
were coerced by `make_type_converting_wrapper`, which read the handler's signature per call and
converted each string via `_convert_string_to_type`; a `ParamBinder` now resolves each handler's
annotations once at registration and binds against a fixed plan.

Two properties of that design are worth keeping, and shape everything below:

- **Handlers are plain functions with typed keyword params.** Tests call them directly
  (`api._search(query="name:opt")`) with no request/response objects, and internal callers use them as
  ordinary methods — `__init__` calls `setup_schema()`, `_run_import_under_lock` calls
  `backfill_prefer_scores()`. Falcon's native `on_get(self, req, resp)` convention would break both.
- **Coercion and defaults come from the signature.** No schema to keep in sync, no per-handler parsing.

## Measured problems

Numbers taken 2026-07-27 on a 64 GB dev machine, against the 520-query engine corpus in
`benchmarks/survey/` for the comparison baselines.

### Coercion is the whole overhead

| | |
| --- | ---: |
| plain call, no coercion | 0.1 μs |
| coercion, logging suppressed | 4.4 μs |
| coercion, INFO logging on (production level) | **20.9 μs** |
| engine query, p50 | 61 μs |

Per parameter, per request, `_convert_string_to_type` rebuilds a 10-entry `converter_map`, splits the
annotation string on `|`, and emits a `logger.info` per successful conversion. `search` has nine
parameters, so a p50 search spends ~21 μs coercing before it spends 61 μs answering, and ~16 μs of
that is log formatting at the `logging.INFO` level `api_worker.py` sets.

### Coercion is fail-open in four ways

| input | behavior before step 1 | now |
| --- | --- | --- |
| unconvertible enum (`orderby=nonsense`) | logs, passes the **raw string** to a handler annotated `CardOrdering` | 400 |
| unconvertible int (`limit=abc`) | logs, passes the raw string | 400 |
| unknown parameter (`?limt=5`) | silently dropped, no error | **still dropped** |
| wrong type from an internal caller | passed straight through | raises |

The clean 400s a black-box probe saw for bad input came from downstream checks like
`_validate_limit`, not from this layer. Unknown parameters are the one row left: rejecting them needs
a per-route opt-out so that `utm_*` and cache-busters keep working on public URLs, which is what
`ignore_unknown_params` is for.

### Coercion depends on a module-level import in the caller's file

`_convert_string_to_type` does `param_type.__name__` and then `param_type.split("|")`, so it only works
when annotations are **strings**:

```python
_convert_string_to_type("x", "str | None")   # -> 'x'
_convert_string_to_type("x", str | None)     # -> AttributeError: 'types.UnionType' has no '__name__'
```

`api/api_resource.py` has `from __future__ import annotations`, which is the only reason this works. A
new resource module without that import would 500 on the first request to any handler with an
`X | None` parameter — and creating a new resource module is exactly what the admin split does.

### Registration is fail-open and self-advertising

`dir(self)` plus `callable()` means a method is routed unless someone remembers a leading underscore,
which overloads `_` to mean both "private in Python" and "not HTTP-reachable" — so
`setup_schema`, called from `__init__` and from tests, cannot be hidden without lying about its Python
visibility. Because `_build_routes_listing` iterates the same table, anything registered is also
published: a request to any unknown path returns every route name.

Scanning the *instance* also means any new public attribute can change the route table. A child
resource stored as `self.admin` escapes registration only because it has no `__call__`.

Step 2 fixed the registration half: routes are marked, the scan is over the class, and a path claimed
twice is a startup error. The self-advertising half is unchanged — all 30 routes are still listed, and
`advertise` exists but is read by nothing until step 4.

### The routing layer is not where the cost is

| path | current dict dispatch | Falcon `CompiledRouter` |
| --- | ---: | ---: |
| `/search` (static) | 91 ns | 146 ns |
| `/static/app_js` (nested static) | 88 ns | 256 ns |
| `/card/eoc/104` (templated) | 232 ns | 254 ns |

Falcon's router is genuinely slower than a bare dict — by tens to a couple hundred nanoseconds. Against
a 20,900 ns coercion path and a 61,000 ns query, that is noise: adopting it spends ~100 ns to save
~19,900 ns. Worth stating explicitly so the cheap layer does not attract the optimization effort.

## Design decisions

**~~Take Falcon's router, not its responder convention.~~ Superseded — the dict dispatch stayed.**
The argument was that `add_route` gives URI templates with converters (`{limit:int}`), per-method
responders, and 405s for unmapped methods, retiring the positional-split scheme that a previous
dispatch rewrite broke. Step 2 got the part that mattered — per-route declared methods and 405s — from
the `@route` marker directly, in about ten lines of `_handle`, without adopting the router. What is
left unclaimed is URI templates: the positional-split scheme and its precomputed capacity are still
there.

Adopting the router now would be a swap with no remaining behavioural payoff, against the measurement
above showing it is the slower of the two. Revisit only if templated paths start carrying real
structure — typed segments, or a path that the split cannot express.

**One decorator marks; it does not wrap.**

```python
@route(methods=("GET",), advertise=True, ignore_unknown_params=True)
def search(self, *, query: str | None = None, limit: int = 100) -> dict[str, Any]: ...
```

It attaches a spec and returns the function unchanged. Wrapping would put a fail-open coercer between
internal callers and handlers — `limit=[1,2,3]` from `_run_import_under_lock` would sail through instead
of raising — and would add a frame to every traceback for no benefit. The existing code already gets
this right by putting the wrapper in `action_map` while `self.method` stays plain; a wrapping decorator
would be a regression.

**Marking flips the default to fail-closed** while keeping declaration at the definition site, so adding
a route stays one line. It frees `_` to mean only "private in Python", and gives `methods` and
`advertise` a home. Registration scans `type(self)` for the marker, not `dir(self)`, so instance
attributes can never become routes.

**Resolve annotations and build a coercion plan once, at import.** `typing.get_type_hints()` gives real
types instead of strings, which fixes the union crash and makes a precomputed plan possible:

```python
plan = ((name, converter, default), ...)     # built once
for name, convert, default in plan:          # per request: no introspection, no dict rebuild
    kwargs[name] = convert(params[name]) if name in params else default
```

An annotation with no converter becomes an **import-time** error rather than a silent pass-through.

**Unconvertible values are a 400. Unknown parameters are per-route.** These are different failures: a
bad `orderby` is a client sending a value the API does not accept, while an extra `utm_source` is a
client sending something irrelevant. Strict is the default; public routes opt into
`ignore_unknown_params=True` so tracking parameters and cache-busters keep working on `/search`.
`DISALLOWED_QUERY_ARGS` then disappears — injected names like `falcon_response` are not *rejected*, they
are simply never sourced from the query string.

**Handlers stop needing `**_: object`.** Worth naming as a goal rather than a side effect: a path
traversal in `read_sql` was inert *only because* it lacked `**_`, so injected kwargs raised `TypeError`
first. Making that a designed property is most of the value here.

## Sequencing

Ordered so that each step is shippable, and so the steps with a measurable story come before the ones
that move code.

1. **Rewrite coercion.** *(shipped, #787)* Precomputed plan, annotations resolved at import, 400 on
   unconvertible, drop the per-conversion log line. Self-contained, has the only performance story,
   and fixes a latent crash that the next step would otherwise trip over. No routes move. The
   per-route unknown-parameter policy slipped out of this step and is still unshipped — declaring it
   needs the marker from step 2.
2. **Add `@route` and explicit registration.** *(shipped, #789)* Pure refactor: same routes, same
   paths, existing tests untouched. Retires `dir(self)`, frees `_` to mean only "private in Python",
   and makes each route declare which HTTP methods it accepts. #791 followed on, registering the
   static assets at the URLs they are actually requested at now that paths are declared rather than
   derived from method names.
3. **Move the admin handlers to a child resource.** *(shipped)* The closure analysis for this — which
   methods and handles are shared, which move — lives in the out-of-tree doc. Mounting is a loop over
   a child's marked methods; resist extracting a `Router` abstraction until a second mount point
   exists.
4. **Delist.** `advertise=False` for the child, and make the public listing opt-in so forgetting a flag
   under-advertises rather than over-advertises.
5. **Auth at the mount.** One check where dispatch enters the child, rather than a decorator on every
   handler that someone forgets on the next one.

## Out of scope

**This does not decompose the god object.** `APIResource` will still own SQL generation, engine dispatch,
import orchestration, static file serving, and HTML rendering after all five steps. Step 3 moves 22
methods out, which helps, but routing hygiene is not architecture and nobody should expect the latter
from this plan.

## Open questions

- Does `mount()` need to exist as a concept, or is it five lines at the one call site? Prefer the loop
  until a second mount point argues otherwise.
- Should the 404 route listing survive at all? It is useful for discovery and it is also the most
  convenient enumeration of the surface. Opt-in advertising makes it defensible either way, but it is a
  flag that has to be right.
- `prefer_score_tuner` serves a static HTML page that adjusts **server-global** scoring weights. That
  makes it a mutation with a UI rather than a developer tool. Delete instead of moving it?
