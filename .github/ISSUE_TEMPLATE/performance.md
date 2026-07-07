---
name: Performance
about: Query latency, memory footprint, or throughput work — a measured problem with a proposed mechanism
title: ""
labels: performance
assignees: ""
---

**Measured problem**
What is slow or too big, with numbers — query timings, archive bytes, RSS. Name the
protocol (warmup/window, corpus size, build, machine) so the numbers are comparable later.

**Where the cost is**
The mechanism, not the symptom: full scan vs candidate gather, emission vs eval, allocation,
lock contention, etc. If a decomposition experiment isolated it, describe the experiment.

**Proposed approach**
The design and its expected cost (archive bytes, build time, code paths touched). Include
alternatives considered and why they lose — ideally with sizing, not vibes.

**Acceptance**
Which benchmark or survey re-run proves it, which queries must improve (by how much), and
which controls must not regress.
