/**
 * @jest-environment jsdom
 */
'use strict';

const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Drain all pending microtasks and one macrotask turn. */
const flushPromises = () => new Promise(resolve => setTimeout(resolve, 0));

/** Multiple microtask yields — used inside fake-timer blocks where setTimeout is frozen. */
const flushMicrotasks = async (n = 10) => {
  for (let i = 0; i < n; i++) await Promise.resolve();
};

/** Build a successful Response-like object. */
function okResponse(data) {
  return { ok: true, status: 200, json: () => Promise.resolve(data) };
}

/** Build a failing Response-like object (non-2xx). */
function errorResponse(title, description = 'bad input', status = 400) {
  return { ok: false, status, json: () => Promise.resolve({ title, description }) };
}

const EMPTY_RESULT = { cards: [], total_cards: 0 };

/** Expected URL for a plain query (no autocomplete transforms). */
function expectedUrl(query) {
  const q = query.trim().replace(/\s+/g, ' ');
  return `/search?q=${encodeURIComponent(q)}&orderby=edhrec&direction=asc&unique=card&prefer=default`;
}

/** An AbortError as thrown by a real fetch. */
function abortError() {
  return Object.assign(new Error('The user aborted a request.'), { name: 'AbortError' });
}

// ---------------------------------------------------------------------------
// Load the class once (jsdom environment is active for the whole module)
// ---------------------------------------------------------------------------

const appCode = fs.readFileSync(path.resolve(__dirname, 'app.js'), 'utf8');
// eslint-disable-next-line no-new-func
const CardSearch = Function(appCode + '; return CardSearch;')();

// ---------------------------------------------------------------------------
// Per-test setup
// ---------------------------------------------------------------------------

/**
 * Each call to `fetch` appends a deferred { promise, resolve, reject } to
 * fetchQueue. Tests control when/how each request settles.
 *
 * Importantly, the mock respects AbortController: if the signal is already
 * aborted when fetch() is called, or fires later, the promise rejects with
 * an AbortError — matching real browser behaviour.
 */
let fetchQueue;

function buildFetchMock() {
  fetchQueue = [];
  return jest.fn((_url, options) => {
    let resolve, reject;
    const promise = new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    });
    fetchQueue.push({ promise, resolve, reject });

    const signal = options?.signal;
    if (signal) {
      if (signal.aborted) {
        reject(abortError());
      } else {
        signal.addEventListener('abort', () => reject(abortError()), { once: true });
      }
    }

    return promise;
  });
}

/** Build the minimal DOM that CardSearch.init() requires. */
function buildDOM() {
  document.body.innerHTML = `
    <div class="header"><h1>Arcane Tutor</h1></div>
    <form class="search-container">
      <input id="searchInput" type="text" />
    </form>
    <select id="orderDropdown"><option value="edhrec" selected>EDHREC</option></select>
    <select id="uniqueDropdown"><option value="card" selected>Card</option></select>
    <select id="preferDropdown"><option value="default" selected>Default</option></select>
    <button id="orderToggle"></button>
    <input id="directionInput" value="asc" />
    <div id="results"></div>
    <div id="loading" style="display:none"></div>
    <div id="statusMessage"></div>
  `;
}

let search;

