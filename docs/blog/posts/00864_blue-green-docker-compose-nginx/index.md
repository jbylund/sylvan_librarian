---
title: "Zero-Downtime Deploys with Blue/Green Docker Compose and nginx"
date: 2027-07-17
publishDate: 2027-07-17
tags: ["infrastructure", "docker", "nginx", "deployment"]
summary: "Two identical Docker Compose stacks behind one nginx upstream. Deploy by bringing up the new stack, waiting for a health check to pass, swapping the nginx upstream with a reload, then tearing down the old one — no orchestrator required."
---

## The Problem with In-Place Restarts

The first deploy strategy for Sylvan Librarian was the obvious one: `docker compose down` followed by `docker compose up`.
The gap between the two commands meant the service was unreachable, but that was not the main problem.
The real issue was what came after: the API process that starts fresh has to warm its LRU cache before latency returns to baseline.
On a cold start, cache-miss requests each hit PostgreSQL — P95 latency measured on the production instance by issuing sequential uncached searches immediately after restart stays around 200–400 ms for the first minute or two until the cache fills, compared to under 5 ms for a warm-cache hit.
The "downtime" was not just the restart itself.

The fix I reached for was not Kubernetes, not Nomad, not Fly.io's built-in zero-downtime deploys.
It was two Docker Compose stacks running on the same host, a few env files, and `nginx -s reload`.

## Two Stacks, One Host

Docker Compose's `--project-name` flag gives every container in a stack a namespaced name and an isolated network.
Two stacks with different project names can run the same `docker-compose.yml` on the same host without conflict, as long as their host ports do not collide.

The port split is in two env files:

```
# envs/blue
API_PORT=18080
APP_ENV=blue
ENABLE_CACHE=true
ENVIRONMENT=prod

# envs/green
API_PORT=18081
APP_ENV=green
ENABLE_CACHE=true
ENVIRONMENT=prod
```

Blue binds to `18080`, green binds to `18081`.
Each stack gets its own Docker project name (`arcane_blue`, `arcane_green`), its own bridge network, and its own volume namespace — so `pgdata` in blue is `arcane_blue_pgdata`, never shared with green.
One stack can be torn down while the other serves traffic.

The data directory is also per-environment:

```yaml
# docker-compose.yml (simplified)
volumes:
  - ./data/api/${APP_ENV:-dev}/:/data/api
```

So `blue` reads from `data/api/blue/` and `green` reads from `data/api/green/`.
Both sets of data stay on disk across restarts.

## The nginx Upstream Swap

nginx sits in front of both stacks as a reverse proxy.
`nginx -s reload` is the mechanism that makes the cutover zero-downtime.
When you send that signal, nginx starts new worker processes with the updated configuration, waits for the old workers to finish their in-flight requests, then exits the old workers cleanly.
Connections that arrive during the reload go to the new workers; connections already open drain through the old ones.
No TCP resets, no connection errors.

The upstream configuration on the host points to whichever stack is currently active:

```nginx
upstream arcane_api {
    server 127.0.0.1:18080;   # blue — active
    # server 127.0.0.1:18081;  # green
}
```

To promote green, write a new config file with the ports swapped and call `nginx -s reload`:

```bash
ACTIVE_PORT=18081  # green's port
sed "s/18080/${ACTIVE_PORT}/" /etc/nginx/conf.d/arcane.conf.tmpl \
    > /etc/nginx/conf.d/arcane.conf
nginx -s reload
```

This swap is not yet wired into `make rolling-deploy` — it is a manual step that runs after `--wait` exits successfully.
Adding it as the final Makefile step is straightforward; the snippet above is the full implementation.

The post-reload race window is benign: nginx does not start the reload until it has parsed and validated the new config.
If parsing fails, the running workers continue uninterrupted with the old config.
Requests that arrive while the new workers are starting land on old workers, which are still serving.
There is no window where the upstream is unreachable.

## The Deploy Script

