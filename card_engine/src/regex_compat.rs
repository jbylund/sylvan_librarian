//! Query-regex compilation: accepting the dialect the SQL path accepts.
//!
//! `o:/.../` is documented against PostgreSQL's `~*`
//! (docs/changelog/2025-02-02-regex-search.md), and two things it accepts the
//! `regex` crate does not:
//!
//! - **Lookaround.** `(?!…)`, `(?=…)`, `(?<=…)`, `(?<!…)`. The `regex` crate
//!   omits these by design — they are what costs it its linear-time guarantee.
//!   Lookahead is on the documented feature list.
//! - **Word-boundary escapes.** ARE spells them `\y` and `\m`. `\y` has an exact
//!   `regex`-crate spelling, so it is rewritten in place; `\m` has none and
//!   becomes lookaround. (Their uppercase twins `\Y`/`\M`/`\Z` cannot be spelled
//!   in a query: api.scryfall.com lowercases the pattern, and so does `compile`.)
//!
//! And one thing NEITHER dialect has: Scryfall's `\s…` shorthands, which its own docs page
//! calls "not formal character classes, it is just shorthand we have added". They are expanded
//! here — see [`SCRYFALL_SHORTHANDS`].
//!
//! Both were engine *declines* — a `build_filter` error that
//! `_search`'s blanket handler turned into a silent PostgreSQL fallback. That
//! made the SQL path load-bearing for a documented feature rather than a
//! crash net.
//!
//! A pattern the `regex` crate accepts still compiles on it, unchanged. The
//! backtracking engine is entered only where the fast one cannot go, which
//! keeps every existing optimization — most importantly the #734 trigram
//! narrowing, whose `regex_syntax::parse` reads the same pattern string.

use std::sync::Arc;

use regex::Regex;

use super::filter::REGEX_BACKTRACK_LIMIT;

/// A compiled query regex, on whichever engine can express it.
///
/// `Clone` is cheap on both arms: `regex::Regex` is internally `Arc`-based, and
/// the backtracking arm is behind an `Arc` here for the same reason — see
/// `FilterExpr`'s `Clone` note.
#[derive(Clone, Debug)]
enum RegexEngine {
    /// The linear-time engine. Every pattern that can be, is.
    Fast(Regex),
    /// The backtracking engine: lookaround and backreferences only.
    Backtrack(Arc<fancy_regex::Regex>),
}

/// One compiled query pattern, plus the one fact about it the matcher needs on every candidate.
///
/// `self_reference` is CACHED rather than re-derived, and the reason is a cost the fix would
/// otherwise have charged to queries that do not use it: the matcher asks this question once per
/// candidate card, and answering it by scanning the compiled source for the sentinel is a scan of
/// ~250 bytes × the whole corpus on EVERY regex query, `~` or not.
#[derive(Clone, Debug)]
pub(crate) struct CompiledRegex {
    engine: RegexEngine,
    self_reference: bool,
}

/// What this engine writes into a card's text in place of each self-reference before a `~`
/// pattern is matched against it: the tilde itself.
///
/// SCRYFALL KEEPS TWO TEXTS AND THE PATTERN PICKS ONE. A pattern with no `~` is matched against
/// the oracle text as printed. A pattern with a `~` ANYWHERE in it is matched against a second
/// text in which every self-reference has been REPLACED by a literal `~` — and in that pattern
/// the `~` is an ordinary character. Measured on api.scryfall.com 2026-10-04, the `|~~~` arm
/// being a way to put a tilde in the pattern without changing what else it can match:
///
/// | scoped to | pattern | answer | so |
/// |---|---|---|---|
/// | Risk Factor ("…may have Risk Factor deal 4 damage…") | `have risk factor deal` | 1 | the plain text has the name |
/// | | `have risk factor deal\|~~~` | 404 | the tilde text does not |
/// | | `have . deal\|~~~` | 1 | it has ONE character there |
/// | | `have [~] deal` | 1 | which is a tilde, and a class can name it |
/// | | `have [\w ]+ deal\|~~~` | 404 | and it is not a word character |
/// | | `~\b deal` / `have \b~` | 404 | so no boundary stands between it and a space |
/// | Honeymoon Hearse ("…: This Vehicle becomes…") | `this vehicle becomes\|~~~` | 404 | the PHRASES are replaced too |
/// | | `: . becomes\|~~~` | 1 | by the same one character |
/// | Michelangelo, On the Scene ("When Michelangelo dies, return this card…") | `when . dies, return . to\|~~~` | 1 | short name and "this card" alike |
/// | Case of the Market Melee ("When this Case enters…") | `when ~ enters` | 404 | names go first: "Case" is its short name |
/// | | `when this ~ enters` | 1 | so the text reads "this ~" |
///
/// This engine used to keep the phrases in the text and expand the pattern's `~` into an
/// alternation of them plus a private-use LETTER standing for the name. That answered a bare `~`
/// correctly and went wrong wherever a pattern said more: `(~|this \w+|target [\w ]+) deals`
/// crossed the name as if it were a word (Risk Factor, Skullscorch, Case of the Market Melee), and
/// `~ becomes…|This Vehicle becomes an artifact creature` found a phrase Scryfall's text no longer
/// holds (Honeymoon Hearse, Invasion Submersible, Tangle Tumbler).
///
/// The tilde is safe to write into the text because no oracle text holds one (checked over the
/// whole corpus 2026-10-04; flavor text does, twice, and flavor is never substituted).
pub(crate) const SELF_REF_SENTINEL: char = '~';

