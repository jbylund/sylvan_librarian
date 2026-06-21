---
title: "Zero-Downtime Deploys with Blue/Green Docker Compose and nginx"
date: 2027-07-03
publishDate: 2027-07-03
tags: ["arcane-tutor", "infrastructure", "docker", "nginx", "deployment"]
summary: "Two identical Docker Compose stacks behind one nginx upstream. Deploy by bringing up the new stack, swapping the upstream on health-check pass, then tearing down the old one — no orchestrator required."
---

## The problem with in-place restarts


## Two stacks, one host


## The nginx upstream swap


## The deploy script


## Failure modes and rollback


## Related

The multi-process worker model this deploys is covered in
[Falcon + Bjoern: Choosing a Python Web Framework](00064_falcon-bjoern-web-framework.md).
Cross-process cache invalidation — a subtlety exposed by the multi-worker setup — is in
[Multi-Process Cache Invalidation with a Generation Counter](00512_multi-process-cache-invalidation.md).
