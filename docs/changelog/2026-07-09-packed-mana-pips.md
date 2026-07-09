# Mana pip counts pack into u64 lanes

`ManaCost` stored pip counts as `HashMap<String, u8>` — a shape chosen
because it made the Postgres jsonb query easy to write, not because the
engine needed it. Devotion and `mana=` comparisons paid string hashing and
map iteration per candidate (~21 ns and ~6–10 ns respectively).

Pips now pack into 8-bit lanes of a `u64` (WUBRGC + snow + X; generic
numbers were already dropped on both sides), with the ~29 hybrid `/` symbols
in a small sorted `(vocab id, count)` overflow vec that is empty on ~97% of
cards. Devotion is an always-materialized 6-lane `u64`. Per-lane containment
is a three-op SWAR compare and pip-set equality is integer equality, so a
zero lane and an absent key are the same thing — `mana=`'s distinct-key
semantics fall out for free.

Measured on the real 31,508-card cost distribution
(`cargo test --release bench_mana -- --ignored --nocapture`, parity asserted
on every card × query × op): devotion 21.4 → 0.7 ns/card (~30×), mana
containment 6–10 → 1.7–2.1 ns/card (~4–5×), equality unchanged (~1 ns).
Both filters move to verifier cost tier 1.

Archive format version bumped (ManaCost layout change). Design discussion:
issue #650.