/// The self-reference phrases `~` aliases, for EVERY card and independent of its own card types.
///
/// Scryfall's docs call `~` "an automatic alias for the current card name or “this spell” if the
/// card mentions itself", which understates it twice over: the phrase family is much wider than
/// "this spell", and it is not conditioned on the card's own type. Both halves are measured, and
/// the reason it is worth measuring at all is the size of the gap — a NAME-ONLY reading of `~`
/// answers 3,046 against the real 19,228, so five matches in six come through a phrase
/// (`o:/~/ -o:/this/` is 3,046).
///
/// TYPE-INDEPENDENCE, three instants and sorceries that never name themselves and whose only
/// self-reference-shaped text is inside an ability they GRANT to something else — all three match
/// `o:/~/` on api.scryfall.com 2026-08-28: Full Steam Ahead, Martyrdom, Storm the Citadel. So the
/// expansion is a fixed alternation, not a per-card choice keyed to the card's types.
///
/// THE MEMBERSHIP, one probe per phrase against a card whose text contains that phrase and
/// nothing else self-referential (`!"<card>" o:/~/`, 2026-08-28). In: creature (Kor Outfitter),
/// spell (Altar's Reap), land (Orzhov Guildgate), artifact (Midnight Clock), enchantment
/// (Beastmaster Ascension), card (Bone Dragon), aura (Psychic Venom), token (Rite of the Raging
/// Storm), equipment (Gate Smasher), vehicle (Voyager Glidecar), permanent (Hidden Stag), saga
/// (The War Games), siege (Invasion of Tolvada), class (Rogue Class), spacecraft (Uthros
/// Scanship), case (Case of the Filched Falcon).
///
/// OUT, and each of these is a card that does NOT match: turn (Surge of Brilliance), way (Mulch),
/// ability (Crown of Gondor), mana (Eldrazi Temple), combat (Neyith of the Dire Hunt), one
/// (Temporal Manipulation), effect (Edgewalker), process (Professor Onyx), phase (Najeela, the
/// Blade-Blossom), game (Commander's Insignia), only (Ondu Spiritdancer), step (Y'shtola Rhul),
/// main (Aggravated Assault), and — the one that says this list is hand-maintained upstream
/// rather than derived from the type system — DOOR (Ticket Booth // Tunnel of Hate). Rooms say
/// "when you unlock this door" and Scryfall does not count it.
///
/// Possessives need no entry of their own: `\bthis creature\b` matches inside "this creature's"
/// because `'` is not a word character, and Howlgeist ("this creature's") confirms it.
///
/// CONTRAPTION and ATTRACTION were the last two in, and they are worth their own paragraph
/// because they are the largest single block the phrase family carries: the Un-set assembled
/// permanents say "Whenever you crank this Contraption" and "When you visit this Attraction", and
/// nothing else in their text is self-referential. Counted whole rather than sampled, on
/// api.scryfall.com 2026-08-28 — `o:/this contraption/` is 45 and `o:/this contraption/ o:/~/` is
/// the same 45; `o:/this attraction/` is 10 and `o:/this attraction/ o:/~/` is the same 10. Every
/// card the phrase reaches is a card `~` reaches, which is what makes them members rather than a
/// coincidence, and the two probes in the format above are Arms Depot and Ferris Wheel, both 1.
/// Their absence was 47 of the 53 names `o:/~/` missed here.
pub(crate) const SELF_REF_THIS_PHRASES: &[&str] = &[
    "creature", "spell", "land", "artifact", "enchantment", "card", "aura", "token", "equipment",
    "vehicle", "permanent", "saga", "siege", "class", "spacecraft", "case", "contraption",
    "attraction",
];

