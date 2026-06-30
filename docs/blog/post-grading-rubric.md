# Blog Post Grading Rubric

A 100-point rubric for evaluating technical blog posts in this series. Posts are aimed at working software engineers — people who can follow code but who need the author to do the synthesis work, not just narrate what happened.

## Categories

### Technical Accuracy and Depth — 25 pts

Does the post explain mechanisms, not just outcomes? Are claims verifiable and calibrated?

- **High (22–25):** The mechanism is explained — not just "X was faster" but why, with the specific constraint or property that makes it so. Edge cases and failure modes are named. Uncertainty is acknowledged where it exists.
- **Mid (16–21):** Claims are correct but shallow. The "what" is present without the "why." Vague phrases like "introduces overhead" or "more efficient" without a mechanism.
- **Low (0–15):** Technically inaccurate, or so high-level that the content could apply to any project without modification.

### Concrete Evidence — 20 pts

Numbers, query plans, benchmarks, code snippets that show real implementation. Reproducible — methodology stated, not just the result.

- **High (17–20):** Every major claim is backed by a specific artifact: a timing, a query plan, a code path, a PR link. The evidence is labeled and parameterized (wrk settings, hardware, endpoint design).
- **Mid (11–16):** Some claims have evidence, others are asserted. Code examples exist but are toy-sized or decontextualized.
- **Low (0–10):** Vague assertions throughout. "Tests showed improvement." No reproducible detail.

**On PR links and code permalinks:** These are the strongest form of evidence for "we actually did this." A PR link proves the change is real, reviewable, and happened at a specific point in history. A commit-anchored code permalink (e.g. `blob/<sha>/path/file.py#L42-L51`) proves the code exists at the exact version discussed — a floating link to `main` will drift. Use both wherever a post describes a specific change: link the PR for the narrative ("we switched from X to Y") and link the code for the mechanism ("here is the implementation"). Inline links in prose are preferable to a references section — they stay attached to the claim.

### Clarity and Structure — 20 pts

Logical section order, clear problem statement, each section earns its place.

- **High (17–20):** A reader knows why they are reading each section. Sections build on each other rather than sitting side-by-side. Problem stated before solution.
- **Mid (11–16):** Generally followable but sections could be reordered without much loss. Some throat-clearing or setup that doesn't pay off.
- **Low (0–10):** Multiple competing arguments, no clear throughline, or information dumps without structure.

### Narrative Cohesion — 15 pts

One clear thesis. Satisfying problem → solution arc. The ending lands with specificity rather than trailing off.

- **High (13–15):** The post makes one argument. The conclusion follows from the body. A reader can state the thesis in one sentence after finishing.
- **Mid (8–12):** The thesis is present but diluted — the post covers too many topics, or the ending restates rather than resolves.
- **Low (0–7):** No clear thesis. Reads like accumulated notes. The ending doesn't connect to the opening.

### Honest Treatment of Tradeoffs — 10 pts

Acknowledges where the chosen approach has costs or limitations. Names uncertainty. Does not oversell.

- **High (8–10):** The post says what the approach cannot do, where measurement was incomplete, or where a different choice might be better in other contexts. Counterarguments are named before being answered.
- **Mid (5–7):** Tradeoffs exist in the prose but are soft-pedaled or buried. Uncertainty is gestured at without being named precisely.
- **Low (0–4):** Pure success story. No costs, no alternatives considered, no honest caveats.

### Writing Quality — 10 pts

Direct prose, no filler. Code examples earn their space. Plain language over impressive-sounding language.

- **High (8–10):** Sentences carry real information. Concrete nouns and active verbs. Code blocks are the right size — minimal enough to scan, complete enough to understand.
- **Mid (5–7):** Mostly clean but with hedging, padding, or jargon used to impress rather than communicate.
- **Low (0–4):** Filler throughout, or code blocks that restate what the prose already said.

## Scoring Notes

**Scope mismatch is a structural penalty, not a depth penalty.** A post covering four topics is not four times as valuable as a post covering one. The Narrative Cohesion and Clarity categories absorb the cost of scope creep — not Technical Depth, which should reflect the quality of the content that is present.

