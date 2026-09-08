// Legalities pack into a u64: 2 bits per format, positions handed out append-only
// by a global registry the first time a format name appears in loaded data, so
// bit assignments stay stable across reloads and engine instances. A format the
// card's JSONB omits reads as not_legal. 32 formats fit; Scryfall ships 22.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering as AtomicOrdering};
use std::sync::{Arc, OnceLock, RwLock};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use rkyv::Archived;

const LEGALITY_NOT_LEGAL: u64 = 0;
pub(crate) const LEGALITY_LEGAL: u64 = 1;
pub(crate) const LEGALITY_RESTRICTED: u64 = 2;
pub(crate) const LEGALITY_BANNED: u64 = 3;
pub(crate) const MAX_FORMATS: usize = 32;

static FORMAT_SHIFTS: OnceLock<RwLock<HashMap<String, u8>>> = OnceLock::new();

pub(crate) fn format_shifts() -> &'static RwLock<HashMap<String, u8>> {
    FORMAT_SHIFTS.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Bumped by every writer of `FORMAT_SHIFTS`; what tells a cached order it is stale.
static FORMAT_GENERATION: AtomicU64 = AtomicU64::new(0);

type FormatOrder = Arc<Vec<(String, u8)>>;

fn format_order_cell() -> &'static RwLock<(u64, FormatOrder)> {
    static CELL: OnceLock<RwLock<(u64, FormatOrder)>> = OnceLock::new();
    // Generation 1 against FORMAT_GENERATION's 0, so the initial empty value never reads as fresh.
    CELL.get_or_init(|| RwLock::new((u64::MAX, Arc::new(Vec::new()))))
}

/// The registry's `(format, shift)` pairs, alphabetical, built once per registry change.
///
/// `legality_bits_to_pydict` is a FIELD_TABLE extractor, so it runs ONCE PER ROW. It used to build
/// this vector itself every time: a read lock, a clone of all 22 format names, and a sort, to
/// decode a word that is a pure function of one `u64` and orders identically for every card in the
/// store. A 175-card page of /cards/search -- which asks for `legalities` on every card object --
/// therefore paid 175 locks, 175 sorts and ~3,850 String allocations to produce 175 copies of one
/// answer. `fields=legalities` on /search (#877) pays it per row too.
///
/// The registry only grows, and only on import or archive attach, so the sorted form is cached and
/// invalidated by generation rather than by lock discipline: a reader never blocks a writer, and a
/// rebuild that races a write is DISCARDED rather than published, so a stale order cannot be
/// served. Worst case is a redundant rebuild.
fn format_order() -> FormatOrder {
    let generation = FORMAT_GENERATION.load(AtomicOrdering::Acquire);
    if let Ok(cached) = format_order_cell().read()
        && cached.0 == generation
    {
        return Arc::clone(&cached.1);
    }
    let mut entries: Vec<(String, u8)> = match format_shifts().read() {
        Ok(shifts) => shifts.iter().map(|(k, v)| (k.clone(), *v)).collect(),
        Err(_) => Vec::new(),
    };
    entries.sort();
    let built: FormatOrder = Arc::new(entries);
    if let Ok(mut slot) = format_order_cell().write() {
        // Only publish if the registry did not move while we were building it.
        if FORMAT_GENERATION.load(AtomicOrdering::Acquire) == generation {
            *slot = (generation, Arc::clone(&built));
        }
    }
    built
}

/// Mark the cached order stale. Called by every writer of `FORMAT_SHIFTS`, after it releases the
/// write lock -- never while holding it, so the two locks are never held at once in either order.
fn invalidate_format_order() {
    FORMAT_GENERATION.fetch_add(1, AtomicOrdering::AcqRel);
}

/// Bit shift for a format already seen in loaded data; None matches nothing.
pub(crate) fn format_shift(format: &str) -> Option<u8> {
    format_shifts().read().ok()?.get(format).copied()
}

