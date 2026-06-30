# Writing a Blog Post

A step-by-step process for drafting a post in this series.

## Step 1: Orient with the Guidance Docs

Read these before touching the draft:

- [post-grading-rubric.md](./post-grading-rubric.md) — 100-point rubric; know what "high" looks like in each category before you write
- [hn-content-guidance.md](./hn-content-guidance.md) — structure, arc, opening hooks, 20 rules
- [hn-title-guidance.md](./hn-title-guidance.md) — title form, what fails, the core pattern

Pay special attention to:

- The **opening hook** rules — do not open with context, history, or "I've been thinking about"
- The **section title style** — title-case, named after the outcome or problem solved, not the topic
- The **close** — one self-contained sentence, no summary, no invitation to comment

## Step 2: Find the Post Stub

Open [blog-post-plan.md](./blog-post-plan.md) and locate the entry for this post. The stub describes the thesis, the mechanism, and any linked PRs or changelogs. Follow every link in the stub.

The plan may be incomplete. If the stub is thin or absent, that is fine — the git history is the authoritative source of what actually happened.

Note the publish date and where this post sits in the dependency chain. If it depends on an earlier post, read that post's draft — do not repeat ground it already covered.

## Step 3: Research the Repository

Read the code and commit history for every mechanism the post claims to explain. Do not write from the stub alone.

**Find the PRs that introduced or significantly changed the relevant code:**

```bash
# Search commit history for the relevant file or feature
git log --oneline --follow <file>
git log --oneline --all --grep="<keyword>"

# Read a specific commit's diff
git show <sha>

# Read the full diff for a PR
gh pr diff <number>

# Read PR description and comments
gh pr view <number>
```

**Read the current source**, not just the diff. Understand the before state — what did the code look like before the change? The contrast is evidence. Walk the current implementation at the function level and note any non-obvious design decisions.

**Generate commit-anchored permalinks** for any code you plan to quote:

```bash
# Get the current HEAD sha for anchoring links
git rev-parse HEAD
# Link form: https://github.com/jbylund/sylvan_librarian/blob/<sha>/path/to/file.py#L42-L51
```

Use anchored SHAs, not links to `main` — floating links drift.

**Check for EXPLAIN ANALYZE output** in PR descriptions, comments, or changelogs. If the post makes a query-performance claim, reproduce the full output — do not summarize it.

## Step 4: Research the Topic Externally

Search for blog posts, official docs, and Stack Overflow threads on the core topic. The goal is two things:

1. **Verify the mechanism** — does what you plan to write match how experts describe it?
2. **Find the counterargument** — what would a skeptic say? You need to voice and dismiss this.

Cite and link any external source that helps a reader go deeper. Do not link to sources just for credibility — only link when the source adds something the post does not cover.

## Step 5: Benchmark If the Post Claims a Speedup

If the post describes a performance difference, measure it. Do not report a number from the stub or a PR description without verifying it still holds.

**Write a minimal benchmark** that isolates the change:

- Warm the system before measuring (at least one untimed run)
- Run enough iterations to get a stable median (50+ for in-process operations)
- Report: environment (hardware, OS, relevant versions), methodology, and the full distribution — not just the best run

**Report results honestly:**

```
Median: X ms  P99: Y ms  (N=50 runs, warm cache, M5 Max, Python 3.13, PostgreSQL 17)
```

If the benchmark shows a smaller gain than the stub claimed, report the actual number. A smaller honest result is more persuasive than a larger claimed one.

## Step 6: Draft the Post

**Frontmatter:**

```markdown
---
title: "..."
date: YYYY-MM-DD
publishDate: YYYY-MM-DD
tags: [...]
summary: "..."
---
```

The summary is one or two sentences: the claim and the mechanism.

**Opening sentence:** Start at the moment of discovery or with the claim. No preamble, no "in this
post I will," and no benchmark or methodology setup — that belongs in the body, after the reader
knows why they should care.

**Show before explain:** Code snippet, benchmark table, or query plan before the prose that explains it.

**Code snippets:** Prefer simplified or pseudocode that conveys the idea without noise. Every snippet should link to where the real code lives (commit-anchored permalink). If the snippet is simplified, say so briefly.

**Arc:** Pick one and hold it:
- Narrative: problem → investigation → dead end(s) → resolution
- Argument: claim → evidence → counterargument → close

Name at least one dead end or failed hypothesis. Dead ends build trust.

**Evidence:** Every major claim needs a link — PR for the narrative, code permalink for the mechanism.

**Close:** One sentence. Slightly final. No summary.

**Formatting:** Use semantic line breaks throughout prose — each sentence on its own line. Single newlines do not render as line breaks in markdown, so the published output is unchanged, but the source is easier to diff and review. Do not apply semantic line breaks inside code blocks, frontmatter, or table cells.

## Step 7: Run Three Independent Graders

Spawn three separate agents, each with a fresh context. Give each one:
- The path to the draft file (e.g. `docs/blog/posts/<slug>/index.md`)
- The path to the rubric: `docs/blog/post-grading-rubric.md`

Ask each to read both files and score the draft against all six rubric categories, then provide their top two or three concrete suggestions for improvement.

Collect the three scores. If they diverge significantly, weight the lower scores more heavily. Identify the suggestions that appear across multiple graders or that address the lowest-scoring category.

## Step 8: Iterate on the Draft

Implement the top two or three improvements from the grader consensus. Then re-run all three graders on the revised draft.

- If the consensus score **increased**, the changes are keepers. Continue to the next round.
- If the consensus score **did not increase**, revert the changes and stop iterating.

Repeat up to **three rounds** total. Stop earlier if the score stops rising or reaches 90+.

After the final round, do a last pass for the common failure modes:

- Topic-label section headers — reframe as outcome or transformation
- Vague quantifiers ("significantly faster") — replace with the number
- Missing scope caveat — name one condition under which the claim does not hold
- Ending that restates the title — replace with a sentence that lands
