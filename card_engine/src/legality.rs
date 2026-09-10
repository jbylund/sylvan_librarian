// Legalities pack into a u64: 2 bits per format, positions handed out by a global
// registry the first time a format name appears in loaded data, so bit assignments
// stay stable across reloads within a process. Every query first adopts the archive's
// own assignments (`sync_format_shifts`), so a worker that never ran the load path --
// or ran it against a different archive -- reads the bits the archive was built with.
// A format the card's JSONB omits reads as not_legal. 32 formats fit; Scryfall ships 22.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock, RwLock};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyString};
use rkyv::Archived;

const LEGALITY_NOT_LEGAL: u64 = 0;
pub(crate) const LEGALITY_LEGAL: u64 = 1;
pub(crate) const LEGALITY_RESTRICTED: u64 = 2;
pub(crate) const LEGALITY_BANNED: u64 = 3;
pub(crate) const MAX_FORMATS: usize = 32;

static FORMAT_SHIFTS: OnceLock<RwLock<HashMap<String, u8>>> = OnceLock::new();

/// Bumped under the registry's write lock on EVERY change -- a format appended by the load path or
/// an archive's assignments adopted by `sync_format_shifts` -- so `format_shifts_sorted()` can
/// detect staleness without taking a lock on the map itself. It used to be the registry's LENGTH,
/// which is blind to an equal-length reassignment: an archive whose formats sit at different
/// shifts than the registry's read every 2-bit field from the wrong position, and the sorted
/// snapshot, the interned keys and the template dicts all kept serving the old assignment.
static FORMAT_GENERATION: AtomicU64 = AtomicU64::new(0);

pub(crate) fn format_shifts() -> &'static RwLock<HashMap<String, u8>> {
    FORMAT_SHIFTS.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Alphabetically sorted `(format, shift)` snapshot of the registry, rebuilt only when
/// `FORMAT_GENERATION` has moved since the last build. A snapshot is one `Arc`, and the two caches
/// derived from it (`format_keys`, `legality_templates`) are keyed on that `Arc`'s identity rather
/// than on any count: they are positionally parallel to exactly the snapshot they were built from,
/// and a new snapshot -- however similar in size -- is a new `Arc` they cannot match.
type SortedFormats = Arc<[(String, u8)]>;

pub(crate) fn format_shifts_sorted() -> SortedFormats {
    static SORTED: OnceLock<RwLock<(u64, SortedFormats)>> = OnceLock::new();
    let cache = SORTED.get_or_init(|| RwLock::new((0, Arc::from([] as [(String, u8); 0]))));
    let current = FORMAT_GENERATION.load(Ordering::Acquire);

    if let Ok(guard) = cache.read()
        && guard.0 >= current
    {
        return guard.1.clone();
    }
    let Ok(mut guard) = cache.write() else { return Arc::from([]) };
    if guard.0 >= current {
        return guard.1.clone(); // rebuilt by another thread while we waited for the write lock
    }
    // Generation and contents read under the same lock the writers bump under, so the label can
    // never be paired with another generation's map. `>=` above: a thread that loaded `current`
    // before a concurrent change must not overwrite the newer snapshot with an older label.
    let (generation, mut entries): (u64, Vec<(String, u8)>) = match format_shifts().read() {
        Ok(m) => (FORMAT_GENERATION.load(Ordering::Acquire), m.iter().map(|(k, v)| (k.clone(), *v)).collect()),
        Err(_) => return guard.1.clone(),
    };
    if guard.0 >= generation {
        return guard.1.clone();
    }
    entries.sort();
    let built: Arc<[(String, u8)]> = Arc::from(entries);
    *guard = (generation, built.clone());
    built
}

/// Bit shift for a format already seen in loaded data; None matches nothing.
pub(crate) fn format_shift(format: &str) -> Option<u8> {
    format_shifts().read().ok()?.get(format).copied()
}

/// Bit shift for a format, assigning the lowest free slot if unseen (reload path).
///
/// The lowest FREE slot, not `len * 2`: the registry mirrors whichever archive was last adopted,
/// and nothing guarantees that archive's shifts are dense from zero.
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
    let shift = (0..MAX_FORMATS as u8).map(|slot| slot * 2).find(|s| !shifts.values().any(|v| v == s))?;
    shifts.insert(format.to_string(), shift);
    FORMAT_GENERATION.fetch_add(1, Ordering::AcqRel);
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