beforeEach(async () => {
  buildDOM();

  global.fetch = buildFetchMock();
  // jsdom defines performance as non-writable, so a plain assignment silently
  // fails. Use Object.defineProperty to forcibly replace it.
  Object.defineProperty(global, 'performance', {
    value: { now: jest.fn(() => 100), clearResourceTimings: jest.fn(), getEntriesByType: jest.fn(() => []) },
    configurable: true,
    writable: true,
  });
  window.commonCardTypesPromise = Promise.resolve([]);

  search = new CardSearch();

  // Stub display/DOM methods so tests assert on calls, not DOM state.
  // These are set synchronously; init()'s post-await code runs as microtasks
  // after this block, so the stubs are in place when init() continues.
  for (const method of [
    'displayResults',
    'loadRandomCards',
    'showLoading',
    'showError',
    'showNoResults',
    'clearResults',
    'clearMessages',
    'updateOrderToggleAppearance',
    'updatePreferVisibility',
    'updateGridColumns',
    'updateURL',
  ]) {
    search[method] = jest.fn();
  }

  // Let init() finish (it awaits fetchCommonCardTypes → Promise.resolve([])).
  await flushPromises();
});

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CardSearch request management', () => {
  // ── 1. Basic request lifecycle ──────────────────────────────────────────

  describe('basic request lifecycle', () => {
    it('does nothing for a blank query', async () => {
      await search.performSearch('   ');
      expect(fetch).not.toHaveBeenCalled();
    });

    it('fires a fetch and calls displayResults on success', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      expect(fetch).toHaveBeenCalledTimes(1);
      expect(fetch).toHaveBeenCalledWith(expectedUrl('lightning bolt'), expect.any(Object));

      fetchQueue[0].resolve(okResponse(EMPTY_RESULT));
      await flushPromises();

      expect(search.displayResults).toHaveBeenCalledTimes(1);
      expect(search.showError).not.toHaveBeenCalled();
    });

    it('calls showError and resets lastRequestedUrl on an HTTP error response', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      fetchQueue[0].resolve(errorResponse('Invalid Query', 'bad syntax'));
      await flushPromises();

      expect(search.showError).toHaveBeenCalledWith(expect.stringContaining('Invalid Query'));
      expect(search.displayResults).not.toHaveBeenCalled();
      expect(search.lastRequestedUrl).toBeNull();
    });

    it('calls showError and resets lastRequestedUrl on a network error', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      fetchQueue[0].reject(new TypeError('Failed to fetch'));
      await flushPromises();

      expect(search.showError).toHaveBeenCalledWith(expect.stringContaining('Failed to fetch'));
      expect(search.lastRequestedUrl).toBeNull();
    });

    it('sets lastRequestedUrl as soon as a request starts', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      expect(search.lastRequestedUrl).toBe(expectedUrl('lightning bolt'));
    });

    it('clears currentController after a request completes', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      expect(search.currentController).not.toBeNull();

      fetchQueue[0].resolve(okResponse(EMPTY_RESULT));
      await flushPromises();

      expect(search.currentController).toBeNull();
    });
  });

  // ── 2. Duplicate suppression ────────────────────────────────────────────

  describe('duplicate suppression', () => {
    it('skips a second call with the same URL while the first is in-flight', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      search.performSearch('lightning bolt');
      await flushPromises();

      expect(fetch).toHaveBeenCalledTimes(1);
    });

    it('skips a second call with the same URL after the first completes successfully', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();
      fetchQueue[0].resolve(okResponse(EMPTY_RESULT));
      await flushPromises();

      search.displayResults.mockClear();
      search.performSearch('lightning bolt');
      await flushPromises();

      expect(fetch).toHaveBeenCalledTimes(1);
      expect(search.displayResults).not.toHaveBeenCalled();
    });

    it('allows the same URL to fire again after an error', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();
      fetchQueue[0].resolve(errorResponse('Bad Request', 'err'));
      await flushPromises();

      expect(search.lastRequestedUrl).toBeNull();

      search.performSearch('lightning bolt');
      await flushPromises();

      expect(fetch).toHaveBeenCalledTimes(2);
    });

    it('fires a new request when the URL differs from the last', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();
      fetchQueue[0].resolve(okResponse(EMPTY_RESULT));
      await flushPromises();

      search.performSearch('counterspell');
      await flushPromises();

      expect(fetch).toHaveBeenCalledTimes(2);
    });

    it('does not cancel an in-flight request when a new query autocompletes to the same URL', async () => {
      // Seed the autocomplete data so 't:creat' completes to 't:creature'
      search.commonCardTypes = [{ t: 'creature', n: 9999 }];

      // 't:creat' autocompletes to 't:creature'; 't:creature' produces the same URL
      search.performSearch('t:creat');
      await flushPromises();
      const urlAfterAutocomplete = search.lastRequestedUrl;

      search.performSearch('t:creature'); // same URL after autocomplete — skips, does not abort
      await flushPromises();

      expect(fetch).toHaveBeenCalledTimes(1);

      // The original in-flight request completes and its results are shown
      fetchQueue[0].resolve(okResponse(EMPTY_RESULT));
      await flushPromises();

      expect(search.displayResults).toHaveBeenCalledTimes(1);
      expect(search.lastRequestedUrl).toBe(urlAfterAutocomplete);
    });
  });

  // ── 3. Superseding requests (different URL) ─────────────────────────────

  describe('superseding requests (different URL)', () => {
    // Helper: start request A, then immediately start request B (different URL).
    // Returns the controller captured for request A so tests can inspect it.
    async function startTwoRequests() {
      search.performSearch('lightning bolt');
      await flushPromises();
      const controllerA = search.currentController;

      search.performSearch('counterspell');
      await flushPromises();

      return { controllerA };
    }

    it('aborts the in-flight request when a new different-URL request starts', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      const controllerA = search.currentController;
      const abortSpy = jest.spyOn(controllerA, 'abort');

      search.performSearch('counterspell');
      await flushPromises();

      expect(abortSpy).toHaveBeenCalled();
    });

    it('fires a second fetch for the new URL', async () => {
      await startTwoRequests();
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(fetch).toHaveBeenNthCalledWith(2, expectedUrl('counterspell'), expect.any(Object));
    });

    it('does not call displayResults when the superseded request resolves', async () => {
      await startTwoRequests();
      // fetchQueue[0] was already rejected via the abort signal;
      // resolving it is a no-op (promise already settled), but we flush to
      // ensure any remaining microtasks from the AbortError path run.
      await flushPromises();

      expect(search.displayResults).not.toHaveBeenCalled();
    });

    it('does not call showError when the superseded request returns an HTTP error', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      // The second performSearch aborts request A via its signal (AbortError).
      // Simulate A returning a bad HTTP status BEFORE the signal fires — not
      // possible with the abort-respecting mock, so we test the requestId guard
      // by manually rejecting with a non-abort error AFTER the supersede.
      const resolveA = fetchQueue[0].resolve; // capture before supersede

      search.performSearch('counterspell');
      await flushPromises(); // signal fires → AbortError already in-flight for A

      // Even if we could force A's underlying promise to settle with an HTTP
      // error, it arrives after requestId was incremented — showError stays silent.
      // (This is a belt-and-suspenders check; with the signal mock A's promise
      //  already settled via AbortError, so resolveA is effectively a no-op.)
      resolveA(errorResponse('Stale Error', 'should not appear'));
      await flushPromises();

      expect(search.showError).not.toHaveBeenCalled();
    });

    it('does not reset lastRequestedUrl when the superseded request is aborted', async () => {
      await startTwoRequests();
      // AbortError for request A was already handled during flushPromises.
      expect(search.lastRequestedUrl).toBe(expectedUrl('counterspell'));
    });

    it('calls displayResults when the superseding request completes', async () => {
      await startTwoRequests();

      fetchQueue[1].resolve(okResponse(EMPTY_RESULT));
      await flushPromises();

      expect(search.displayResults).toHaveBeenCalledTimes(1);
    });

    it("does not null currentController in the superseded request's finally block", async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      search.performSearch('counterspell');
      // Capture B's controller right after it was assigned
      const controllerB = search.currentController;

      await flushPromises(); // A's AbortError + finally run here

      // B's controller must survive A's finally block
      expect(search.currentController).toBe(controllerB);
    });
  });

  // ── 4. Clear input ──────────────────────────────────────────────────────

  describe('clear input (handleSearch with empty query)', () => {
    it('aborts any in-flight request', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      const abortSpy = jest.spyOn(search.currentController, 'abort');
      search.handleSearch('');

      expect(abortSpy).toHaveBeenCalled();
    });

    it('sets currentController to null', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      search.handleSearch('');

      expect(search.currentController).toBeNull();
    });

    it('resets lastRequestedUrl', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      search.handleSearch('');

      expect(search.lastRequestedUrl).toBeNull();
    });

    it('calls clearResults', () => {
      search.handleSearch('');
      expect(search.clearResults).toHaveBeenCalled();
    });

    it('does not call displayResults after the cleared request resolves (via AbortError)', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      search.handleSearch(''); // aborts → AbortError queued as microtask
      await flushPromises(); // AbortError handler runs

      expect(search.displayResults).not.toHaveBeenCalled();
    });

    it('allows the same query to fire again after clearing', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      search.handleSearch('');
      await flushPromises(); // AbortError handled, lastRequestedUrl is null

      search.performSearch('lightning bolt');
      await flushPromises();

      expect(fetch).toHaveBeenCalledTimes(2);
    });
  });

  // ── 5. Explicit abort with no superseding request ────────────────────────

  describe('explicit abort with no superseding request', () => {
    it('resets lastRequestedUrl so the same query can be retried', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      // Abort the request without starting a new one (e.g., navigating away)
      search.currentController.abort();
      await flushPromises(); // AbortError handler runs

      expect(search.lastRequestedUrl).toBeNull();

      // Retry succeeds
      search.performSearch('lightning bolt');
      await flushPromises();

      expect(fetch).toHaveBeenCalledTimes(2);
    });

    it('does not call displayResults after an explicit abort', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      search.currentController.abort();
      await flushPromises();

      expect(search.displayResults).not.toHaveBeenCalled();
    });
  });

  // ── 6. requestId staleness guard ────────────────────────────────────────

  describe('requestId staleness guard', () => {
    it('discards displayResults from a request that resolves after a newer request started', async () => {
      // Request A (requestId 1) starts
      search.performSearch('lightning bolt');
      await flushPromises();
      const requestIdA = search.requestId;

      // Request B (requestId 2) starts — aborts A via signal
      search.performSearch('counterspell');
      await flushPromises();

      expect(search.requestId).toBe(requestIdA + 1);
      // A was already aborted by the signal; its AbortError was handled.
      // displayResults was not called.
      expect(search.displayResults).not.toHaveBeenCalled();
    });

    it('does not increment requestId when a duplicate URL is skipped', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();
      const idAfterFirst = search.requestId;

      search.performSearch('lightning bolt'); // duplicate — skipped
      await flushPromises();

      expect(search.requestId).toBe(idAfterFirst);
    });

    it('still renders results from the original in-flight request when a duplicate is skipped', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      search.performSearch('lightning bolt'); // duplicate — no abort, no new fetch
      await flushPromises();

      fetchQueue[0].resolve(okResponse(EMPTY_RESULT));
      await flushPromises();

      expect(search.displayResults).toHaveBeenCalledTimes(1);
    });

    it('discards showError from a stale request that errors after a newer request started', async () => {
      search.performSearch('lightning bolt');
      await flushPromises();

      search.performSearch('counterspell'); // supersedes — aborts A
      await flushPromises();

      // A was aborted; even if we force a non-abort error (belt-and-suspenders),
      // requestId guard must block showError and must not reset lastRequestedUrl.
      // With the abort-respecting mock, A's promise already settled via AbortError,
      // so this resolve is a no-op — but the assertion confirms the property holds.
      fetchQueue[0].resolve(errorResponse('Stale Error', 'should not appear'));
      await flushPromises();

      expect(search.showError).not.toHaveBeenCalled();
      expect(search.lastRequestedUrl).toBe(expectedUrl('counterspell'));
    });
  });

  // ── 7. Random cards on initial load ─────────────────────────────────────

  describe('loadRandomCards on init', () => {
    it('calls loadRandomCards on init when there is no initial query', () => {
      // loadRandomCards is stubbed in beforeEach; confirm init() called it
      expect(search.loadRandomCards).toHaveBeenCalledTimes(1);
    });

    it('does not call loadRandomCards when an initial query is present', async () => {
      // Push a URL with a query param so the new CardSearch sees ?q=fireball
      window.history.pushState({}, '', '/?q=fireball');

      buildDOM();
      global.fetch = buildFetchMock();
      window.commonCardTypesPromise = Promise.resolve([]);
      const s2 = new CardSearch();
      for (const m of [
        'displayResults',
        'loadRandomCards',
        'performSearch',
        'updateOrderToggleAppearance',
        'updatePreferVisibility',
        'updateURL',
      ]) {
        s2[m] = jest.fn();
      }
      await flushPromises();

      expect(s2.loadRandomCards).not.toHaveBeenCalled();

      window.history.pushState({}, '', '/');
    });

    it('fetches /random_search?num_cards=10 and calls displayResults', async () => {
      // Call the real implementation directly (bypassing the jest.fn() stub from beforeEach)
      const cards = [{ name: 'Lightning Bolt' }, { name: 'Serra Angel' }];
      global.fetch = jest.fn(() => Promise.resolve(okResponse(cards)));

      const displaySpy = jest.fn();
      search.displayResults = displaySpy;
      search.showLoading = jest.fn();

      await CardSearch.prototype.loadRandomCards.call(search);

      expect(global.fetch).toHaveBeenCalledWith('/random_search?num_cards=10', expect.any(Object));
      expect(displaySpy).toHaveBeenCalledWith(cards, null, null);
    });

    it('clears the loading message and does not call displayResults on an HTTP error', async () => {
      global.fetch = jest.fn(() => Promise.resolve(errorResponse('Server Error', 'oops', 500)));
      const displaySpy = jest.fn();
      const clearMessagesSpy = jest.fn();
      search.displayResults = displaySpy;
      search.showLoading = jest.fn();
      search.clearMessages = clearMessagesSpy;

      await CardSearch.prototype.loadRandomCards.call(search);

      expect(displaySpy).not.toHaveBeenCalled();
      expect(clearMessagesSpy).toHaveBeenCalledTimes(1);
    });

    it('clears the loading message and does not call displayResults on a network error', async () => {
      global.fetch = jest.fn(() => Promise.reject(new Error('network error')));
      const displaySpy = jest.fn();
      const clearMessagesSpy = jest.fn();
      search.displayResults = displaySpy;
      search.showLoading = jest.fn();
      search.clearMessages = clearMessagesSpy;

      await CardSearch.prototype.loadRandomCards.call(search);

      expect(displaySpy).not.toHaveBeenCalled();
      expect(clearMessagesSpy).toHaveBeenCalledTimes(1);
    });

    it('does not call displayResults or clearMessages when aborted before response arrives', async () => {
      // Fetch never resolves — simulates the random request still in-flight
      let rejectFetch;
      global.fetch = jest.fn(
        () =>
          new Promise((_, rej) => {
            rejectFetch = rej;
          })
      );
      const displaySpy = jest.fn();
      const clearMessagesSpy = jest.fn();
      search.displayResults = displaySpy;
      search.clearMessages = clearMessagesSpy;
      search.showLoading = jest.fn();

      const randomPromise = CardSearch.prototype.loadRandomCards.call(search);

      // Simulate performSearch aborting the in-flight random request
      search.currentController.abort();
      rejectFetch(abortError());

      await randomPromise;

      expect(displaySpy).not.toHaveBeenCalled();
      expect(clearMessagesSpy).not.toHaveBeenCalled();
    });

    it('sets currentController so performSearch can abort the in-flight random fetch', async () => {
      global.fetch = jest.fn(() => new Promise(() => {})); // never resolves
      search.showLoading = jest.fn();

      CardSearch.prototype.loadRandomCards.call(search);
      await flushMicrotasks();

      expect(search.currentController).not.toBeNull();
      expect(search.currentRequestUrl).toBeNull();
    });
  });

  // ── 8. Debounce ──────────────────────────────────────────────────────────

  describe('debounce (handleSearch)', () => {
    beforeEach(() => jest.useFakeTimers());

    it('does not call performSearch before the debounce delay elapses', async () => {
      const spy = jest.spyOn(search, 'performSearch');
      search.handleSearch('lightning bolt');

      jest.advanceTimersByTime(search.debounceDelay - 1);
      await flushMicrotasks();

      expect(spy).not.toHaveBeenCalled();
    });

    it('calls performSearch with the query after the debounce delay', async () => {
      const spy = jest.spyOn(search, 'performSearch');
      search.handleSearch('lightning bolt');

      jest.advanceTimersByTime(search.debounceDelay + 10);
      await flushMicrotasks();

      expect(spy).toHaveBeenCalledWith('lightning bolt');
    });

    it('cancels an earlier pending debounce when a new query is typed', async () => {
      const spy = jest.spyOn(search, 'performSearch');
      search.handleSearch('lightning');
      search.handleSearch('lightning bolt');

      jest.advanceTimersByTime(search.debounceDelay + 10);
      await flushMicrotasks();

      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy).toHaveBeenCalledWith('lightning bolt');
    });

    it('clears results immediately (no debounce) when the query is emptied', () => {
      search.handleSearch('');
      expect(search.clearResults).toHaveBeenCalled();
      // clearResults should fire synchronously — before any timer advances
    });
  });
});
