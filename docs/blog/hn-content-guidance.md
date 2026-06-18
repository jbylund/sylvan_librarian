# HN Post Content and Structure Guidance

Derived from two rounds of analysis:
1. ~9 top posts from four HN front-page dates (April–June 2026) — general writing patterns
2. ~10 posts found via HN Algolia search for performance, parser, Rust, PostgreSQL, and "I built X" topics — patterns specific to the types of posts in this series

See [hn-title-guidance.md](./hn-title-guidance.md) for title and framing rules.

## The core pattern

The best posts are deductive: claim comes first, evidence comes second. The reader knows what you
are arguing by the end of paragraph one. Posts that build up to a conclusion bury the interesting
part.

---

## Opening hooks

Every high-performing post uses one of three moves:

**The scene-of-discovery sentence.** First sentence places you in a concrete moment before any
context. "I needed to check that one container could reach another over an internal Docker network."
No preamble, no "in this post I will", no background. You are already inside the problem.

**The blunt thesis with a credibility kicker.** State the conclusion, then immediately establish
why you have standing: "Nobody can write correct C, or C++. And I say that as someone who's written
C and C++ on an almost daily basis for about 30 years."

**The upfront result + backstory promise.** State the outcome first, then offer the causal story:
"This is a story of how building HTML-first doubled a company's users literally overnight."

What they do NOT do: open with history of the problem, with a definition, with "recently I've been
thinking about...", or with a question.

---

## Structure and length

Top posts are 500–1,400 words. A 3,500-word post can work if it earns the length by showing many
independent self-contained examples (each surprising on its own).

Eight of nine general posts had zero headers or just one. Headers appear as navigation aids in
tightly technical pieces where readers need to orient inside code — not as padding or section
announcements.

All posts follow one of two arcs:
- **Narrative arc:** problem encountered → investigation → discovery → resolution
- **Argument arc:** bold claim → evidence → implications → close

No meandering. Every paragraph advances either the story or the argument.

---

## 10 rules for general content and structure

**1. Open with the exact moment of discovery, not the surrounding context.**
Your first sentence should be the instant you hit the problem. "I needed to check that one
container could reach another" is the moment. "I've been working with Docker networks for a while
and recently ran into an interesting situation" is context around it. Start at the moment.

**2. State your thesis in the first paragraph, then prove it.**
Every successful post here works deductively: claim comes first, evidence comes second. "Nobody can
write correct C." "A database is just files." "Building HTML-first doubled our users." The reader
should know what you are arguing by the end of paragraph one.

**3. Show the artifact before explaining it.**
If you have a stack trace, a three-line bash command, a config file, or a benchmark table, show it
before you explain what it means. The reader's brain engages with the artifact first and your
explanation lands on prepared ground.

**4. Include one embedded anecdote or secondary scene that does not feature you.**
The highest-scoring posts contain a story attributed to someone else — a colleague's war story, a
housing benefit office scene, a 30-year career credential. This gives the post a voice beyond
"person explains thing they know."

**5. Express uncertainty as a specific carve-out, not general softening.**
Instead of "this may not work in all cases," write "this only works for plaintext HTTP" or "the
fix is only safe when the generation counter fits in a single cache line." Name the exact conditions
under which your claim does not hold.

**6. End with a sentence that has no follow-up.**
The best closes are self-contained and slightly final: a punchline, a wry contradiction, or one
sentence that generalizes the story without re-explaining it. Do not summarize, invite comments,
or re-state the title claim.

**7. Use a rebuttal structure for at least one objection.**
Voice and dismiss the most common counterargument before the reader forms it. This signals you have
thought harder about the problem than a first-pass argument and inoculates readers who might
otherwise disengage.

**8. Include one concrete number that makes the result legible.**
Not "performance improved significantly" — "45,000 req/s at 10k records, still 38,000 req/s at 1M
records." The number does not need to be impressive; it needs to be specific enough that the reader
can feel the scale.

**9. Keep the post to the length of the argument, not the length of your knowledge.**
You know more about this topic than you are writing. That is correct. The right length is the
minimum needed to make the argument undeniable; everything else is a separate post.

**10. Let the interesting thing be interesting without editorializing it.**
The posts that do not work are the ones that tell you how to feel about the thing they just showed
you. Show the gdb oscillation. Show the patch. Trust the reader.

---

## 10 rules specific to performance and systems posts

These are drawn from analysis of PostgreSQL optimization, Rust/systems, parser/compiler, and "I
built X" posts — the specific types in this series.

**1. Prove the claim before explaining it.**
Show the screenshot, the benchmark number, or the EXPLAIN ANALYZE output *before* you explain why
it happened. Readers need to see the problem is real before they will care about the analysis.

**2. Name your dead ends.**
"My investigation was chaotic and instinct-driven at first." Dead ends build trust because they
signal you actually did the work rather than reverse-engineered a clean narrative. Every
performance post that scored well shows at least one failed approach before the solution.

**3. Give the multiplier a context anchor, not just a raw ratio.**
"309 GB of data processed to return 10,000 rows." Translate raw numbers to human scale. If you have
a buffer count, convert it: `SELECT pg_size_pretty(40557115 * current_setting('block_size')::bigint)`.
The reader needs a reference to feel the absurdity of the inefficiency.

**4. Use progressive pseudocode, not prose, for algorithms.**
Show the algorithm in 3-5 versions, each adding exactly one concept. "Add minterm compression —
that's one extra (5th) detail." This works better than prose because the reader can see what changed
and what stayed constant. Use it for any algorithm with more than two moving parts.

**5. Justify "I built X" from first principles, not from "existing tools are bad."**
"Rust appears late in the Bootstrappable Builds chain. For bootstrapping before C++ exists, you
need a Rust compiler written in C." This is a narrow, provable, specific gap. Vague dissatisfaction
reads as hubris; specific gap analysis reads as engineering.

**6. State exactly what your thing cannot do.**
"The compiler passes 34/220 test cases." "This only works for plaintext HTTP." Stating scope
limits makes everything you claim it *can* do believable. Posts that oversell get destroyed in
comments.

**7. Surface the non-obvious cost, not just the speedup.**
"The index is 214 MB — almost half the size of the entire table." The interesting insight is not
"4x faster." It is "the standard advice costs 214 MB; the better advice delivers the same speed at
66 MB." Always ask: what is the hidden cost of the obvious solution?

**8. Show the EXPLAIN ANALYZE, do not summarize it.**
Reproduce the full output, including `Rows Removed by Filter` lines that contain the diagnostic
information. Readers who know Postgres can verify the claim; readers who don't learn what to look
for. "The query was doing a seq scan" strips the teachable moment.

**9. Ground safety and correctness claims in actual bugs caught.**
"The port revealed subtle bugs in the C++ code when the Rust compiler wouldn't let me do things
that turned out to be legitimately incorrect." For systems/Rust posts, "the type system prevents X"
lands harder when you can name a specific instance of X that existed in the previous implementation.
Theoretical safety claims are less persuasive than a concrete race condition you actually had.

**10. Let your emotional reaction to a surprising result show.**
"That's 30 milliseconds, or ~1000 times less than the original query!" High-scoring technical posts
are not written in academic prose. Calibrated enthusiasm — "What????" after a 500,000× improvement
— signals to the reader that the author was genuinely surprised and that they should be too. This
is not unprofessional; it makes the numbers feel real.