/// Whether a `~` in the pattern is the self-reference, which the COLUMN decides.
#[derive(Clone, Copy, PartialEq, Debug)]
pub(crate) enum SelfRefScope {
    /// Not the alias: `~` is the literal tilde, matched against the text as stored.
    ///
    /// EVERY COLUMN BUT THE TWO ORACLE ONES, and `ft:` is the one that took two measurements to
    /// place. `name:/~/`, `t:/~/` and `mana:/~/` are all 404 on api.scryfall.com (2026-08-28) —
    /// `name:` could not be, if `~` were the card's name there. `ft:/~/` is 2, which looks like
    /// an alias answering thinly and is not: the two cards are Blighted Agent and Urabrask the
    /// Hidden, whose Phyrexian-script flavor text contains a literal `~`, and the plain substring
    /// `ft:"~"` returns the same two. The phrase family confirms it from the other side —
    /// `ft:/this creature/` is 6 and `ft:/this creature/ -ft:/~/` is the same 6, so not one of
    /// those six matches. Expanding names on flavor answers 680 against 2.
    None,
    /// Rules text: the card's names AND the "this <noun>" phrase family. `o:/~/` 19,228 here and
    /// on api.scryfall.com — card for card, verified by diffing the full id sets both ways
    /// (2026-08-28). `fo:/~/` is 22,045 here against 22,037 there, and that gap is the THIRD
    /// deliberate non-reproduction in this dialect.
    ///
    /// `fo:` keeps the reminder text `o:` strips, so it can only ever match MORE. Scryfall breaks
    /// that: `o:/~/ -fo:/~/` is 8 there and 0 here, eight cards matching the stripped column and
    /// not the full one. Turn the Tide is the one that settles it — its oracle text is "Creatures
    /// your opponents control get -2/-0 until end of turn." with no parenthetical anywhere, so its
    /// two columns hold the SAME STRING and Scryfall still answers 1 and 0. The other seven are
    /// Choice of Damnations, Flashback, For the Common Good, Hand of Vecna, Library of Lat-Nam,
    /// Library of Leng and Turn the Tables, each matching on a short name or its own full name.
    ///
    /// The direction that makes sense agrees exactly: `fo:/~/ -o:/~/` is 2,817 on both.
    Oracle,
}

/// Lowercase a query pattern, which is the first thing api.scryfall.com does to it.
///
/// SCRYFALL DOWNCASES THE WHOLE QUERY BEFORE IT PARSES ANYTHING — its `next_page` echoes the
/// lowercased `q` — so a regex reaches its engine with every letter folded, the letter after a
/// backslash included. For the literals that is invisible (the match is case-insensitive anyway).
/// For the escapes it is a change of MEANING, because an uppercase class escape is the negation of
/// its lowercase twin: `\S` becomes `\s`, and "anything, across lines" (`[\s\S]*`) becomes
/// "whitespace only" (`[\s\s]*`).
///
/// MEASURED on api.scryfall.com 2026-10-04, each scoped `!"Fierce Retribution"` ("Cleave {5}{W}",
/// then "Destroy target [attacking] creature." on the next line), so the answer is 1 or 404:
///
/// | pattern                    | if the escape kept its case | Scryfall | reads as      |
/// |----------------------------|-----------------------------|----------|---------------|
/// | `destroy\Starget`          | 404 (a space is not `\S`)   | 1        | `\s`          |
/// | `destroy[\S]target`        | 404                         | 1        | `[\s]`        |
/// | `destroy[^\S]target`       | 1                           | 404      | `[^\s]`       |
/// | `destroy\Wtarget`          | 1 (a space is `\W`)         | 404      | `\w`          |
/// | `destr\Dy`                 | 1 (`o` is `\D`)             | 404      | `\d`          |
/// | `destro\By`                | 1 (no boundary inside)      | 404      | `\b`          |
/// | `destro\Yy`                | 1                           | 404      | `\y`          |
/// | `destroy\M target`         | 1 (end of a word)           | 404      | `\m`          |
/// | `\Acleave`                 | 1 (start of text)           | 404      | `\a`          |
/// | `destroy\X20target`        | an error                    | 1        | `\x20`        |
/// | `destroy[[:SPACE:]]target` | an error                    | 1        | `[[:space:]]` |
///
/// The same holds on every regex column (`name:/Fierce\Sretribution/` is 1). This engine kept
/// the escapes' case, on purpose, from 2026-08-28 until 2026-10-04, and what that cost is a query
/// written here and run on Scryfall answering differently wherever it said `[\s\S]*`.
/// `o:/Enchant permanent[\s\S]*You control enchanted/` reached Dream Leash and Volition Reins
/// across the line between, where Scryfall's `[\s\s]*` stops at the first letter, and
/// `o:/(?:a|opponent.s|single) graveyard[\s\S]*copy[\s\S]*you may cast/` answered 9 cards against
/// a 404. Keeping `\S` "right" is a regex that means one thing here and another where the user
/// runs it.
///
/// `\A` folds to `\a`, the BEL character on both engines, so it matches nothing; `\Z` folds to
/// `\z`, which Scryfall rejects ("invalid escape \\ sequence", the term ignored with a warning)
/// and this engine reads as end-of-text.
///
/// Borrows when there is nothing to fold, which is the common case.
pub(crate) fn fold_query_case(pattern: &str) -> std::borrow::Cow<'_, str> {
    if pattern.chars().any(char::is_uppercase) {
        std::borrow::Cow::Owned(pattern.to_lowercase())
    } else {
        std::borrow::Cow::Borrowed(pattern)
    }
}

