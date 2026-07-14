# Engine: reject impossible flavor needles via absent-gram presence bitmap

Status: filed 2026-07-07, follow-up to PR #622 / [00620-engine-flavor-text-narrowing.md](00620-engine-flavor-text-narrowing.md).
GitHub: #623.

## Idea

Presence bitmaps over the entire alpha 2/3-gram space of the flavor corpus:
676 + 17,576 bits ≈ **2.3 kB** (every letter appears somewhere; no 1-gram
tier). At bind, if any needle gram is absent from the corpus, no text can
contain the needle — return the provably-empty match set in O(needle),
skipping the fingerprint pass, the distinct-text scan, and verification.
Exact empty narrowing collapses Or-combos containing the impossible child.

The df=0 endpoint of the necessary-condition-filter spectrum: fingerprint
(learned features, ~3% statistical pass) → full n-gram postings (exact,
~5 MB) → presence bitmap (exact rejection only, 2.3 kB).

## Measured (26,321 distinct texts)

- Absent 2-grams: 90/676; absent 3-grams: 11,636/17,576 (66%) — the 3-gram
  tier is where the power is.
- 9.3% of `/usr/share/dict/words` (21,933 of 235,762, ≥3 chars) contain an
  absent gram → instant provable no-match.
- Real MTG vocabulary (eldrazi, phyrexian, jace, urza) is all present; the
  beneficiaries are typos, gibberish, and genuinely-absent words
  (xylophone, quixotic).

## Expectations

Polish, not milliseconds: the fingerprint already handles these needles at
~0.02 ms (`ft:zzzqqq` = 0.017 ms on the #622 branch); this takes them to
~1 µs and upgrades a statistical guarantee to a formal one. ~30 lines in
bind, built in the same pass as the fingerprints.

## Tasks

- [ ] 2/3-gram presence bitmaps in FlavorIndex
- [ ] Bind check before the fingerprint pass: absent needle gram → empty FlavorMatch
- [ ] Unit test: absent-gram needle proves empty; present-gram needle scans
- [ ] Confirm `ft:zzzqqq`-class queries drop to ~µs, nothing else moves
