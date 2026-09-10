use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

mod cuckoo;
mod gen_cache;
mod region;
mod types;

use gen_cache::{GenerationalSharedCache, access_response};

// ── Python-visible response type ──────────────────────────────────────────────

#[pyclass(get_all)]
pub struct CachedResponsePy {
    pub status: String,
    pub headers: Py<PyList>,
    pub body: Option<Py<PyBytes>>,
    pub result_count: Option<i64>,
    pub total_cards: Option<i64>,
}

fn build_response(py: Python, bytes: &[u8]) -> PyResult<CachedResponsePy> {
    let ar = access_response(bytes);
    let headers_list = PyList::new(
        py,
        ar.headers.iter().map(|t| (t.0.as_str(), t.1.as_str())),
    )?.unbind();
    let body = ar.body.as_ref().map(|b| PyBytes::new(py, b.as_slice()).into());
    Ok(CachedResponsePy {
        status: ar.status.as_str().to_owned(),
        headers: headers_list,
        body,
        result_count: ar.result_count.as_ref().map(|x| i64::from(*x)),
        total_cards: ar.total_cards.as_ref().map(|x| i64::from(*x)),
    })
}

// ── Python-visible cache type ─────────────────────────────────────────────────

#[pyclass]
pub struct SharedCache {
    inner: GenerationalSharedCache,
}

#[pymethods]
impl SharedCache {
    #[new]
    #[pyo3(signature = (path, maxsize=10_000, default_ttl=None, arena_mb=None, n_pages=2))]
    fn new(
        path: &str,
        maxsize: usize,
        default_ttl: Option<f64>,
        arena_mb: Option<usize>,
        n_pages: usize,
    ) -> PyResult<Self> {
        GenerationalSharedCache::open(path, maxsize, n_pages, default_ttl, arena_mb)
            .map(|inner| SharedCache { inner })
            .map_err(|e| pyo3::exceptions::PyIOError::new_err(e.to_string()))
    }

    #[pyo3(signature = (key, value, ttl=None))]
    fn set(
        &mut self,
        key: &[u8],
        value: &Bound<PyAny>,
        ttl: Option<f64>,
    ) -> PyResult<()> {
        // Extract body zero-copy first — fast_check only needs the bytes, not ownership.
        let body_obj = value.getattr("body")?;
        let body_bytes: Option<Bound<PyBytes>> = body_obj.extract()?;
        let body: Option<&[u8]> = body_bytes.as_ref().map(|b| b.as_bytes());
        // Skip rkyv + headers extraction entirely when content is unchanged. When it isn't,
        // fast_check's key hash and body fingerprint are reused by set() below instead of
        // being recomputed.
        let Some(pending) = self.inner.fast_check(key, body) else {
            return Ok(());
        };
        let status: String = value.getattr("status")?.extract()?;
        let headers: Vec<(String, String)> = value.getattr("headers")?.extract()?;
        let result_count: Option<i64> = value.getattr("result_count")?.extract()?;
        let total_cards: Option<i64> = value.getattr("total_cards")?.extract()?;
        self.inner.set(key, pending, &status, headers, body, result_count, total_cards, ttl);
        Ok(())
    }

    fn get(&mut self, py: Python, key: &[u8]) -> PyResult<Option<CachedResponsePy>> {
        self.inner.get_with(key, |b| build_response(py, b)).transpose()
    }

    fn __setitem__(&mut self, key: &[u8], value: &Bound<PyAny>) -> PyResult<()> {
        self.set(key, value, None)
    }

    fn __getitem__(&mut self, py: Python, key: &[u8]) -> PyResult<CachedResponsePy> {
        self.get(py, key)?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("cache miss"))
    }

    fn __len__(&self) -> usize {
        self.inner.entry_count() as usize
    }

    /// Occupancy snapshot: per-page entry counts and arena usage, the entry budget per page and
    /// the rotation count. A page with `arena_used` near `arena_capacity` and `entry_count` well
    /// under `gen_maxsize` is arena-bound (bodies larger than the per-entry share).
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let pages = PyList::empty(py);
        for (index, page) in self.inner.page_stats().into_iter().enumerate() {
            let d = PyDict::new(py);
            d.set_item("index", index)?;
            d.set_item("entry_count", page.entry_count)?;
            d.set_item("arena_used", page.arena_used)?;
            d.set_item("arena_capacity", page.arena_capacity)?;
            d.set_item("sealed", page.sealed)?;
            pages.append(d)?;
        }
        let stats = PyDict::new(py);
        stats.set_item("pages", pages)?;
        stats.set_item("gen_maxsize", self.inner.gen_maxsize())?;
        stats.set_item("rotations", self.inner.rotation_count())?;
        Ok(stats)
    }

    fn __contains__(&mut self, key: &[u8]) -> bool {
        self.inner.contains(key)
    }

    fn pop(&mut self, key: &[u8]) -> bool {
        self.inner.pop(key)
    }

    fn invalidate(&mut self) {
        self.inner.invalidate();
    }

    fn _get_raw<'py>(&mut self, py: Python<'py>, key: &[u8]) -> PyResult<Option<Bound<'py, PyBytes>>> {
        Ok(self.inner.get_with(key, |b| PyBytes::new(py, b)))
    }

    fn _get_raw_decoded(&mut self, py: Python, key: &[u8]) -> PyResult<Option<CachedResponsePy>> {
        self.get(py, key)
    }

    /// Benchmarking helper: filter check + active-page probe under lock, no arena copy.
    fn _probe_only(&mut self, key: &[u8]) -> bool {
        self.inner.probe_only(key)
    }
}

// ── Module ────────────────────────────────────────────────────────────────────

#[pymodule]
fn shared_cache(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_class::<SharedCache>()?;
    m.add_class::<CachedResponsePy>()?;
    Ok(())
}