/// The inline flags every query regex is compiled with, and the exact prefix the two callers that
/// read a compiled pattern back (`regex_tier`, `regex_required_factors`) strip before parsing it.
///
/// `i` is the `~*` operator the SQL path uses. `m` makes `^` and `$` match at every line boundary
/// rather than only at the ends of the string, which is what Scryfall does — measured against
/// api.scryfall.com on 2026-08-16, `o:/^Whenever you cast/ e:khm` returns Firja, Judge of Valor
/// (khm/209), whose oracle text is `"Flying, lifelink\nWhenever you cast your second spell each
/// turn, …"`, and `o:/lifelink$/ e:khm` returns it too. Oracle text is the only multi-line column,
/// so this changes nothing on the single-line ones (name, type line, artist, set code).
///
/// It does NOT turn on `s`: `.` still stops at a newline, verified the same way
/// (`o:/Flying.Whenever/ e:khm` is empty on Scryfall while `o:/Flying\nWhenever/ e:khm` is not).
/// Together that is exactly PostgreSQL ARE's newline-sensitive mode — the SQL path spells the
/// same pair `(?n)` — so the two paths still accept and answer one dialect.
///
/// THAT MODE HAS A THIRD LEG NO FLAG HERE CAN SPELL: a NEGATED bracket expression never matches a
/// newline either. The `regex` crate's `[^.]` does, so `translate_query_escapes` writes the
/// newline into every negated class — see `NEGATED_CLASS_NEWLINE`.
///
/// Keep this a single `(?…)` group: the strippers match it by literal prefix.
pub(crate) const QUERY_REGEX_FLAGS: &str = "(?im)";

impl CompiledRegex {
    /// Compile a query pattern under [`QUERY_REGEX_FLAGS`].
    ///
    /// The error string is the linear engine's, not the backtracking one's: if
    /// both reject the pattern it is malformed rather than merely non-linear,
    /// and the first message is the one that names the actual syntax problem.
    pub(crate) fn new(pattern: &str) -> Result<Self, String> {
        Self::compile(pattern, SelfRefScope::None)
    }

    /// Compile a pattern whose `~` is the self-reference: see [`SELF_REF_SENTINEL`].
    ///
    /// A SEPARATE ENTRY POINT BECAUSE THE COLUMN DECIDES, and Scryfall's answer says so out loud:
    /// `name:/~/` is 404 on api.scryfall.com (2026-08-28). If `~` were the card's name there, that
    /// query would be the whole corpus; it is nothing, so the alias is simply not expanded on
    /// `name:`. `t:/~/` and `mana:/~/` are 404 too — vacuously, since no type line or mana cost
    /// carries a self-reference — and on all three `~` stays the literal tilde no such field
    /// contains. The columns that DO expand it are the rules-text ones: `o:/~/` 19,228, `fo:/~/`
    /// 22,037 (the difference being reminder text, which `fo:` keeps).
    pub(crate) fn new_self_referential(pattern: &str, scope: SelfRefScope) -> Result<Self, String> {
        Self::compile(pattern, scope)
    }