/// Bit shift for a format, assigning the next free slot if unseen (reload path).
pub(crate) fn format_shift_or_assign(format: &str) -> Option<u8> {
    if let Some(shift) = format_shift(format) {
        return Some(shift);
    }
    let mut shifts = format_shifts().write().ok()?;
    if let Some(&shift) = shifts.get(format) {
        return Some(shift); // assigned while we waited for the write lock
    }
    if shifts.len() >= MAX_FORMATS {
        return None;
    }
    let shift = (shifts.len() * 2) as u8;
    shifts.insert(format.to_string(), shift);
    drop(shifts);
    invalidate_format_order();
    Some(shift)
}

fn legality_code(status: &str) -> u64 {
    match status {
        "legal"      => LEGALITY_LEGAL,
        "restricted" => LEGALITY_RESTRICTED,
        "banned"     => LEGALITY_BANNED,
        _            => LEGALITY_NOT_LEGAL,
    }
}

pub(crate) fn jsonb_obj_to_legality_bits(d: &Bound<PyDict>, key: &str) -> u64 {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| {
            v.cast::<PyDict>().ok().map(|m| {
                m.iter()
                    .filter_map(|(k, v)| {
                        let format = k.extract::<String>().ok()?;
                        let status = v.extract::<String>().ok()?;
                        let shift = format_shift_or_assign(&format)?;
                        Some(legality_code(&status) << shift)
                    })
                    .fold(0u64, |bits, b| bits | b)
            })
        })
        .unwrap_or_default()
}

/// Decode a packed legality word into a `{format: status}` Python dict covering every
/// format the registry knows, alphabetically — the field-extraction counterpart of
/// `jsonb_obj_to_legality_bits`. A format absent from the imported JSONB round-trips
/// as "not_legal", exactly as the encoder treated it.
/// The format names as interned `PyString` keys, parallel to a `FormatOrder` snapshot.
type FormatKeys = Arc<[Py<PyString>]>;

/// The format names as interned `PyString` keys, positionally parallel to a
/// `format_order()` snapshot of the same length.
///
/// Keyed on the snapshot's LENGTH rather than `FORMAT_COUNT` on purpose. The registry is
/// append-only in its *shift* assignments, but `format_order()` is sorted
/// ALPHABETICALLY, so a new format lands in the middle and moves every later entry's index.
/// Positional correspondence therefore only holds against a snapshot of the same length --
/// and because the registry is append-only, a given length pins a unique format set and so a
/// unique sorted order. Matching lengths is exactly the condition under which `keys[i]`
/// describes `entries[i]`.
fn format_keys(py: Python<'_>, entries: &FormatOrder) -> FormatKeys {
    static KEYS: OnceLock<RwLock<(usize, FormatKeys)>> = OnceLock::new();
    let cache = KEYS.get_or_init(|| RwLock::new((usize::MAX, Arc::from([] as [Py<PyString>; 0]))));

    if let Ok(guard) = cache.read()
        && guard.0 == entries.len()
    {
        return guard.1.clone();
    }
    let Ok(mut guard) = cache.write() else { return Arc::from([]) };
    if guard.0 == entries.len() {
        return guard.1.clone(); // rebuilt by another thread while we waited for the write lock
    }
    let built: FormatKeys =
        entries.iter().map(|(format, _)| PyString::intern(py, format.as_str()).unbind()).collect();
    *guard = (entries.len(), built.clone());
    built
}

/// Cap on distinct legality words held as template dicts.
///
/// The real corpus has 591 distinct combinations across 97,812 printings, so this is ~7x headroom
/// and exists only so a pathological corpus cannot grow the map without bound. Past the cap the
/// builder still returns correct dicts, just uncached.
const MAX_CACHED_LEGALITY_WORDS: usize = 4096;

