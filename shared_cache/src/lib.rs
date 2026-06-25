mod cache;
mod cuckoo;
mod region;
mod types;

use pyo3::prelude::*;

use cache::{access_response, SharedCache as Inner};

/// Returned by `SharedCache.get()`. Attribute layout matches the Python
/// `CachedResponse` NamedTuple so the existing middleware code needs no changes.
#[pyclass(get_all)]
pub struct CachedResponse {
    pub status: String,
    pub headers: Py<pyo3::types::PyList>,
    pub body: Option<Py<pyo3::types::PyBytes>>,
    pub result_count: Option<i64>,
    pub total_cards: Option<i64>,
}

#[pymethods]
impl CachedResponse {
    fn __repr__(&self, py: Python) -> String {
        let rc = self
            .result_count
            .map(|v| v.to_string())
            .unwrap_or_else(|| "None".to_string());
        format!(
            "CachedResponse(status={:?}, body_len={}, result_count={})",
            self.status,
            self.body.as_ref().map(|b| b.bind(py).as_bytes().len()).unwrap_or(0),
            rc,
        )
    }
}

/// Cross-process LRU cache backed by a memory-mapped file.
///
/// All worker processes that open the same `path` share a single cache.
/// Values are stored as rkyv-serialized `CachedResponse` structs so reads
/// are zero-copy within the locked critical section.
///
/// Keys must be `bytes`. The caller is responsible for serializing the logical
/// key before calling get/set (e.g. ``key_bytes = orjson.dumps(key_tuple)``).
/// Computing key bytes once and reusing them for both get and set on a miss
/// avoids a redundant serialization call.
///
/// Usage::
///
///     import orjson
///     cache = SharedCache(path="/tmp/arcane.cache", maxsize=10_000, default_ttl=300.0)
///     key_bytes = orjson.dumps(key)
///     cached = cache.get(key_bytes)   # returns CachedResponse | None
///     cache[key_bytes] = response     # stores a CachedResponse (or any object with those attrs)
///     cache.invalidate()              # flush all entries
#[pyclass]
pub struct SharedCache {
    inner: Inner,
}

#[pymethods]
impl SharedCache {
    #[new]
    #[pyo3(signature = (path, maxsize=10_000, default_ttl=None, arena_mb=None))]
    fn new(
        path: &str,
        maxsize: u32,
        default_ttl: Option<f64>,
        arena_mb: Option<u32>,
    ) -> PyResult<Self> {
        let arena_bytes = arena_mb.map(|mb| mb * 1024 * 1024);
        let inner = Inner::open(path, maxsize, default_ttl, arena_bytes)
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?;
        Ok(Self { inner })
    }

    /// Store a response under `key` (must be `bytes`).
    /// `value` must have `.status`, `.headers`, `.body`, `.result_count`, and
    /// `.total_cards` attributes (compatible with the `CachedResponse` NamedTuple).
    fn set(
        &mut self,
        key: &[u8],
        value: &Bound<PyAny>,
        ttl: Option<f64>,
    ) -> PyResult<()> {
        let (status, headers, body, result_count, total_cards) = extract_response(value)?;
        self.inner
            .set(key, &status, headers, body, result_count, total_cards, ttl);
        Ok(())
    }

    /// Return the cached `CachedResponse` for `key` (`bytes`), or `None` on miss / expiry.
    fn get(&mut self, py: Python, key: &[u8]) -> PyResult<Option<CachedResponse>> {
        Ok(self.inner.get_with(key, |bytes| build_response(py, bytes)))
    }

    fn __setitem__(
        &mut self,
        key: &[u8],
        value: &Bound<PyAny>,
    ) -> PyResult<()> {
        self.set(key, value, None)
    }

    fn __getitem__(&mut self, py: Python, key: &[u8]) -> PyResult<CachedResponse> {
        match self.inner.get_with(key, |bytes| build_response(py, bytes)) {
            Some(r) => Ok(r),
            None => Err(pyo3::exceptions::PyKeyError::new_err(
                "key not in shared cache",
            )),
        }
    }

    fn __len__(&self) -> usize {
        self.inner.entry_count() as usize
    }

    /// Lock-free membership test via the cuckoo filter (~3% FPR, no false negatives).
    /// `key in cache` costs ~30 ns vs ~390 ns for a full get — use it as a fast pre-check.
    fn __contains__(&self, key: &[u8]) -> bool {
        self.inner.contains(key)
    }

    /// Wipe all entries by resetting the arena and slot table.
    fn invalidate(&mut self) {
        self.inner.invalidate();
    }

    // ── Benchmarking probes ───────────────────────────────────────────────────

    /// Lock + probe + release + mmap→PyBytes copy, no intermediate Vec.
    fn _get_raw<'py>(
        &mut self,
        py: Python<'py>,
        key: &[u8],
    ) -> PyResult<Option<pyo3::Bound<'py, pyo3::types::PyBytes>>> {
        Ok(self.inner.get_with(key, |bytes| {
            pyo3::types::PyBytes::new(py, bytes).unbind()
        }).map(|b| b.into_bound(py)))
    }

    /// Lock + probe + release only — no arena copy.
    fn _probe_only(&mut self, key: &[u8]) -> bool {
        self.inner.probe_only(key)
    }

    /// Like `_get_raw` but also builds the Python CachedResponse from rkyv bytes.
    fn _get_raw_decoded(&mut self, py: Python, key: &[u8]) -> Option<CachedResponse> {
        self.inner.get_with(key, |bytes| build_response(py, bytes))
    }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn extract_response(
    value: &Bound<PyAny>,
) -> PyResult<(
    String,
    Vec<(String, String)>,
    Option<Vec<u8>>,
    Option<i64>,
    Option<i64>,
)> {
    let status: String = value.getattr("status")?.extract()?;
    let headers: Vec<(String, String)> = value.getattr("headers")?.extract()?;
    let body: Option<Vec<u8>> = value.getattr("body")?.extract()?;
    let result_count: Option<i64> = value.getattr("result_count")?.extract()?;
    let total_cards: Option<i64> = value.getattr("total_cards")?.extract()?;
    Ok((status, headers, body, result_count, total_cards))
}

fn build_response(py: Python, bytes: &[u8]) -> CachedResponse {
    let ar = access_response(bytes);
    // Build Python list of 2-tuples directly from the rkyv archive — no intermediate
    // Rust String allocations. Each archived &str is handed straight to PyString::new.
    // With Py<PyList> stored in the pyclass, .headers attribute access is a refcount bump.
    let headers: Py<pyo3::types::PyList> = pyo3::types::PyList::new(
        py,
        ar.headers.iter().map(|t| (t.0.as_str(), t.1.as_str())),
    ).unwrap().unbind();
    let body: Option<Py<pyo3::types::PyBytes>> = ar
        .body
        .as_ref()
        .map(|b| pyo3::types::PyBytes::new(py, b.as_slice()).unbind());
    // rkyv 0.8 archives i64 as i64_le (endian-portable); convert via From.
    let result_count: Option<i64> = ar.result_count.as_ref().map(|v| i64::from(*v));
    let total_cards: Option<i64> = ar.total_cards.as_ref().map(|v| i64::from(*v));
    CachedResponse {
        status: ar.status.to_string(),
        headers,
        body,
        result_count,
        total_cards,
    }
}

#[pymodule]
fn shared_cache(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_class::<SharedCache>()?;
    m.add_class::<CachedResponse>()?;
    Ok(())
}