    fn compile(pattern: &str, scope: SelfRefScope) -> Result<Self, String> {
        // Folded FIRST, before the alias or any escape is read — see `fold_query_case`.
        let folded = fold_query_case(pattern);
        // A `~` ANYWHERE in the pattern selects the substituted text — bare, escaped or inside a
        // bracket expression (`o:/have [~] deal/` is 1 on Risk Factor). The pattern itself is not
        // rewritten: the tilde is a literal, and the text is what changes. A pattern without one
        // (`o:/draw/` asked for the alias and has none) pays nothing and keeps the narrow.
        let self_reference = scope != SelfRefScope::None && folded.contains(SELF_REF_SENTINEL);
        let translated = translate_query_escapes(&folded);
        let cased = format!("{QUERY_REGEX_FLAGS}{translated}");
        match Regex::new(&cased) {
            Ok(re) => Ok(CompiledRegex { engine: RegexEngine::Fast(re), self_reference }),
            // `.seek(true)`. Without it fancy_regex searches by trying the VM at EVERY character
            // of the haystack, and each failed attempt costs one backtrack per top-level
            // alternative — so the budget measured the LENGTH OF THE TEXT, not the pattern.
            // `(?<!any )(number of|for each)…|…|…|…` spends over five per character and exhausts
            // 8,192 on any text past ~1,450 characters. The longest rules text in this corpus is
            // 777 (Ral, Monsoon Mage), so that pattern stayed under; sixteen alternatives behind
            // the same lookbehind do not, and the query is then refused for the length of a card
            // it could never have matched.
            //
            // With it, fancy_regex derives the pattern's lookaround-free over-approximation
            // (lookarounds dropped, backreferences inlined), finds candidate positions with that
            // on the LINEAR engine, and runs the VM only there. A text with no candidate costs no
            // budget at all and no VM entry, which is nearly every card. A pattern with nothing to
            // seek on (`(?=a)(?=b)`) keeps the old search.
            Err(linear_err) => match fancy_regex::RegexBuilder::new(&cased)
                .backtrack_limit(REGEX_BACKTRACK_LIMIT)
                .seek(true)
                .build()
            {
                Ok(re) => Ok(CompiledRegex { engine: RegexEngine::Backtrack(Arc::new(re)), self_reference }),
                Err(_) => Err(format!("invalid regex '{pattern}': {linear_err}")),
            },
        }
    }

    /// Does this pattern match anywhere in `haystack`, budget permitting?
    ///
    /// The linear arm cannot fail — it has no step budget to exhaust — so the
    /// `Err` case is reachable only from a lookaround or backreference pattern
    /// that ran past [`REGEX_BACKTRACK_LIMIT`]. `filter::regex_is_match` is the
    /// caller that turns that into the query-level `UnsupportedRegexError`
    /// rather than a silent non-match; see its note.
    #[inline]
    pub(crate) fn try_is_match(&self, haystack: &str) -> Result<bool, fancy_regex::Error> {
        match &self.engine {
            RegexEngine::Fast(re) => Ok(re.is_match(haystack)),
            RegexEngine::Backtrack(re) => re.is_match(haystack),
        }
    }

    /// [`try_is_match`](Self::try_is_match) with an exhausted budget read as
    /// "no match".
    ///
    /// For callers outside a query's execution — tests, and anywhere the
    /// failure latch is not being read afterwards.
    #[inline]
    pub(crate) fn is_match(&self, haystack: &str) -> bool {
        self.try_is_match(haystack).unwrap_or(false)
    }

    /// The compiled pattern source, [`QUERY_REGEX_FLAGS`] prefix included.
    ///
    /// Feeds `regex_tier` (cost) and the #734 literal-factor extraction. The
    /// latter parses this with `regex_syntax`, which fails on a backtracking
    /// pattern and yields no factors — so those patterns lose the trigram
    /// narrow and scan, which is correct, just not fast.
    pub(crate) fn as_str(&self) -> &str {
        match &self.engine {
            RegexEngine::Fast(re) => re.as_str(),
            RegexEngine::Backtrack(re) => re.as_str(),
        }
    }

    /// True when this pattern needed the backtracking engine. Cost only.
    #[inline]
    pub(crate) fn is_backtracking(&self) -> bool {
        matches!(self.engine, RegexEngine::Backtrack(_))
    }

    /// True when this pattern holds a `~` on an oracle column, so matching it needs the per-card
    /// substitution — and so the #734 trigram narrow must decline.
    #[inline]
    pub(crate) fn has_self_reference(&self) -> bool {
        self.self_reference
    }
}