/// Template dicts by legality word, valid for a `FormatOrder` snapshot of the recorded length.
type LegalityTemplates = (usize, HashMap<u64, Py<PyDict>>);

fn legality_templates() -> &'static RwLock<LegalityTemplates> {
    static DICTS: OnceLock<RwLock<LegalityTemplates>> = OnceLock::new();
    DICTS.get_or_init(|| RwLock::new((usize::MAX, HashMap::new())))
}

/// Build one row's `{format: status}` dict.
///
/// The whole dict is memoized on the legality word, not just its pieces: the corpus has 591
/// distinct combinations over 97,812 printings, and `{format: status}` is a pure function of the
/// word and the format snapshot. Rebuilding it per row costs 23 dict inserts; copying a template
/// costs one `PyDict_Copy`, measured at 64 ns against 429 ns to rebuild.
///
/// A COPY, not the template itself. Returning the shared dict would be a further ~20x, but two rows
/// with the same legalities would then be the same object: a caller mutating one row's dict would
/// silently change every other row carrying that word, and corrupt the template for the rest of the
/// process. Each row keeping its own mutable dict is the behavior callers have today.
pub(crate) fn legality_bits_to_pydict<'a>(py: Python<'a>, bits: u64) -> PyResult<pyo3::Bound<'a, PyDict>> {
    let entries = format_order();

    if let Ok(guard) = legality_templates().read()
        && guard.0 == entries.len()
        && let Some(template) = guard.1.get(&bits)
    {
        return template.bind(py).copy();
    }

    let keys = format_keys(py, &entries);
    debug_assert_eq!(keys.len(), entries.len(), "format_keys snapshot is not parallel to entries");
    let dict = PyDict::new(py);
    for ((_, shift), key) in entries.iter().zip(keys.iter()) {
        let word = match (bits >> shift) & 0b11 {
            LEGALITY_LEGAL => intern!(py, "legal"),
            LEGALITY_RESTRICTED => intern!(py, "restricted"),
            LEGALITY_BANNED => intern!(py, "banned"),
            _ => intern!(py, "not_legal"),
        };
        dict.set_item(key.bind(py), word)?;
    }

    let mut cached = false;
    if let Ok(mut guard) = legality_templates().write() {
        // A snapshot of a different length means a format was registered since these templates were
        // built, so every one of them is missing a key. Drop the lot rather than serve short dicts.
        if guard.0 != entries.len() {
            guard.1.clear();
            guard.0 = entries.len();
        }
        if guard.1.len() < MAX_CACHED_LEGALITY_WORDS {
            guard.1.insert(bits, dict.clone().unbind());
            cached = true;
        }
    }
    // `dict` is now the TEMPLATE, not a row's dict -- `Bound::clone` increfs the same object rather
    // than copying it. Handing it back would let the caller's first mutation rewrite the template
    // and every later row built from it, which is the exact aliasing this function copies to avoid.
    // Only the uncached path, whose dict no one else holds, may return it directly.
    if cached { dict.copy() } else { Ok(dict) }
}

/// True when the registry already holds every `(format, shift)` pair the archive names — the
/// question `sync_format_shifts` asks before it takes a write lock.
///
/// It is CONTENT, not cardinality. This used to be `registry.len() < archive.len()`, which asks
/// only "does the archive name MORE formats than I do", and on a name-keyed map that misses every
/// change which keeps the count:
///
///   * a RENAME, which this corpus has actually had — Scryfall renamed `brawl` to `standardbrawl`
///     and `historicbrawl` to `brawl` in one pass;
///   * one format retired and another added in the same dump.
///
/// Shifts are handed out in first-seen order over a `legalities` object whose keys arrive sorted,
/// so either shape re-slots every alphabetically-later format by one. A registry that declines to
/// adopt the new map then reads every card's legality word at its NEIGHBOUR's two bits, and binds
/// `legality:`/`banned:`/`restricted:` to the same wrong pair. Nothing errors: the words still
/// decode, to the wrong statuses.
///
/// It cannot bite the store as built TODAY, because every Scryfall card carries the full
/// `legalities` object — so the first row of any build assigns all 22 formats in the same sorted
/// order, and two archives of the same vocabulary carry identical maps. That is a property of
/// Scryfall's output, not of this function.
fn shifts_agree<'a>(shifts: &HashMap<String, u8>, archive: impl IntoIterator<Item = (&'a str, u8)>) -> bool {
    archive.into_iter().all(|(format, shift)| shifts.get(format) == Some(&shift))
}