pub(crate) fn jsonb_obj_to_legality_bits(d: &Bound<PyDict>, key: &Bound<'_, PyString>) -> u64 {
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
/// The format names as interned `PyString` keys, parallel to a `SortedFormats` snapshot.
type FormatKeys = Arc<[Py<PyString>]>;

/// The format names as interned `PyString` keys, positionally parallel to the
/// `format_shifts_sorted()` snapshot they were built from.
///
/// Keyed on the snapshot `Arc`'s IDENTITY. `keys[i]` describes `entries[i]` only for the exact
/// entries the keys were built from: the snapshot is sorted alphabetically, so a registered format
/// lands in the middle and moves every later index, and an adopted reassignment changes shifts
/// without changing the length. Neither a count nor a length can tell those apart; the `Arc` the
/// snapshot cache hands out can, because it only ever allocates a new one when the registry moved.
/// The cached clone keeps the old snapshot alive, so a pointer can never be reused for a new one.
///
/// A `PyOnceLock`, as the `intern!` keys use: Python objects are built while holding the GIL, and
/// a std cell would block every other initializer for the duration -- pyo3's documented deadlock.
fn format_keys(py: Python<'_>, entries: &SortedFormats) -> FormatKeys {
    static KEYS: PyOnceLock<RwLock<Option<(SortedFormats, FormatKeys)>>> = PyOnceLock::new();
    let cache = KEYS.get_or_init(py, || RwLock::new(None));

    if let Ok(guard) = cache.read()
        && let Some((built_for, keys)) = &*guard
        && Arc::ptr_eq(built_for, entries)
    {
        return keys.clone();
    }
    let Ok(mut guard) = cache.write() else { return Arc::from([]) };
    if let Some((built_for, keys)) = &*guard
        && Arc::ptr_eq(built_for, entries)
    {
        return keys.clone(); // rebuilt by another thread while we waited for the write lock
    }
    let built: FormatKeys =
        entries.iter().map(|(format, _)| PyString::intern(py, format.as_str()).unbind()).collect();
    *guard = Some((entries.clone(), built.clone()));
    built
}

/// Cap on distinct legality words held as template dicts.
///
/// The real corpus has 591 distinct combinations across 97,812 printings, so this is ~7x headroom
/// and exists only so a pathological corpus cannot grow the map without bound. Past the cap the
/// builder still returns correct dicts, just uncached.
const MAX_CACHED_LEGALITY_WORDS: usize = 4096;

/// Template dicts by legality word, valid for exactly the recorded `SortedFormats` snapshot (by
/// `Arc` identity, as `format_keys`).
type LegalityTemplates = (Option<SortedFormats>, HashMap<u64, Py<PyDict>>);

fn legality_templates() -> &'static RwLock<LegalityTemplates> {
    static DICTS: OnceLock<RwLock<LegalityTemplates>> = OnceLock::new();
    DICTS.get_or_init(|| RwLock::new((None, HashMap::new())))
}

/// Whether a derived cache recorded as built for `built_for` is valid for `entries`.
fn same_snapshot(built_for: &Option<SortedFormats>, entries: &SortedFormats) -> bool {
    built_for.as_ref().is_some_and(|b| Arc::ptr_eq(b, entries))
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
    let entries = format_shifts_sorted();

    if let Ok(guard) = legality_templates().read()
        && same_snapshot(&guard.0, &entries)
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
        // A different snapshot means the registry moved since these templates were built -- a format
        // registered (every template short a key) or a reassignment adopted (every status under the
        // wrong key). Drop the lot rather than serve them.
        if !same_snapshot(&guard.0, &entries) {
            guard.1.clear();
            guard.0 = Some(entries.clone());
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

/// Adopt the archive's format→shift assignments into this process's registry.
/// Cheap no-op (one read lock) once the registry agrees with the archive.
///
/// Agreement is by CONTENT -- every archived `(format, shift)` pair present in the registry at that
/// shift -- and disagreement REPLACES the registry with the archive's map. It used to compare
/// lengths, so an archive of the same size with different assignments was "already caught up" and
/// every legality bit was read from the wrong 2-bit field. Formats the registry has beyond the
/// archive's are left alone when everything the archive has agrees: their bits are zero in every
/// printing of this archive, which reads as not_legal, exactly what an omitted format means.
pub(crate) fn sync_format_shifts(archived: &Archived<HashMap<String, u8>>) {
    let agrees = |m: &HashMap<String, u8>| archived.iter().all(|(format, shift)| m.get(format.as_str()) == Some(shift));
    if format_shifts().read().map(|m| agrees(&m)).unwrap_or(true) {
        return;
    }
    if let Ok(mut shifts) = format_shifts().write() {
        if agrees(&shifts) {
            return; // adopted by another thread while we waited for the write lock
        }
        shifts.clear();
        shifts.extend(archived.iter().map(|(format, shift)| (format.as_str().to_string(), *shift)));
        FORMAT_GENERATION.fetch_add(1, Ordering::AcqRel);
    }
}

/// Perf-audit finding #4: `legality_bits_to_pydict` used to clone the whole format registry
/// into a fresh `Vec` and sort it on every call -- once per output row whenever `legalities`
/// is requested. Compares that against the cached, pre-sorted `Arc<[(String, u8)]>` snapshot
/// `format_shifts_sorted()` now serves, over a registry sized like the real one (22 formats,
/// per this module's header comment).
///
///     cargo test --release bench_legality_dict_cost -- --ignored --nocapture
#[cfg(test)]
mod bench_legality_dict_cost {
    use std::hint::black_box;
    use std::time::Instant;

    use super::{format_shift_or_assign, format_shifts, format_shifts_sorted};

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
        assert_eq!(format_shifts().read().unwrap().len(), FORMATS.len());
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

        // Fixed: reuse the cached, pre-sorted Arc snapshot.
        let start = Instant::now();
        for _ in 0..ITERS {
            let entries = format_shifts_sorted();
            black_box(&entries);
            for (_, shift) in entries.iter() {
                black_box((black_box(bits) >> shift) & 0b11);
            }
        }
        let cached_ns = start.elapsed().as_nanos() as f64 / ITERS as f64;

        println!("clone+sort per row (pre-fix): {clone_sort_ns:.1} ns/call");
        println!("cached Arc snapshot (fixed):  {cached_ns:.1} ns/call");
        println!(
            "delta: {:.1} ns/call ({:.0}% reduction)",
            clone_sort_ns - cached_ns,
            100.0 * (clone_sort_ns - cached_ns) / clone_sort_ns
        );
    }
}