/// One mana symbol, as Scryfall's `\sm` shorthand means it.
///
/// MEASURED, not derived from the symbology table: every alternative below is a card the corpus
/// actually holds, and the whole expression was checked by asking api.scryfall.com for BOTH counts
/// on 2026-08-28 — `o:/\sm/` and `o:/<this>/` are 11,057 apiece, corpus-wide.
///
/// Four alternatives exist only because a probe found the card that needs them, and each is one
/// card wide: `{½}` (Cheap Ass), `{H}` — Scryfall's spelling of the generic Phyrexian symbol
/// (Rage Extractor) — `{HR}`/`{HW}` half-mana (Mons's Goblin Waiters), and `{P}`, which is
/// Bloomburrow's PAWPRINT and not Phyrexian at all (the five `Season of …` cards). The last is why
/// `\smp` below cannot simply reuse this: Scryfall counts `{P}` as a mana symbol and NOT as a
/// Phyrexian one, contradicting its own docs page, which offers `{P}` as a `\smp` example.
const MANA_SYMBOL: &str = r"\{(?:[0-9]+|[wubrgcsxyz]|[^{}]*/[^{}]*|h[wubrg]?|p|½)\}";

/// The same vocabulary MINUS the bare `{P}`, which is what `\smr` repeats over.
///
/// Scryfall's `\sm` counts the Bloomburrow PAWPRINT as a mana symbol and its two DERIVED
/// shorthands do not: `\smp` excludes it (42, not 47) and so does `\smr`. `o:/\smr/` is 1,189 on
/// api.scryfall.com (2026-08-28); reusing `MANA_SYMBOL` here answers 1,194, and the five extras
/// are exactly the `Season of …` cards, whose `{P}{P} —` mode lines are a repeated pawprint and
/// nothing else.
const REPEATABLE_MANA_SYMBOL: &str = r"\{(?:[0-9]+|[wubrgcsxyz]|[^{}]*/[^{}]*|h[wubrg]?|½)\}";

/// Scryfall's non-standard regex shorthands, as `(suffix after \s, expansion)`.
///
/// <https://scryfall.com/docs/regular-expressions> documents these as "not formal character
/// classes, it is just shorthand we have added", and they are the reason a `\s` in a query regex
/// cannot be read as whitespace without looking at what follows it. THE FAILURE IS SILENT:
/// `o:/\smp/` answers 42 on api.scryfall.com and answers ZERO under a whitespace reading, because
/// no oracle text contains whitespace followed by "mp" — and `o:/\sm/`, worse, answers a plausible
/// 10,791 against Scryfall's 11,057, a wrong number that looks like a right one.
///
/// EVERY EXPANSION IS A MEASURED EQUALITY, established by asking api.scryfall.com for the count of
/// the shorthand and the count of the expansion and requiring them to agree, corpus-wide,
/// 2026-08-28:
///
/// | shorthand | means                        | count  | expansion agrees |
/// |-----------|------------------------------|--------|------------------|
/// | `\ss`     | any card symbol              | 12,446 | yes              |
/// | `\sm`     | any mana symbol              | 11,057 | yes              |
/// | `\sc`     | any COLORED mana symbol      |  6,676 | yes              |
/// | `\smh`    | any hybrid card symbol       |    172 | yes              |
/// | `\smp`    | any Phyrexian card symbol    |     42 | yes              |
/// | `\smr`    | any REPEATED mana symbol     |  1,189 | see below        |
/// | `\spt`    | an X/X power/toughness       |  3,185 | yes              |
/// | `\spp`    | a +X/+X                      |  7,160 | yes              |
/// | `\smm`    | a -X/-X                      |    841 | yes              |
///
/// `\sc` excludes the half-mana symbols and `\smh` excludes the MONOCOLOR Phyrexian ones, both
/// measured rather than assumed: `\{[^{}]*[wubrg][^{}]*\}` is 6,677 against `\sc`'s 6,676 (the
/// extra is `{HR}`), and every symbol carrying a `/` is 213 against `\smh`'s 172 (the 41
/// difference is exactly `o:/\/p}/`, the `{X/P}` cards).
///
/// LONGEST MATCH. `\smm` is the -X/-X shorthand and not `\sm` followed by a literal `m`, and the
/// same holds for `\smr`/`\smh`/`\smp` against `\sm`. Scryfall reads them the same way and its
/// choice is observable: `o:/\smana/` is 404 there — `\sm` then "ana" — where a whitespace reading
/// answers 2,784, the count for whitespace followed by "mana".
///
/// Each expansion is wrapped in `(?:…)` so a quantifier binds to the whole shorthand: `\sm{2}` is
/// two mana symbols, not one symbol whose closing brace repeats.
const SCRYFALL_SHORTHANDS: &[(&str, &str)] = &[
    // Three characters first, so the longest match wins.
    ("mh", r"(?:\{(?:[^{}]*/[^{}]*/[^{}]*|[^{}]*/[^{}p])\})"),
    ("mp", r"(?:\{(?:[^{}]*/p|h)\})"),
    ("mm", r"(?:-[0-9x*]+/-[0-9x*]+)"),
    ("pt", r"(?:[0-9x*]+/[0-9x*]+)"),
    ("pp", r"(?:\+[0-9x*]+/\+[0-9x*]+)"),
    ("s", r"(?:\{[^{}]*\})"),
    ("c", r"(?:\{[0-9wubrgcpxyz/½]*[wubrg][0-9wubrgcpxyz/½]*\})"),
];