/// Make `shifts` agree with `archive`, which is the authority for the words the archive holds.
///
/// Inserting the archive's pairs is not enough, which only shows up once same-count changes are
/// visible at all: a retired name STILL HOLDS ITS SLOT. Insert `{a:0, b:2, d:4}` over
/// `{a:0, b:2, c:4}` and `format_order` emits both `c` and `d` at bits 4-5, so a format that no
/// longer exists reports the new one's status on every card. So drop exactly the entries this
/// archive contradicts — a slot it hands to a DIFFERENT name — and leave every slot it never
/// mentions alone, so a format assigned by an import running in this process and absent from this
/// archive survives the sync.
fn adopt_shifts(shifts: &mut HashMap<String, u8>, archive: &[(&str, u8)]) {
    let owner_of: HashMap<u8, &str> = archive.iter().map(|(format, shift)| (*shift, *format)).collect();
    shifts.retain(|name, shift| match owner_of.get(&*shift) {
        Some(owner) => *owner == name.as_str(),
        None => true,
    });
    for (format, shift) in archive {
        shifts.insert((*format).to_string(), *shift);
    }
}

/// Adopt the archive's format→shift assignments into this process's registry.
///
/// Cheap no-op (one read lock and ~22 lookups, no write and no generation bump) once the registry
/// agrees — which matters because this runs on EVERY query: `bind_and_split_filter` syncs before
/// `build_filter`, and a gratuitous `invalidate_format_order` would discard the cached order that
/// `format_order` exists to protect on every request.
pub(crate) fn sync_format_shifts(archived: &Archived<HashMap<String, u8>>) {
    let pairs = || archived.iter().map(|(format, shift)| (format.as_str(), *shift));
    // The common path borrows and allocates nothing. A poisoned lock reads as "agrees" and does
    // nothing, exactly as the old `unwrap_or(false)` did.
    if format_shifts().read().map(|m| shifts_agree(&m, pairs())).unwrap_or(true) {
        return;
    }
    let archive: Vec<(&str, u8)> = pairs().collect();
    if let Ok(mut shifts) = format_shifts().write() {
        adopt_shifts(&mut shifts, &archive);
    }
    invalidate_format_order();
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The cached order must follow the registry, or a format assigned after the first decode
    /// would be missing from every `legalities` value for the life of the process.
    ///
    /// Asserts only properties that hold under parallel tests against a shared global registry:
    /// the registry grows monotonically, so CONTAINMENT and SORTEDNESS are stable, while an
    /// exact-equality assertion would race anything else that loads a store.
    #[test]
    fn format_order_follows_the_registry() {
        let sorted = |v: &[(String, u8)]| v.windows(2).all(|w| w[0] <= w[1]);

        let before = format_order();
        assert!(sorted(&before), "cached order must be sorted");

        // A name no fixture uses, so this cannot collide with a real format.
        let probe = "zzz_test_only_format";
        let assigned = format_shift_or_assign(probe);
        if assigned.is_none() {
            return; // registry already full (MAX_FORMATS); nothing to assert
        }

        let after = format_order();
        assert!(sorted(&after), "still sorted after an assignment");
        assert!(
            after.iter().any(|(name, _)| name == probe),
            "a format assigned after the order was cached must appear in it"
        );

        // The caching itself: two calls with no writer between them reuse the allocation. Asserted
        // only when the generation held still across the pair, because this registry is global and
        // any other test loading a store bumps it -- an unconditional assert here is FLAKY, which
        // is how it was first written and how it failed once in a full parallel run.
        let generation = FORMAT_GENERATION.load(AtomicOrdering::Acquire);
        let first = format_order();
        let second = format_order();
        if FORMAT_GENERATION.load(AtomicOrdering::Acquire) == generation {
            assert!(Arc::ptr_eq(&first, &second), "second call must reuse the cached order");
        }
    }

    fn map_of(pairs: &[(&str, u8)]) -> HashMap<String, u8> {
        pairs.iter().map(|(k, v)| ((*k).to_owned(), *v)).collect()
    }

    /// Sorted pairs — `format_order`'s content, without needing a registry to hold it.
    fn sorted_pairs(shifts: &HashMap<String, u8>) -> Vec<(String, u8)> {
        let mut out: Vec<(String, u8)> = shifts.iter().map(|(k, v)| (k.clone(), *v)).collect();
        out.sort();
        out
    }

    /// STALENESS IS CONTENT, NOT CARDINALITY. Two maps of the same SIZE can disagree about which
    /// format owns which two bits, and the question `sync_format_shifts` used to ask —
    /// `registry.len() < archive.len()` — says "nothing to do" to every one of them.
    ///
    /// The fixture is the shape this corpus has actually had: a RENAME. Scryfall renamed `brawl`
    /// to `standardbrawl`; the count is identical before and after, and the slot the old name held
    /// goes to the new one.
    ///
    /// Asserted against `shifts_agree`/`adopt_shifts` over an owned map rather than against the
    /// global registry, so it is exact and unconditional. The test above documents what reading
    /// `FORMAT_SHIFTS` costs: containment instead of equality, and an early return whenever the
    /// shared registry happens to be full.
    #[test]
    fn a_same_count_archive_is_not_agreement() {
        let mut shifts = map_of(&[("brawl", 0), ("commander", 2), ("modern", 4)]);
        let archive = [("standardbrawl", 0u8), ("commander", 2), ("modern", 4)];
        assert_eq!(shifts.len(), archive.len(), "the fixture is only interesting because the counts MATCH");

        assert!(!shifts_agree(&shifts, archive), "a renamed format is a disagreement the count cannot see");
        adopt_shifts(&mut shifts, &archive);

        assert_eq!(shifts.get("standardbrawl"), Some(&0), "the archive's name must become bindable");
        assert_eq!(
            shifts.get("brawl"),
            None,
            "the retired name must not keep the slot — it would report standardbrawl's status on every card"
        );
        assert_eq!(
            sorted_pairs(&shifts),
            [("commander".to_owned(), 2), ("modern".to_owned(), 4), ("standardbrawl".to_owned(), 0)]
        );
        assert!(shifts_agree(&shifts, archive), "and the adoption must settle it");
    }

    /// A slot the archive never mentions belongs to whoever holds it: an import running in this
    /// process assigns formats through `format_shift_or_assign` against the same registry, and
    /// dropping those on every archive attach would un-assign a format mid-import.
    ///
    /// The growth case rides along — an archive that is a strict SUPERSET still extends, which is
    /// the only thing the old count check could see and must keep working.
    #[test]
    fn adopting_keeps_slots_the_archive_does_not_claim_and_still_extends() {
        let mut shifts = map_of(&[("commander", 0), ("modern", 2), ("import_only", 4)]);
        let archive = [("commander", 0u8), ("modern", 2), ("alchemy", 6)];

        assert!(!shifts_agree(&shifts, archive), "a format only the archive knows is news");
        adopt_shifts(&mut shifts, &archive);

        assert_eq!(shifts.get("import_only"), Some(&4), "a slot the archive is silent about is left alone");
        assert_eq!(shifts.get("alchemy"), Some(&6), "and a format only the archive knows is adopted");
        assert_eq!(
            sorted_pairs(&shifts),
            [
                ("alchemy".to_owned(), 6),
                ("commander".to_owned(), 0),
                ("import_only".to_owned(), 4),
                ("modern".to_owned(), 2)
            ]
        );
    }

    /// The fast path has to STAY fast: `bind_and_split_filter` syncs before `build_filter`, so
    /// this question is asked on every query, and answering "no" takes a write lock and discards
    /// the cached order. An archive that names a subset is not news either.
    #[test]
    fn an_archive_the_registry_already_holds_is_agreement() {
        let shifts = map_of(&[("commander", 0), ("modern", 2)]);
        assert!(shifts_agree(&shifts, [("commander", 0u8), ("modern", 2)]));
        assert!(shifts_agree(&shifts, [("modern", 2u8)]), "a subset teaches the registry nothing");
        assert!(shifts_agree(&shifts, [] as [(&str, u8); 0]), "and an empty archive least of all");
        assert!(!shifts_agree(&shifts, [("modern", 4u8)]), "the same name at another slot IS news");
    }
}

