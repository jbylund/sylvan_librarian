//! Query-regex compilation: accepting the dialect the SQL path accepts.
//!
//! `o:/.../` is documented against PostgreSQL's `~*`
//! (docs/changelog/2025-02-02-regex-search.md), and two things it accepts the
//! `regex` crate does not:
//!
//! - **Lookaround.** `(?!…)`, `(?=…)`, `(?<=…)`, `(?<!…)`. The `regex` crate
//!   omits these by design — they are what costs it its linear-time guarantee.
//!   Lookahead is on the documented feature list.
//! - **Word-boundary escapes.** ARE spells them `\y`/`\Y`/`\m`/`\M`, and ARE's
//!   `\Z` is Rust's `\z`. `\y` and `\Z` have exact `regex`-crate spellings, so
//!   they are rewritten in place; `\m`/`\M` have none and become lookaround.
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
pub(crate) enum CompiledRegex {
    /// The linear-time engine. Every pattern that can be, is.
    Fast(Regex),
    /// The backtracking engine: lookaround and backreferences only.
    Backtrack(Arc<fancy_regex::Regex>),
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
/// Keep this a single `(?…)` group: the strippers match it by literal prefix.
pub(crate) const QUERY_REGEX_FLAGS: &str = "(?im)";

impl CompiledRegex {
    /// Compile a query pattern under [`QUERY_REGEX_FLAGS`].
    ///
    /// The error string is the linear engine's, not the backtracking one's: if
    /// both reject the pattern it is malformed rather than merely non-linear,
    /// and the first message is the one that names the actual syntax problem.
    pub(crate) fn new(pattern: &str) -> Result<Self, String> {
        let translated = translate_are_escapes(pattern);
        let cased = format!("{QUERY_REGEX_FLAGS}{translated}");
        match Regex::new(&cased) {
            Ok(re) => Ok(CompiledRegex::Fast(re)),
            Err(linear_err) => match fancy_regex::RegexBuilder::new(&cased)
                .backtrack_limit(REGEX_BACKTRACK_LIMIT)
                .build()
            {
                Ok(re) => Ok(CompiledRegex::Backtrack(Arc::new(re))),
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
        match self {
            CompiledRegex::Fast(re) => Ok(re.is_match(haystack)),
            CompiledRegex::Backtrack(re) => re.is_match(haystack),
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
        match self {
            CompiledRegex::Fast(re) => re.as_str(),
            CompiledRegex::Backtrack(re) => re.as_str(),
        }
    }

    /// True when this pattern needed the backtracking engine. Cost only.
    #[inline]
    pub(crate) fn is_backtracking(&self) -> bool {
        matches!(self, CompiledRegex::Backtrack(_))
    }
}

/// Rewrite PostgreSQL ARE escapes that the `regex` crate spells differently or
/// cannot spell at all.
///
/// | ARE  | meaning              | rewritten to      |
/// |------|----------------------|-------------------|
/// | `\y` | word boundary        | `\b`              |
/// | `\Y` | not a word boundary  | `\B`              |
/// | `\m` | start of a word      | `(?<!\w)(?=\w)`   |
/// | `\M` | end of a word        | `(?<=\w)(?!\w)`   |
/// | `\Z` | end of string        | `\z`              |
///
/// `\y`/`\Y`/`\Z` have exact equivalents, so a pattern using only those stays
/// on the linear engine. `\m`/`\M` do not, and their lookaround rewrite sends
/// the pattern to the backtracking engine — correct, and rare enough to be
/// worth the access path.
///
/// Bracket expressions are copied through untouched: inside `[…]` these are
/// ordinary escapes, not constraints. A `]` in the first position of a class is
/// literal (POSIX), so it does not close it.
pub(crate) fn translate_are_escapes(pattern: &str) -> String {
    let mut out = String::with_capacity(pattern.len());
    let mut chars = pattern.chars().peekable();
    // Position within the current bracket expression, if any: `Some(n)` means
    // n characters have been consumed since `[`, which is how the leading-`]`
    // rule is applied without a second scan.
    let mut class_pos: Option<usize> = None;

    while let Some(c) = chars.next() {
        if c == '\\' {
            let Some(next) = chars.next() else {
                out.push('\\');
                break;
            };
            if class_pos.is_some() {
                out.push('\\');
                out.push(next);
                class_pos = class_pos.map(|n| n + 2);
                continue;
            }
            match next {
                'y' => out.push_str(r"\b"),
                'Y' => out.push_str(r"\B"),
                'm' => out.push_str(r"(?<!\w)(?=\w)"),
                'M' => out.push_str(r"(?<=\w)(?!\w)"),
                'Z' => out.push_str(r"\z"),
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
                    class_pos = Some(0);
                }
            }
            // A leading `^` negates without occupying the first position, so
            // `[^]…]` gets the same literal-`]` treatment as `[]…]`.
            Some(0) if c == '^' => {}
            // `[]…]`: a `]` in the first position is a literal member.
            Some(0) if c == ']' => class_pos = Some(1),
            Some(_) if c == ']' => class_pos = None,
            Some(n) => class_pos = Some(n + 1),
        }
        out.push(c);
    }
    out
}