**Introductory posts are not penalized for being introductory.** A series opener that correctly previews three mechanisms without explaining any of them in depth scores appropriately on Technical Depth for what it attempts — it should not be compared against posts that go deep on one mechanism.

**Honest caveats raise the score.** A post that says "packrat made simple queries 15% slower and we don't have production data to know if the tradeoff is net positive" scores higher on Tradeoffs than one that says "packrat improved parse performance." The admission of uncertainty is a strength, not a weakness.

**Evidence must be reproducible to score high.** Saying "benchmarks showed 13× faster" is not evidence. Saying "wrk with 4 threads, 100 connections, 30 seconds, same Docker Compose network, pre-loaded data, M5 Max" is. Methodology matters as much as the number.

## Reference Scores from the First Six Posts

Scores are the consensus of five independent graders, averaged and rounded to the nearest whole number.

| Post | Score | Tech (25) | Evidence (20) | Clarity (20) | Narrative (15) | Tradeoffs (10) | Writing (10) | Strongest | Weakest |
|------|-------|-----------|---------------|--------------|----------------|----------------|--------------|-----------|---------|
| 00032: Why Build a Self-Hosted Scryfall | **74** | 17 | 13 | 17 | 12 | 7 | 8 | Clarity; Narrative | Concrete Evidence |
| 00064: Falcon + Bjoern | **89** | 22 | 19 | 18 | 13 | 9 | 9 | Technical Accuracy; Concrete Evidence | Narrative Cohesion |
| 00096: Choosing a Parser | **92** | 23 | 18 | 19 | 14 | 9 | 9 | Clarity and Structure | Concrete Evidence |
| 00112: Implicit AND and Query Balancing | **78** | 22 | 17 | 14 | 8 | 10 | 8 | Honest Tradeoffs | Narrative Cohesion; Clarity |
| 00128: Compiling AST to SQL | **87** | 23 | 17 | 18 | 14 | 6 | 9 | Technical Accuracy; Narrative | Honest Tradeoffs |
| 00144: One Query, Two Answers | **95** | 24 | 20 | 19 | 14 | 9 | 9 | Concrete Evidence | (all categories strong) |

Post 00096 and 00144 set the standard: a working alternative grammar (Lark) alongside the chosen one, the `NOT MATERIALIZED` behavior documented with actual query plans and an honest "this surprised me."

Post 00112 shows the cost of scope creep: excellent individual sections on implicit AND injection, query balancing, and packrat caching — but three topics in one post fragments the thesis and drops the Narrative Cohesion score even when Technical Accuracy is high.

## Top Improvement Suggestions per Post

**00032** — add one paragraph explaining *why* the Rust engine achieves the speedup (the performance table has methodology but no mechanism); anchor the ranking/prefer_score section with at least one named weight or connect it to the arithmetic thesis.

**00064** — the post opens with benchmark setup (wrk parameters, container configuration) before the reader has a reason to care; add a one-paragraph opener that states the claim and why it matters for a cache-heavy search engine, then let the methodology follow as evidence. Also ground the conclusion beyond the Lotus/Mercedes metaphor with one concrete sentence naming the actual tradeoffs.

**00096** — describe the 121-query corpus (query types, hardest cases) so "all 121 pass" is a falsifiable claim rather than an assertion; add a rough timing comparison between Lark and pyparsing, or name one scenario where Lark would be the better choice.

**00112** — add a single opening frame naming all sections as instances of one problem (without it, the sections sit side-by-side with no thesis); cut the arithmetic parsing section to only what is new here (the fold function and BinaryOperatorNode reuse), since the grammar structure was already covered in 00096.

**00128** — name the specific failure mode that breaks the injection-safety guarantee (a column name entering the alias map from user input would collapse the structural argument); address the LIKE multi-word behavior explicitly — is "words in order, not adjacent" intentional parity with Scryfall or a known divergence.

**00144** — add a PostgreSQL version callout (CTE materialization behavior changed in PG 12, so the NOT MATERIALIZED plans are only reproducible with a version); expand or relocate the "surprised me" split-timings paragraph — it is the best insight in the post but is buried mid-section.