/// Perf-audit finding #4 (upstream #1056): `legality_bits_to_pydict` used to clone the whole
/// format registry into a fresh `Vec` and sort it on every call -- once per output row whenever
/// `legalities` is requested. Compares that against the cached, pre-sorted snapshot
/// `format_order()` serves on this branch, over a registry sized like the real one (22 formats,
/// per this module's header comment).
///
///     cargo test --release bench_legality_dict_cost -- --ignored --nocapture
#[cfg(test)]
mod bench_legality_dict_cost {
    use std::hint::black_box;
    use std::time::Instant;

    use super::{format_order, format_shift_or_assign, format_shifts};

    const ITERS: usize = 200_000;
    const FORMATS: &[&str] = &[
        "standard", "pioneer", "modern", "legacy", "pauper", "vintage", "penny", "commander",
        "oathbreaker", "standardbrawl", "brawl", "alchemy", "paupercommander", "duel", "oldschool",
        "premodern", "predh", "historic", "timeless", "gladiator", "explorer", "future",
    ];

    fn seed_registry() {
        for f in FORMATS {
            format_shift_or_assign(f);
        }
        assert!(format_shifts().read().unwrap().len() >= FORMATS.len());
    }

    #[test]
    #[ignore]
    fn bench_legality_dict_cost() {
        seed_registry();
        let bits: u64 = 0x5555_5555; // arbitrary — content doesn't affect either path's cost

        // Pre-fix behavior: clone every (String, u8) entry out of the map into a fresh Vec, sort it.
        let start = Instant::now();
        for _ in 0..ITERS {
            let shifts = format_shifts().read().unwrap();
            let mut entries: Vec<(String, u8)> = shifts.iter().map(|(k, v)| (k.clone(), *v)).collect();
            entries.sort();
            black_box(&entries);
            for (_, shift) in &entries {
                black_box((black_box(bits) >> shift) & 0b11);
            }
        }
        let clone_sort_ns = start.elapsed().as_nanos() as f64 / ITERS as f64;

        // Fixed: reuse the cached, pre-sorted snapshot.
        let start = Instant::now();
        for _ in 0..ITERS {
            let entries = format_order();
            black_box(&entries);
            for (_, shift) in entries.iter() {
                black_box((black_box(bits) >> shift) & 0b11);
            }
        }
        let cached_ns = start.elapsed().as_nanos() as f64 / ITERS as f64;

        println!("clone+sort per row (pre-fix): {clone_sort_ns:.1} ns/call");
        println!("cached snapshot (fixed):      {cached_ns:.1} ns/call");
        println!(
            "delta: {:.1} ns/call ({:.0}% reduction)",
            clone_sort_ns - cached_ns,
            100.0 * (clone_sort_ns - cached_ns) / clone_sort_ns
        );
    }
}