/// What a negated bracket expression is opened with, in place of the bare `[^`.
///
/// A NEGATED CLASS NEVER MATCHES A NEWLINE on api.scryfall.com, which is the third leg of
/// PostgreSQL ARE's newline-sensitive mode (`.` and `^`/`$` are the two [`QUERY_REGEX_FLAGS`]
/// carries) and the one the `regex` crate has no flag for: its `[^.]` is "anything but a full
/// stop", line break included. So `o:/you control enters tapped, [^.]*untap/` reached across the
/// break in Tiller Engine — "Whenever a land you control enters tapped, choose one —\n• Untap that
/// land.\n• Tap target nonland permanent an opponent controls." — and answered 2 cards against
/// Scryfall's 1 (Amulet of Vigor), and `fo:/whenever you (cycle or )?discard[^.]*draw a card/`
/// answered Monument to Endurance beside Bone Miser where Scryfall answers Bone Miser alone.
///
/// MEASURED 2026-10-03, one variable per probe, each scoped `!"Tiller Engine"` so the answer is 1
/// or 404, the pattern being `choose one .X. untap` with X the one character that has to be the
/// line break (the `.`s take the dash before it and the bullet after):
///
/// | X               | Scryfall | here, before |
/// |-----------------|----------|--------------|
/// | `\n`            | 1        | 1            |
/// | `.`             | 404      | 404          |
/// | `[^x]`          | 404      | 1            |
/// | `[^a-z]`        | 404      | 1            |
/// | `[^[:alpha:]]`  | 404      | 1            |
/// | `\s`            | 1        | 1            |
/// | `[\s\S]`        | 1        | 1            |
/// | `[\n]`          | 1        | 1            |
/// | `[[:space:]]`   | 1        | 1            |
/// | `(.\|\n)`       | 1        | 1            |
///
/// and `[^x]{3}` in place of all three is 404 too. So the rule is about NEGATION and nothing
/// else: a positive class that names the newline, by itself or through `\s` or `[:space:]`, still
/// matches it, and it is the line break and not the `—`/`•` around it (`[^.]*one` on the same
/// line is 1). The flavor column obeys the same rule — `ft:/\."[^x]—marianne/` on LEA Dragon
/// Whelp, whose attribution sits on its own line, is 404 where `\s` in that position is 1.
///
/// Written INTO the class rather than wrapped around it: `(?:(?!\n)[^.])` would say the same
/// thing through a lookahead, which is the backtracking engine and no trigram narrow. A class
/// with one more member is still a class, on the linear engine, at no cost per candidate.
const NEGATED_CLASS_NEWLINE: &str = r"[^\n";