`make rolling-deploy` (added in [PR #455](https://github.com/jbylund/sylvan_librarian/pull/455))
brings both stacks up sequentially. The full target, anchored to the current commit:

```makefile
# https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/makefile#L114-L119
rolling-deploy: deps-blue deps-green
	@echo "=== Deploying blue ==="
	cd $(GIT_ROOT) && docker compose \
	  --project-name arcane_blue \
	  --env-file .env \
	  --env-file envs/blue \
	  --file $(BASE_COMPOSE) \
	  up --remove-orphans --detach --wait
	@echo "=== Blue healthy. Deploying green ==="
	cd $(GIT_ROOT) && docker compose \
	  --project-name arcane_green \
	  --env-file .env \
	  --env-file envs/green \
	  --file $(BASE_COMPOSE) \
	  up --remove-orphans --detach --wait
	@echo "=== Rolling deploy complete ==="
```

The `--wait` flag blocks until every service in the stack passes its health check (or the retries are exhausted).
The API health check probes `localhost:8080/get_pid`, which only succeeds after the process has fully started and is accepting connections
([docker-compose.yml, lines 89–100](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/docker-compose.yml#L89-L100)):

```yaml
healthcheck:
  test:
    - CMD
    - curl
    - --fail
    - --user-agent
    - healthcheck
    - localhost:8080/get_pid
  interval: 5s
  timeout: 1s
  retries: 60
  start_period: 40s
```

`retries: 60` at `interval: 5s` gives the stack five minutes to become healthy.
The API process takes roughly 40 seconds to load card data on first start — the `start_period` covers that window.

## Failure Modes and Rollback

**The new stack never becomes healthy.** `docker compose up --wait` exits non-zero after the retry window.
The old stack is still running and nginx still points at it — users see nothing.
Tear down the new stack with `make green-down`, fix the problem, and redeploy.
The old stack was never touched.

**The new stack becomes healthy but the nginx swap fails.** Same recovery.
The old stack is running, nginx still points at it.
The new stack is idle and can be torn down.

**A bug makes it through health checks.** The `/get_pid` probe confirms the process started and responded — it does not verify that search queries return correct results, that card data loaded cleanly, or that database connectivity is intact.
A code bug that allows startup can reach production.
Rolling back takes about two seconds: swap the nginx upstream config back and reload.
As long as the old stack has not been torn down, rollback is a one-liner.

That last point argues for keeping the old stack running for a short soak period — ten or fifteen minutes — before tearing it down.
The cost is about 3 GB of RAM (one extra postgres process plus one API process) during that window.

## What This Skips

This approach works well for a single-host deployment with a low deploy rate.
Three things it does not handle:

**Cross-host load balancing.** If the service runs on multiple machines, the nginx swap and health check polling would need to happen at the load balancer level, not per host.

**Non-additive database migrations.** Both stacks share their postgres volume only within a project, but if a migration is not backward-compatible — a dropped column, a changed type — old and new code cannot run simultaneously against the same schema.
The current deploy requires every migration to be additive (new columns with defaults, nothing dropped).

**Database state rollback.** Swapping nginx backward rolls back the application code, not the data.
Writes that arrived during the new stack's window are visible to the old code.
For a read-heavy search API this is rarely an issue; it would matter for any endpoint that modifies state.

For a self-hosted project where the operator controls the deploy window and traffic is predictable, those constraints are manageable.
The gap between the complexity of running Kubernetes and the simplicity of `docker compose up --wait` + `nginx -s reload` is wide enough that the constraint list is worth accepting.

Two stacks, one nginx, a reload signal.
No magic — just composition of primitives that already exist.

## Related

The multi-process worker model this deploys is covered in
[Falcon + Bjoern: Choosing a Python Web Framework](00064_falcon-bjoern-web-framework.md).
Cross-process cache invalidation — a subtlety exposed by the multi-worker setup — is in
[Multi-Process Cache Invalidation with a Generation Counter](00512_multi-process-cache-invalidation.md).
