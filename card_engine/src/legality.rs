// Legalities pack into a u64: 2 bits per format, positions handed out append-only
// by a global registry the first time a format name appears in loaded data, so
// bit assignments stay stable across reloads and engine instances. A format the
// card's JSONB omits reads as not_legal. 32 formats fit; Scryfall ships 22.

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
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

/// Mirrors `format_shifts().len()`, updated under the same write lock that grows the map.
/// Lets `format_shifts_sorted()` detect staleness without taking a lock on the map itself.
static FORMAT_COUNT: AtomicUsize = AtomicUsize::new(0);

pub(crate) fn format_shifts() -> &'static RwLock<HashMap<String, u8>> {
    FORMAT_SHIFTS.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Alphabetically sorted `(format, shift)` snapshot of the registry, rebuilt only when
/// `FORMAT_COUNT` has moved since the last build. The registry is append-only (new formats
/// get the next free shift; existing ones never change), so a stale-but-shorter snapshot is
/// simply missing the newest formats, never wrong about the ones it has -- safe to keep
/// serving while a concurrent rebuild is in flight.
type SortedFormats = Arc<[(String, u8)]>;

fn format_shifts_sorted() -> SortedFormats {
    static SORTED: OnceLock<RwLock<(usize, SortedFormats)>> = OnceLock::new();
    let cache = SORTED.get_or_init(|| RwLock::new((0, Arc::from([] as [(String, u8); 0]))));
    let current = FORMAT_COUNT.load(Ordering::Acquire);

    if let Ok(guard) = cache.read()
        && guard.0 == current
    {
        return guard.1.clone();
    }
    let Ok(mut guard) = cache.write() else { return Arc::from([]) };
    if guard.0 == current {
        return guard.1.clone(); // rebuilt by another thread while we waited for the write lock
    }
    let mut entries: Vec<(String, u8)> = format_shifts()
        .read()
        .map(|m| m.iter().map(|(k, v)| (k.clone(), *v)).collect())
        .unwrap_or_default();
    entries.sort();
    let built: Arc<[(String, u8)]> = Arc::from(entries);
    *guard = (current, built.clone());
    built
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
    FORMAT_COUNT.store(shifts.len(), Ordering::Release);
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
/// The format names as interned `PyString` keys, parallel to a `SortedFormats` snapshot.
type FormatKeys = Arc<[Py<PyString>]>;

/// The format names as interned `PyString` keys, positionally parallel to a
/// `format_shifts_sorted()` snapshot of the same length.
///
/// Keyed on the snapshot's LENGTH rather than `FORMAT_COUNT` on purpose. The registry is
/// append-only in its *shift* assignments, but `format_shifts_sorted()` is sorted
/// ALPHABETICALLY, so a new format lands in the middle and moves every later entry's index.
/// Positional correspondence therefore only holds against a snapshot of the same length --
/// and because the registry is append-only, a given length pins a unique format set and so a
/// unique sorted order. Matching lengths is exactly the condition under which `keys[i]`
/// describes `entries[i]`.
fn format_keys(py: Python<'_>, entries: &SortedFormats) -> FormatKeys {
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

/// Template dicts by legality word, valid for a `SortedFormats` snapshot of the recorded length.
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
    let entries = format_shifts_sorted();

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

/// Adopt the archive's format→shift assignments into this process's registry.
/// Cheap no-op (one read lock) once the registry has caught up.
pub(crate) fn sync_format_shifts(archived: &Archived<HashMap<String, u8>>) {
    let behind = format_shifts().read().map(|m| m.len() < archived.len()).unwrap_or(false);
    if !behind {
        return;
    }
    if let Ok(mut shifts) = format_shifts().write() {
        for (format, shift) in archived.iter() {
            shifts.insert(format.as_str().to_string(), *shift);
        }
        FORMAT_COUNT.store(shifts.len(), Ordering::Release);
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