/// Rewrite PostgreSQL ARE escapes that the `regex` crate spells differently or
/// cannot spell at all, and keep a negated bracket expression off the newline
/// ([`NEGATED_CLASS_NEWLINE`]).
///
/// | ARE  | meaning              | rewritten to      |
/// |------|----------------------|-------------------|
/// | `\y` | word boundary        | `\b`              |
/// | `\m` | start of a word      | `(?<!\w)(?=\w)`   |
///
/// `\y` has an exact equivalent, so a pattern using only that stays on the linear engine. `\m`
/// does not, and its lookaround rewrite sends the pattern to the backtracking engine — correct,
/// and rare enough to be worth the access path. ARE's three UPPERCASE constraints (`\Y` not a
/// boundary, `\M` end of a word, `\Z` end of string) have no row because no query can spell them:
/// the pattern is lowercased before it is translated, see [`fold_query_case`].
///
/// The `\s…` half is [`SCRYFALL_SHORTHANDS`] plus `\smr`, which is the one shorthand no static
/// expansion can express: "the SAME mana symbol twice" needs a backreference, so it compiles a
/// named group and `\k<…>` and therefore lands on `fancy_regex` — losing the #734 trigram narrow
/// along with it. Every other shorthand stays on the linear engine, and the group is NAMED (and
/// numbered per occurrence) so it cannot collide with a capture the user wrote.
///
/// Bracket expressions are copied through untouched: inside `[…]` these are
/// ordinary escapes, not constraints. A `]` in the first position of a class is
/// literal (POSIX), so it does not close it. Scryfall does NOT skip classes — `o:/[\sm]/` comes
/// back "parentheses () not balanced" there, its own substitution having broken the class — and
/// reproducing that particular bug would turn a query that reads perfectly well ("whitespace or
/// the letter m") into an error.
///
/// THE INPUT IS ALREADY LOWERCASE — `compile` folds the pattern first ([`fold_query_case`]), so no
/// uppercase escape reaches this function from a query: `\S` arrives as `\s`, `\Y` as `\y`, `\M`
/// as `\m`, `\Z` as `\z`. The shorthands are therefore read after the fold, as Scryfall reads
/// them: `\Sm` is a mana symbol there and here.
pub(crate) fn translate_query_escapes(pattern: &str) -> String {
    let chars: Vec<char> = pattern.chars().collect();
    let mut out = String::with_capacity(pattern.len());
    // Position within the current bracket expression, if any: `Some(n)` means
    // n characters have been consumed since `[`, which is how the leading-`]`
    // rule is applied without a second scan.
    let mut class_pos: Option<usize> = None;
    // Distinguishes the capture groups two `\smr`s in one pattern would otherwise share.
    let mut smr_seq = 0usize;
    let mut i = 0usize;

    while i < chars.len() {
        let c = chars[i];
        if c == '\\' {
            i += 1;
            let Some(&next) = chars.get(i) else {
                out.push('\\');
                break;
            };
            i += 1;
            if class_pos.is_some() {
                out.push('\\');
                out.push(next);
                class_pos = class_pos.map(|n| n + 2);
                continue;
            }
            if next == 's' {
                if chars.get(i) == Some(&'m') && chars.get(i + 1) == Some(&'r') {
                    out.push_str(&format!("(?:(?<smr{smr_seq}>{REPEATABLE_MANA_SYMBOL})\\k<smr{smr_seq}>)"));
                    smr_seq += 1;
                    i += 2;
                    continue;
                }
                if chars.get(i) == Some(&'m') && !matches!(chars.get(i + 1), Some('h' | 'p' | 'm')) {
                    out.push_str(&format!("(?:{MANA_SYMBOL})"));
                    i += 1;
                    continue;
                }
                if let Some((suffix, expansion)) = SCRYFALL_SHORTHANDS
                    .iter()
                    .find(|(suffix, _)| suffix.chars().enumerate().all(|(k, sc)| chars.get(i + k) == Some(&sc)))
                {
                    out.push_str(expansion);
                    i += suffix.chars().count();
                    continue;
                }
            }
            match next {
                'y' => out.push_str(r"\b"),
                'm' => out.push_str(r"(?<!\w)(?=\w)"),
                other => {
                    out.push('\\');
                    out.push(other);
                }
            }
            continue;
        }

        match class_pos {
            None => {
                if c == '[' {
                    if chars.get(i + 1) == Some(&'^') {
                        // A leading `^` negates without occupying the first position, so
                        // `[^]…]` gets the same literal-`]` treatment as `[]…]` — and the
                        // newline written in after it WOULD occupy that position, turning a
                        // literal `]` into the class's close and a literal `-` into a range
                        // from the newline up. Both are escaped so they stay the members
                        // they were.
                        out.push_str(NEGATED_CLASS_NEWLINE);
                        i += 2;
                        class_pos = Some(0);
                        if let Some(&first @ (']' | '-')) = chars.get(i) {
                            out.push('\\');
                            out.push(first);
                            i += 1;
                            class_pos = Some(1);
                        }
                        continue;
                    }
                    class_pos = Some(0);
                }
            }
            // `[]…]`: a `]` in the first position is a literal member.
            Some(0) if c == ']' => class_pos = Some(1),
            Some(_) if c == ']' => class_pos = None,
            Some(n) => class_pos = Some(n + 1),
        }
        out.push(c);
        i += 1;
    }
    out
}
