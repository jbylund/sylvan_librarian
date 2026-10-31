const UNIQUE_PRINTING = 'printing';
const DWELL_MS = 2500; // milliseconds user must stay on results before adding a history entry
const MAX_EXPLANATION_LENGTH = 140; // truncate very long query explanations (e.g. giant OR chains)
const MAX_QUERY_UTF8_BYTES = 3500;
const QUERY_TOO_LONG_MESSAGE = 'Search query exceeds the maximum allowed length.';

// Hoisted so escapeHtml() doesn't allocate a new RegExp or callback on every call.
const HTML_ESCAPE_RE = /[&<>"]/g;
const HTML_ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
const htmlEscapeChar = c => HTML_ESCAPE_MAP[c];

// Hoisted so queryUtf8ByteLength() doesn't allocate a new TextEncoder on every call —
// TextEncoder is stateless and safe to reuse across calls.
const QUERY_BYTE_ENCODER = new TextEncoder();

// Debounce delay for the resize listener, to avoid recomputing grid columns (and writing
// style.gridTemplateColumns) on every one of the many resize events a drag-resize fires.
const RESIZE_DEBOUNCE_MS = 150;

// Invert a columnar cards payload ({field: [values, ...]}) back into an array of
// card objects. Row-shaped payloads (already arrays) pass through untouched, so
// consumers are indifferent to which shape the server sent (e.g. a stale cached
// row-shaped response, or the embedded no-JS payload).
function columnsToRows(cards) {
  if (!cards || Array.isArray(cards)) return cards || [];
  const keys = Object.keys(cards);
  const count = keys.length ? cards[keys[0]].length : 0;
  return Array.from({ length: count }, (_, i) => Object.fromEntries(keys.map(k => [k, cards[k][i]])));
}

class CatalogMap {
  constructor(mapping) {
    this._words = Object.entries(mapping)
      .map(([v, n]) => ({ v, n, lower: v.toLowerCase() }))
      .sort((a, b) => a.lower.localeCompare(b.lower));

    // Sparse lookup tables: prefix string → best word for depths 1–3.
    // Built frequency-first so first-write-wins gives the highest-n word.
    this._d1 = {};
    this._d2 = {};
    this._d3 = {};
    const byFreq = [...this._words].sort((a, b) => b.n - a.n);
    for (const w of byFreq) {
      const l = w.lower;
      if (l.length >= 1 && !(l[0] in this._d1)) this._d1[l[0]] = w.v;
      if (l.length >= 2 && !(l[0] + l[1] in this._d2)) this._d2[l[0] + l[1]] = w.v;
      if (l.length >= 3 && !(l[0] + l[1] + l[2] in this._d3)) this._d3[l[0] + l[1] + l[2]] = w.v;
    }
  }

  _lowerBound(prefix) {
    let lo = 0,
      hi = this._words.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (this._words[mid].lower < prefix) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  get bool() {
    return this._words.length > 0;
  }

  get size() {
    return this._words.length;
  }

  getBestMatch(prefix) {
    if (!prefix) return null;
    if (prefix.length === 1) return this._d1[prefix] ?? null;
    if (prefix.length === 2) return this._d2[prefix] ?? null;
    if (prefix.length === 3) return this._d3[prefix] ?? null;

    const pos = this._lowerBound(prefix);
    let best = null;
    for (let i = pos; i < this._words.length; i++) {
      if (!this._words[i].lower.startsWith(prefix)) break;
      if (!best || this._words[i].n > best.n) best = this._words[i];
    }
    return best?.v ?? null;
  }
}

class CardSearch {
  constructor() {
    this.searchForm = document.querySelector('.search-container');
    this.searchInput = document.getElementById('searchInput');
    this.resultsContainer = document.getElementById('results');
    this.statusMessage = document.getElementById('statusMessage');
    this.orderDropdown = document.getElementById('orderDropdown');
    this.uniqueDropdown = document.getElementById('uniqueDropdown');
    this.preferDropdown = document.getElementById('preferDropdown');
    this.orderToggle = document.getElementById('orderToggle');
    this.directionInput = document.getElementById('directionInput');

    // Disable browser autocomplete when JavaScript is enabled
    this.searchInput.setAttribute('autocomplete', 'off');

    this.debounceTimeout = null;
    this.debounceDelay = 50; // milliseconds
    this.resizeTimeout = null;
    this.currentController = null;
    this.currentRequestUrl = null; // URL of the in-flight request, if any
    this.imageObserver = null;
    this.cardsData = new Map(); // Store card data by ID
    this.lastCompletedUrl = null; // URL whose results are currently displayed; null when results are cleared
    this.isAscending = true; // Track order direction
    this.currentCardCount = 0; // Track current number of cards displayed for resize handling

    // Autocomplete properties — Maps from first letter to sorted subarray
    this.typeMap = new CatalogMap({});
    this.keywordMap = new CatalogMap({});

    // Initialize cached regex patterns for mana symbol replacement (performance optimization)
    this.initManaSymbolPatterns();

    this.init();
  }

  initManaSymbolPatterns() {
    // Define mana symbol maps once
    const manaMap = {
      '{R}': 'ms ms-r ms-cost',
      '{G}': 'ms ms-g ms-cost',
      '{W}': 'ms ms-w ms-cost',
      '{U}': 'ms ms-u ms-cost',
      '{B}': 'ms ms-b ms-cost',
      '{C}': 'ms ms-c ms-cost',
      '{0}': 'ms ms-0 ms-cost',
      '{1}': 'ms ms-1 ms-cost',
      '{2}': 'ms ms-2 ms-cost',
      '{3}': 'ms ms-3 ms-cost',
      '{4}': 'ms ms-4 ms-cost',
      '{5}': 'ms ms-5 ms-cost',
      '{6}': 'ms ms-6 ms-cost',
      '{7}': 'ms ms-7 ms-cost',
      '{8}': 'ms ms-8 ms-cost',
      '{9}': 'ms ms-9 ms-cost',
      '{10}': 'ms ms-10 ms-cost',
      '{11}': 'ms ms-11 ms-cost',
      '{12}': 'ms ms-12 ms-cost',
      '{13}': 'ms ms-13 ms-cost',
      '{14}': 'ms ms-14 ms-cost',
      '{15}': 'ms ms-15 ms-cost',
      '{16}': 'ms ms-16 ms-cost',
      '{X}': 'ms ms-x ms-cost',
      '{Y}': 'ms ms-y ms-cost',
      '{Z}': 'ms ms-z ms-cost',
      '{T}': 'ms ms-tap',
      '{Q}': 'ms ms-untap',
      '{E}': 'ms ms-energy',
      '{P}': 'ms ms-p ms-cost',
      '{S}': 'ms ms-s ms-cost',
      '{CHAOS}': 'ms ms-chaos',
      '{PW}': 'ms ms-pw',
      '{∞}': 'ms ms-infinity',
    };

    const hybridMap = {
      '{W/U}': 'ms ms-wu ms-cost',
      '{U/B}': 'ms ms-ub ms-cost',
      '{B/R}': 'ms ms-br ms-cost',
      '{R/G}': 'ms ms-rg ms-cost',
      '{G/W}': 'ms ms-gw ms-cost',
      '{W/B}': 'ms ms-wb ms-cost',
      '{U/R}': 'ms ms-ur ms-cost',
      '{B/G}': 'ms ms-bg ms-cost',
      '{R/W}': 'ms ms-rw ms-cost',
      '{G/U}': 'ms ms-gu ms-cost',
      '{2/W}': 'ms ms-2w ms-cost',
      '{2/U}': 'ms ms-2u ms-cost',
      '{2/B}': 'ms ms-2b ms-cost',
      '{2/R}': 'ms ms-2r ms-cost',
      '{2/G}': 'ms ms-2g ms-cost',
      '{W/P}': 'ms ms-wp ms-cost',
      '{U/P}': 'ms ms-up ms-cost',
      '{B/P}': 'ms ms-bp ms-cost',
      '{R/P}': 'ms ms-rp ms-cost',
      '{G/P}': 'ms ms-gp ms-cost',
      '{W/U/P}': 'ms ms-wup ms-cost',
      '{W/B/P}': 'ms ms-wbp ms-cost',
      '{U/B/P}': 'ms ms-ubp ms-cost',
      '{U/R/P}': 'ms ms-urp ms-cost',
      '{B/R/P}': 'ms ms-brp ms-cost',
      '{B/G/P}': 'ms ms-bgp ms-cost',
      '{R/W/P}': 'ms ms-rwp ms-cost',
      '{R/G/P}': 'ms ms-rgp ms-cost',
      '{G/W/P}': 'ms ms-gwp ms-cost',
      '{G/U/P}': 'ms ms-gup ms-cost',
    };

    const manaTextMap = {
      '{W}': '☀️',
      '{U}': '💧',
      '{B}': '💀',
      '{R}': '🔥',
      '{G}': '🌳',
      '{C}': '◇',
      '{T}': '↻',
      '{Q}': '↺',
      '{E}': '⚡',
      '{P}': 'Φ',
      '{S}': '❄',
      '{X}': 'X',
      '{Y}': 'Y',
      '{Z}': 'Z',
      '{0}': '⓪',
      '{1}': '①',
      '{2}': '②',
      '{3}': '③',
      '{4}': '④',
      '{5}': '⑤',
      '{6}': '⑥',
      '{7}': '⑦',
      '{8}': '⑧',
      '{9}': '⑨',
      '{10}': '⑩',
      '{11}': '⑪',
      '{12}': '⑫',
      '{13}': '⑬',
      '{14}': '⑭',
      '{15}': '⑮',
      '{16}': '⑯',
      '{CHAOS}': '🌀',
      '{PW}': 'PW',
      '{∞}': '♾︎',
      '{W/U}': '(☀️/💧)',
      '{U/B}': '(💧/💀)',
      '{B/R}': '(💀/🔥)',
      '{R/G}': '(🔥/🌳)',
      '{G/W}': '(🌳/☀️)',
      '{W/B}': '(☀️/💀)',
      '{U/R}': '(💧/🔥)',
      '{B/G}': '(💀/🌳)',
      '{R/W}': '(🔥/☀️)',
      '{G/U}': '(🌳/💧)',
      '{2/W}': '(②/☀️)',
      '{2/U}': '(②/💧)',
      '{2/B}': '(②/💀)',
      '{2/R}': '(②/🔥)',
      '{2/G}': '(②/🌳)',
      '{W/P}': '(☀️/Φ)',
      '{U/P}': '(💧/Φ)',
      '{B/P}': '(💀/Φ)',
      '{R/P}': '(🔥/Φ)',
      '{G/P}': '(🌳/Φ)',
      '{W/U/P}': '(☀️/💧/Φ)',
      '{W/B/P}': '(☀️/💀/Φ)',
      '{U/B/P}': '(💧/💀/Φ)',
      '{U/R/P}': '(💧/🔥/Φ)',
      '{B/R/P}': '(💀/🔥/Φ)',
      '{B/G/P}': '(💀/🌳/Φ)',
      '{R/W/P}': '(🔥/☀️/Φ)',
      '{R/G/P}': '(🔥/🌳/Φ)',
      '{G/W/P}': '(🌳/☀️/Φ)',
      '{G/U/P}': '(🌳/💧/Φ)',
    };

    // Cache the merged symbol map for convertManaSymbols
    // Use simple pattern that matches any content between braces (1-5 chars)
    // Use Map for O(1) lookup with single get() operation
    this.manaSymbolsMap = new Map(Object.entries({ ...hybridMap, ...manaMap }));
    this.manaSymbolsRegex = /\{[^}]{1,5}\}/g;

    // Cache the text map for convertManaSymbolsToText
    this.manaTextMap = new Map(Object.entries(manaTextMap));
    this.manaTextRegex = /\{[^}]{1,5}\}/g;
  }

  async init() {
    // Fetch common card types in background — only needed for autocomplete
    this.fetchCommonCardTypes();

    // On page load, check for query params and restore state
    const params = new URLSearchParams(window.location.search);
    const initialQuery = params.get('q') || '';
    const initialOrder = params.get('orderby') || 'edhrec';
    const initialDirection = params.get('direction') || 'asc';
    const initialUnique = params.get('unique') || 'card';
    const initialPrefer = params.get('prefer') || 'default';

    // Set the order controls to match URL params
    this.orderDropdown.value = initialOrder;
    this.uniqueDropdown.value = initialUnique;
    this.preferDropdown.value = initialPrefer;
    this.isAscending = initialDirection === 'asc';
    this.directionInput.value = initialDirection;
    this.updateOrderToggleAppearance();
    this.updatePreferVisibility();

    if (initialQuery) {
      this.searchInput.value = initialQuery;
      // Record arrival time so we only push this state when leaving if they stayed > DWELL_MS
      const initialUrl = this.buildCurrentSearchUrl();
      window.history.replaceState({ arrivalTime: Date.now() }, '', initialUrl);
      // Check if we have embedded search results from the server
      if (window.EMBEDDED_SEARCH_RESULTS) {
        // Use the embedded results directly without making an API call
        this.displayResults(window.EMBEDDED_SEARCH_RESULTS, initialQuery, null);
        // Clear the embedded results so they're not reused
        delete window.EMBEDDED_SEARCH_RESULTS;
      } else {
        // No embedded results, perform the search via API
        this.performSearch(initialQuery);
      }
    } else {
      // No query on load — show a random selection of cards as a discovery prompt
      this.loadRandomCards();
    }

    // Back/forward: restore search state from URL and re-fetch results
    window.addEventListener('popstate', () => {
      const params = new URLSearchParams(window.location.search);
      const q = params.get('q') || '';
      const orderby = params.get('orderby') || 'edhrec';
      const direction = params.get('direction') || 'asc';
      const unique = params.get('unique') || 'card';
      const prefer = params.get('prefer') || 'default';

      this.searchInput.value = q;
      this.orderDropdown.value = orderby;
      this.uniqueDropdown.value = unique;
      this.preferDropdown.value = prefer;
      this.isAscending = direction === 'asc';
      this.directionInput.value = direction;
      this.updateOrderToggleAppearance();
      this.updatePreferVisibility();

      if (q) {
        this.performSearch(q);
      } else {
        clearTimeout(this.debounceTimeout);
        this.currentController?.abort();
        this.lastCompletedUrl = null;
        this.clearResults();
      }
    });

    // Prevent form submission when JavaScript is enabled
    this.searchForm.addEventListener('submit', e => {
      e.preventDefault();
      clearTimeout(this.debounceTimeout);
      this.performSearch(this.searchInput.value);
    });

    this.searchInput.addEventListener('input', e => {
      const query = e.target.value;
      this.handleSearch(query);
      // Update the URL as the user types
      const order = this.orderDropdown.value;
      const unique = this.uniqueDropdown.value;
      const prefer = this.preferDropdown.value;
      const direction = this.isAscending ? 'asc' : 'desc';
      this.updateURL(query, order, direction, unique, prefer);
    });

    // Handle enter key for immediate search
    this.searchInput.addEventListener('keypress', e => {
      if (e.key === 'Enter') {
        clearTimeout(this.debounceTimeout);
        this.performSearch(e.target.value);
      }
    });

    // Add event delegation for card clicks
    this.resultsContainer.addEventListener('click', e => {
      const cardItem = e.target.closest('.card-item');
      if (!cardItem) return;
      // Modifier clicks (ctrl/cmd) on the card-page link open the card page in a new tab.
      // Middle-click fires auxclick, not click, so it also passes through naturally.
      if ((e.ctrlKey || e.metaKey) && e.target.closest('.card-page-link')) return;
      e.preventDefault();
      this.handleCardClick(cardItem);
    });

    // Add click handler for header to clear search
    document.querySelector('.header h1').addEventListener('click', () => {
      this.clearSearch();
    });

    // Add event listeners for order controls
    this.orderDropdown.addEventListener('change', () => {
      this.handleOrderChange();
    });

    this.uniqueDropdown.addEventListener('change', () => {
      this.updatePreferVisibility();
      this.handleUniqueChange();
    });

    this.preferDropdown.addEventListener('change', () => {
      this.handlePreferChange();
    });

    this.orderToggle.addEventListener('click', () => {
      this.toggleOrderDirection();
    });

    // Add resize listener to update columns dynamically. Debounced because a drag-resize
    // fires many resize events per second, and each recompute writes gridTemplateColumns,
    // forcing a style/layout recalc.
    window.addEventListener('resize', () => {
      clearTimeout(this.resizeTimeout);
      this.resizeTimeout = setTimeout(() => {
        this.updateGridColumns(this.currentCardCount);
      }, RESIZE_DEBOUNCE_MS);
    });
  }

  handleSearch(query) {
    // Clear previous timeout
    clearTimeout(this.debounceTimeout);

    // Clear results if query is empty
    if (!query.trim()) {
      this.currentController?.abort();
      this.lastCompletedUrl = null;
      this.clearResults();
      return;
    }

    // If the query has changed from what's currently in-flight, abort immediately
    // rather than waiting for the debounce to fire a new request.
    if (this.currentController && !this.currentController.signal.aborted) {
      const inFlightQuery = this.currentRequestUrl
        ? new URLSearchParams(this.currentRequestUrl.split('?')[1]).get('q')
        : null;
      if (inFlightQuery !== this._processQuery(query)) {
        this.currentController.abort();
      }
    }

    // Set up debounced search
    this.debounceTimeout = setTimeout(() => {
      this.performSearch(query);
    }, this.debounceDelay);
  }

  // Applies autocomplete, bracket-balancing, and whitespace normalisation to a raw query string.
  _processQuery(query) {
    return this._balanceAndNormalize(this.autoCompleteQuery(query));
  }

  // The second half of _processQuery, split out so performSearch can inspect the autocompleted query
  // before balancing hides whether a span was left empty.
  _balanceAndNormalize(autocompleted) {
    return this.collapseWhitespaceOutsideSpans(this.balanceQuery(autocompleted)).trim();
  }

  // Returns the index closing the quoted string, /regex/, or {mana symbol} that opens at i, or null if
  // `query[i]` doesn't open one there, or it never closes — callers that only act on a *closed* span
  // treat both cases identically, so they don't need to tell them apart. scanSpans doesn't use this: it
  // has to act on the unterminated case too (closing the span, reporting where its content started),
  // so it calls quoteCloseIndex/regexCloseIndex/braceCloseIndex directly.
  // The JS counterpart of no single Python function — spans.py's callers each make this same
  // three-way dispatch themselves, since hand_parser's lexer and parsing_f's balancer need the
  // unterminated case too.
  // An apostrophe preceded by a word character and followed by either another word character or
  // NOTHING is part of the word, not an opening quote — the same rule _scan_word_end applies in
  // the tokenizer, and the two must agree exactly or this sends something the API rejects.
  // Without the "or nothing", typing "urza'" on the way to "urza's" sent "urza''", which parses
  // as `urza` AND an empty quoted string: results silently widened to every card containing
  // "urza", and the count line read "35 cards where the name contains Urza and " — a dangling
  // conjunction, with the apostrophe the user typed dropped from the search entirely.
  isWordApostrophe(query, i) {
    const wordChar = /[\p{L}\p{N}_.]/u;
    return (
      query[i] === "'" &&
      i > 0 &&
      wordChar.test(query[i - 1]) &&
      (i + 1 >= query.length || wordChar.test(query[i + 1]))
    );
  }

  closedSpanEnd(query, i) {
    const char = query[i];
    if ((char === '"' || char === "'") && !this.isWordApostrophe(query, i)) {
      return this.quoteCloseIndex(query, i + 1, char);
    }
    if (char === '/' && this.opensRegex(query, i)) {
      return this.regexCloseIndex(query, i + 1);
    }
    if (char === '{') {
      return this.braceCloseIndex(query, i + 1);
    }
    return null;
  }

  // Collapses runs of whitespace to a single space, except inside a quoted string or /regex/ (mana
  // symbols never contain whitespace, so they need no protection here). A plain `.replace(/\s+/g, ' ')`
  // over the whole query would silently rewrite `o:/a  b/` to `o:/a b/` and `o:"draw  a  card"` to
  // `o:"draw a card"`, changing what the user actually typed before it ever reaches the server.
  collapseWhitespaceOutsideSpans(query) {
    let out = '';

    for (let i = 0; i < query.length; i++) {
      const closeIndex = this.closedSpanEnd(query, i);
      if (closeIndex !== null) {
        out += query.slice(i, closeIndex + 1);
        i = closeIndex;
        continue;
      }

      const char = query[i];
      if (/\s/.test(char)) {
        if (!out.endsWith(' ')) {
          out += ' ';
        }
      } else {
        out += char;
      }
    }

    return out;
  }

  async fetchCommonCardTypes() {
    // Use the promise that was started at the very top of the page (before CSS parsing)
    const data = await (window.commonCardTypesPromise || Promise.resolve({ types: {}, keywords: {} }));
    const types = data?.types || {};
    const keywords = data?.keywords || {};
    this.typeMap = new CatalogMap(types);
    this.keywordMap = new CatalogMap(keywords);
    console.debug('Loaded', this.typeMap.size, 'common card types,', this.keywordMap.size, 'keywords');
  }

  autoCompleteQuery(query) {
    const catalogMatch = query.match(/(?:^|\s)-?(kw|keyword|t|type):([a-zA-Z]{2,})$/i);
    if (!catalogMatch) {
      return query;
    }

    const selector = catalogMatch[1].toLowerCase();
    const originalPrefix = catalogMatch[2];
    const prefix = originalPrefix.toLowerCase();
    const isKeywordSelector = selector === 'kw' || selector === 'keyword';
    const catalog = isKeywordSelector ? this.keywordMap : this.typeMap;
    const bestMatch = catalog.getBestMatch(prefix);
    console.debug(`autocomplete: "${prefix}" → "${bestMatch}"`);

    if (!bestMatch) {
      return query;
    }

    let completion;
    if (originalPrefix === originalPrefix.toUpperCase()) {
      completion = bestMatch.toUpperCase();
    } else if (originalPrefix === originalPrefix.toLowerCase()) {
      completion = bestMatch.toLowerCase();
    } else {
      completion = originalPrefix + bestMatch.slice(originalPrefix.length);
    }

    return query.replace(/(?:^|\s)-?(?:kw|keyword|t|type):[a-zA-Z]+$/i, match => {
      return match.replace(/[a-zA-Z]+$/, completion);
    });
  }

  // True when the '/' at slashIndex opens a regex rather than being division. Mirrors the lexer's
  // rule in hand_parser.tokenize: a regex only opens in value position, i.e. directly after a
  // comparison operator (every one of : = != >= <= > < ends in one of these characters).
  // The JS counterpart of opens_regex in api/parsing/spans.py.
  opensRegex(query, slashIndex) {
    let i = slashIndex - 1;
    while (i >= 0 && /\s/.test(query[i])) {
      i--;
    }
    return i >= 0 && ':=><'.includes(query[i]);
  }

  // Returns { closeIndex, danglingEscape } from a single walk: closeIndex is the index of the next
  // unescaped closer at or after start, or null if it never closes; danglingEscape is true only when
  // the walk instead ran off the end of query on an unfinished backslash escape. A caller that only
  // needs the index (regexCloseIndex/quoteCloseIndex, and every closed-span check) can ignore the
  // second value; scanSpans, completing an unterminated span, needs both — computing them in one walk
  // is the only way to get both without reading the same tail of query twice.
  // The JS counterpart of find_close_index in api/parsing/spans.py.
  findCloser(query, start, closer) {
    for (let i = start; i < query.length; i++) {
      if (query[i] === '\\') {
        if (i + 1 >= query.length) {
          return { closeIndex: null, danglingEscape: true };
        }
        i++;
      } else if (query[i] === closer) {
        return { closeIndex: i, danglingEscape: false };
      }
    }
    return { closeIndex: null, danglingEscape: false };
  }

  regexCloseIndex(query, start) {
    return this.findCloser(query, start, '/').closeIndex;
  }

  quoteCloseIndex(query, start, quote) {
    return this.findCloser(query, start, quote).closeIndex;
  }

  // Returns the index of the '}' closing a mana symbol whose content starts at start, or null when
  // there is none. A plain search: no escape sequence exists inside a mana symbol.
  // The JS counterpart of brace_close_index in api/parsing/spans.py.
  braceCloseIndex(query, start) {
    const index = query.indexOf('}', start);
    return index < 0 ? null : index;
  }

  // Returns the suffix that closes a span left open on a danglingEscape or not.
  // A trailing backslash has nothing to escape yet, so appending the closer on its own
  // would escape that instead of ending the span — escape the backslash first.
  // The JS counterpart of _closer_for_partial_span in api/parsing/parsing_f.py.
  closerForPartialSpan(danglingEscape, closer) {
    return (danglingEscape ? '\\' : '') + closer;
  }

  // One pass over the query's spans and parens, reporting everything callers need:
  //   suffix — the closing text needed to balance it, or null when a ')' has no matching opener
  //            (the JS counterpart of balance_partial_query's ValueError in api/parsing/parsing_f.py)
  //   openSpanContentStart — index where an unterminated span's content begins, or null if every span
  //            is closed. Only used to tell an empty span from one with something in it.
  //   blanked — query with every span's body blanked, the same output blankOpaqueSpans would produce
  //            on `query + suffix` (computed here instead of by a second walk over the balanced
  //            string — the boundaries are exactly what this scan already finds). An unterminated
  //            span is blanked as though `suffix` had already closed it, since it always will be: per
  //            the note above, nothing follows it in `query`, so its trailing ')'s (added once below,
  //            the same way `suffix` itself is) reproduce what blanking `query + suffix` would give.
  // balanceSuffix, performSearch's empty-span guard, and validateQuery's opaque-span check all read
  // this, so there is one scan and one set of rules.
  scanSpans(query) {
    const quoteChars = new Set(["'", '"']);

    let openSpanContentStart = null;
    let openParens = 0;
    // Closer for whichever span is still open at the end of the query. Only one is ever needed,
    // because everything after an unterminated opener is span content — there is nothing left to
    // open, and nothing after it to close. That is also why it can be appended before the parens: an
    // unterminated span is necessarily the innermost thing open.
    let spanSuffix = '';
    let blanked = '';

    for (let i = 0; i < query.length; i++) {
      const char = query[i];

      // An apostrophe inside a word is content, not an opening quote — see isWordApostrophe.
      if (this.isWordApostrophe(query, i)) {
        blanked += char;
        continue;
      }

      // A quoted string, a /regex/ and a {mana symbol} are all opaque: the quotes and parens inside
      // them are content, not delimiters.
      if (quoteChars.has(char)) {
        const { closeIndex, danglingEscape } = this.findCloser(query, i + 1, char);
        if (closeIndex === null) {
          spanSuffix = this.closerForPartialSpan(danglingEscape, char);
          openSpanContentStart = i + 1;
          blanked += char + char;
          break;
        }
        blanked += char + char;
        i = closeIndex;
        continue;
      }

      // A '/' in value position opens a regex; anywhere else it is division, an ordinary character.
      if (char === '/') {
        if (this.opensRegex(query, i)) {
          const { closeIndex, danglingEscape } = this.findCloser(query, i + 1, '/');
          if (closeIndex === null) {
            // Still being typed. Close the regex rather than reading on, or the metacharacters the
            // user has typed so far get balanced as query structure: `o:/[)` is a partial `o:/[)]/`,
            // not a stray ')'.
            spanSuffix = this.closerForPartialSpan(danglingEscape, '/');
            openSpanContentStart = i + 1;
            blanked += '//';
            break;
          }
          blanked += '//';
          i = closeIndex;
          continue;
        }
        blanked += char;
        continue;
      }

      // A {mana symbol} is opaque whatever it holds, and an unterminated one gets closed for the same
      // reason an unterminated quote does: the lexer demands a '}' for every '{', so leaving it open
      // would make `mana:{` — a prefix of `mana:{W}` — unlexable while it is being typed. No escapes
      // exist inside a mana symbol, so there is no dangling-backslash case here.
      if (char === '{') {
        const closeIndex = this.braceCloseIndex(query, i + 1);
        if (closeIndex === null) {
          spanSuffix = '}';
          openSpanContentStart = i + 1;
          blanked += '{}';
          break;
        }
        blanked += '{}';
        i = closeIndex;
        continue;
      }

      if (char === '(') {
        openParens++;
      } else if (char === ')') {
        if (openParens === 0) {
          return { suffix: null, openSpanContentStart: null, blanked: null }; // ')' with no opener — nothing fixes this
        }
        openParens--;
      }
      blanked += char;
    }

    // The trailing ')'s are appended once here, the same way suffix's are — whether the loop broke
    // on an unterminated span or ran to completion with parens still open, they're always the very
    // last thing in `query + suffix`, and always outside any span at that point.
    return {
      suffix: spanSuffix + ')'.repeat(openParens),
      openSpanContentStart,
      blanked: blanked + ')'.repeat(openParens),
    };
  }

  balanceSuffix(query) {
    return this.scanSpans(query).suffix;
  }

  // Balance parentheses for typeahead searches, skipping over quotes, regexes and mana symbols;
  // unbalance-able queries are returned unchanged so validateQuery reports them.
  balanceQuery(query) {
    const suffix = this.balanceSuffix(query);
    return suffix === null ? query : query + suffix;
  }

  // Blanks the body of every quoted string, closed /regex/ span, and {mana symbol}, keeping the
  // delimiters, so the structural checks in validateQuery cannot fire on a ':' or ')' that is
  // really string, pattern, or mana content. Built on the same closedSpanEnd dispatch as
  // collapseWhitespaceOutsideSpans, because a fourth opinion about where a span starts is a fourth way
  // to reject a query the parser accepts (o:/x:\)/).
  blankOpaqueSpans(query) {
    let out = '';

    for (let i = 0; i < query.length; i++) {
      const char = query[i];
      const closeIndex = this.closedSpanEnd(query, i);
      if (closeIndex !== null) {
        // '""'/"''" for a quote and '//' for a regex share their open and close character; a mana
        // symbol doesn't, so '{}' can't come from doubling char.
        out += char === '{' ? '{}' : char + char;
        i = closeIndex;
        continue;
      }

      // An unterminated quote, regex, or mana symbol is not a span yet, so it stays an ordinary character.
      out += char;
    }

    return out;
  }

  queryUtf8ByteLength(query) {
    return QUERY_BYTE_ENCODER.encode(query).length;
  }

  // Returns an error string if the query is structurally invalid, or null if it looks ok.
  // alreadyBalanced lets a caller that just ran scanSpans itself (and knows the answer was not
  // null) skip a second identical scan here, rather than re-discovering what it already knows.
  // blanked lets that same caller pass the scan's blanked-span output straight through too, rather
  // than making blankOpaqueSpans re-walk the whole string to find the same span boundaries again.
  validateQuery(query, { alreadyBalanced = false, blanked = null } = {}) {
    if (this.queryUtf8ByteLength(query) > MAX_QUERY_UTF8_BYTES) {
      return QUERY_TOO_LONG_MESSAGE;
    }

    // A closing paren with no matching opener can't be balanced away.
    if (!alreadyBalanced && this.balanceSuffix(query) === null) {
      return `Failed to parse query: "${query}"`;
    }

    // Blank quoted strings and regex patterns so we don't match content inside them.
    const q = blanked ?? this.blankOpaqueSpans(query);

    // Trailing AND/OR with no right operand: "name:test and", "power>1 or"
    if (/(?:^|\s)(and|or)\s*$/i.test(q)) {
      return `Failed to parse query: "${query}"`;
    }

    // Any word followed by : with no value: "t:" at end, or "(t:)" where ) follows immediately.
    if (/\b\w+\s*:\s*(?:$|\))/.test(q)) {
      return `Failed to parse query: "${query}"`;
    }

    return null;
  }

  async performSearch(query) {
    if (!query.trim()) {
      return;
    }

    const autocompleted = this.autoCompleteQuery(query);

    // One scan of autocompleted serves both the empty-span guard below and the balancing that
    // follows, instead of each independently re-scanning the same string on every keystroke.
    // openSpanContentStart is a position in autocompleted, not in a trimmed copy of it, but that
    // doesn't change what it means: trailing whitespace can't be a span closer, so the position a
    // span was left open at is the same whether or not the string is trimmed first.
    const { suffix, openSpanContentStart, blanked } = this.scanSpans(autocompleted);

    // Mid-span with nothing typed after the opener: wait rather than search. Balancing would turn
    // `o:/` into the empty pattern `o://`, which matches every card and cannot use an index, and
    // `mana:{` into the non-cost `mana:{}`. Neither is what the user is on their way to typing, so
    // this has to be asked before balancing closes the span and hides that it was empty.
    //
    // clearMessages, not a bare return: handleSearch aborts the in-flight request as soon as the
    // typed query stops matching it, so backspacing `o:/a/` down to `o:/` cancels the fetch and
    // lands here with the previous showLoading still on screen. Returning without touching the
    // status container left "Searching o:/a/…" over an empty grid until the next searchable
    // keystroke. Clearing converges on the same "nothing to show" state as an empty query.
    if (openSpanContentStart !== null && autocompleted.slice(openSpanContentStart).trim() === '') {
      this.clearMessages();
      return;
    }

    // collapseWhitespaceOutsideSpans, not a plain `.replace(/\s+/g, ' ')`: the latter would collapse
    // whitespace inside a quote or regex too, silently rewriting what the user typed (see that
    // function's docstring). balanced reuses the suffix scanSpans already found above, rather than
    // letting balanceQuery re-scan the same string for it.
    const balanced = suffix === null ? autocompleted : autocompleted + suffix;
    const normalizedQuery = this.collapseWhitespaceOutsideSpans(balanced).trim();

    // suffix !== null means scanSpans already proved this string balanceable, and its blanked output
    // already reflects every span's boundaries (whitespace count and trimming don't change which
    // structural checks fire, so it's as good a check-target as normalizedQuery's own blanked form)
    // — so validateQuery doesn't need blankOpaqueSpans to re-walk the string a second time either.
    const validationError = this.validateQuery(normalizedQuery, { alreadyBalanced: suffix !== null, blanked });
    if (validationError) {
      this.showError(`Failed to search: Invalid Search Query: ${validationError}`);
      return;
    }

    // Get current order settings
    const order = this.orderDropdown.value;
    const unique = this.uniqueDropdown.value;
    const prefer = this.preferDropdown.value;
    const orderDirection = this.isAscending ? 'asc' : 'desc';

    // Generate the URL for this request
    const url = `/search?q=${encodeURIComponent(normalizedQuery)}&orderby=${order}&direction=${orderDirection}&unique=${unique}&prefer=${prefer}&shape=columnar`;

    // Same URL already in-flight (and not already aborted) — let it finish
    if (this.currentRequestUrl === url && !this.currentController.signal.aborted) return;
    // Same URL already completed — results are already showing
    if (this.lastCompletedUrl === url) return;

    // Different URL: abort in-flight and start fresh
    this.currentController?.abort();
    const controller = new AbortController();
    this.currentController = controller;
    this.currentRequestUrl = url;
    this.lastCompletedUrl = null; // cleared until this search successfully completes

    this.showLoading(normalizedQuery);

    try {
      // Clear any previous resource timing entries for this URL
      performance.clearResourceTimings && performance.clearResourceTimings();
      // Take a timestamp just before sending the request
      const startTimestampMs = performance.now();
      const response = await fetch(url, {
        method: 'GET',
        headers: {
          Accept: 'application/json',
        },
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;

      if (!response.ok) {
        // Try to get the error message from the response body
        let errorMessage = `HTTP error! status: ${response.status}`;
        try {
          const errorData = await response.json();
          if (controller.signal.aborted) return;
          if (errorData.title && errorData.description) {
            // If description is an object (like with 500 errors), just use the title
            if (typeof errorData.description === 'object') {
              errorMessage = errorData.title;
            } else {
              errorMessage = `${errorData.title}: ${errorData.description}`;
            }
          } else if (errorData.description) {
            // Only use description if it's a string, not an object
            if (typeof errorData.description === 'string') {
              errorMessage = errorData.description;
            }
          }
        } catch {
          // If we can't parse the error response, use the generic message
        }
        throw new Error(errorMessage);
      }

      const data = await response.json();
      if (controller.signal.aborted) return;

      // Compute round-trip duration from our own timestamps
      const computedRoundTripMs = Math.round(performance.now() - startTimestampMs);

      // Use PerformanceResourceTiming to get the network time
      let elapsed = null;
      const resources = performance.getEntriesByType('resource');
      // Find the most recent entry for this URL
      // (If there are multiple, pick the last one)
      const matching = resources.filter(e => e.name.includes(url));
      if (matching.length > 0) {
        const entry = matching[matching.length - 1];
        // responseEnd - startTime is the total time as shown in dev tools
        elapsed = Math.round(entry.responseEnd - entry.startTime);
      }
      // Use the minimum of PerformanceResourceTiming and our computed duration
      if (typeof elapsed === 'number') {
        elapsed = Math.min(elapsed, computedRoundTripMs);
      } else {
        elapsed = computedRoundTripMs;
      }

      if (controller.signal.aborted) return;
      this.lastCompletedUrl = url;
      this.displayResults(data, normalizedQuery, elapsed);
    } catch (error) {
      if (error.name === 'AbortError') return;
      console.error('Search error:', error);
      this.showError(`Failed to search: ${error.message}`);
    } finally {
      // Only clear the shared references if they still belong to this request
      if (this.currentController === controller) {
        this.currentController = null;
        this.currentRequestUrl = null;
      }
    }
  }

  displayResults(data, query, elapsed) {
    const cards = columnsToRows(data.cards);
    const totalCards = data.total_cards || cards.length;
    const queryExplanation = data.query_explanation || '';

    if (cards.length === 0) {
      this.showResults(totalCards, query, queryExplanation, elapsed);
      return;
    }

    // Clear previous card data and store new cards
    this.cardsData.clear();
    cards.forEach((card, index) => {
      const cardId = index.toString();
      console.debug('Storing card with ID:', cardId, 'Card data:', card);
      this.cardsData.set(cardId, card);
    });

    console.debug('Total cards stored in cardsData:', this.cardsData.size);
    console.debug('CardsData keys:', Array.from(this.cardsData.keys()));

    this.showResults(totalCards, query, queryExplanation, elapsed);

    // Store card count for resize handling
    this.currentCardCount = cards.length;

    // Set max columns based on card count to prevent more columns than cards
    this.updateGridColumns(cards.length);

    // If the server already rendered cards into the DOM (SSR), skip re-rendering.
    // This preserves early image loads that the browser started from the HTML, which
    // dramatically improves LCP — re-rendering would discard those in-flight requests.
    const hasSSRContent = this.resultsContainer && this.resultsContainer.children.length > 0;
    if (!hasSSRContent) {
      // Calculate number of columns in the first row for fetchpriority
      const firstRowCount = this.calculateFirstRowCount(cards.length);

      this.resultsContainer.innerHTML = cards
        .map((card, index) => this.createCardHTML(card, index, index < firstRowCount))
        .join('');
    }

    // Record arrival time; we only push this state when leaving if they stayed > DWELL_MS and it's not already saved (updateURL)
    const url = this.buildCurrentSearchUrl();
    window.history.replaceState({ arrivalTime: Date.now() }, '', url);
  }

  getColumnsFromViewportWidth() {
    // Determine columns based on screen width breakpoints
    const viewportWidth = window.innerWidth;

    if (viewportWidth < 410) {
      return 1;
    } else if (viewportWidth < 750) {
      return 2;
    } else if (viewportWidth < 1370) {
      return 3;
    } else if (viewportWidth < 2500) {
      return 4;
    } else {
      return 5;
    }
  }

  calculateFirstRowCount(cardCount) {
    // Calculate how many cards fit in the first row based on viewport width and card count
    const columnsFromWidth = this.getColumnsFromViewportWidth();

    // Return the minimum of columns from width and card count
    return Math.min(columnsFromWidth, cardCount);
  }

  updateGridColumns(cardCount) {
    // Only update if we have cards displayed
    if (cardCount === 0) {
      return;
    }

    // Determine columns based on screen width breakpoints
    const columnsFromWidth = this.getColumnsFromViewportWidth();

    // Use the minimum of columns from width and card count
    const actualColumns = Math.min(columnsFromWidth, cardCount);

    // Set the grid-template-columns directly
    this.resultsContainer.style.gridTemplateColumns = `repeat(${actualColumns}, 1fr)`;
  }

  buildImageUrl(card, size) {
    const face = card.face_idx || 1;
    return `https://d1hot9ps2xugbc.cloudfront.net/img/${card.set_code}/${card.collector_number}/${face}/${size}.webp`;
  }

  createCardHTML(card, index, isFirstRow = false) {
    const cardId = index.toString();

    // Build image URLs for srcset - using 4 sizes uniformly spread between 280 and 745
    const image280 = this.buildImageUrl(card, '280');
    const image388 = this.buildImageUrl(card, '388');
    const image538 = this.buildImageUrl(card, '538');
    const image745 = this.buildImageUrl(card, '745');

    // Debug logging
    console.debug('Creating card HTML for:', card);
    console.debug('Card ID will be:', cardId);

    // Create descriptive alt text with card name, mana cost, and oracle text
    let altText = this.escapeHtml(card.name || 'Unknown Card');
    if (card.mana_cost) {
      // Convert mana symbols to Unicode for alt text
      const manaTextRepresentation = this.convertManaSymbolsToText(card.mana_cost);
      altText += ` / ${this.escapeHtml(manaTextRepresentation)}`;
    }
    altText += '\n\n';
    if (card.oracle_text) {
      // Convert mana symbols in oracle text to Unicode for alt text first, then truncate.
      // Truncate on code points (not UTF-16 units) so an emoji can't be split in half
      // and the cutoff matches Python string slicing in noscript_helpers.
      const oracleTextWithSymbols = this.convertManaSymbolsToText(card.oracle_text);
      const maxLength = 300;
      let truncatedText = oracleTextWithSymbols;
      // Code-point count is always <= UTF-16 length, so short strings can skip the array
      if (oracleTextWithSymbols.length > maxLength) {
        const codePoints = Array.from(oracleTextWithSymbols);
        if (codePoints.length > maxLength) {
          truncatedText = codePoints.slice(0, maxLength).join('') + '...';
        }
      }
      altText += this.escapeHtml(truncatedText);
    }

    // Build srcset and sizes for responsive images
    // sizes attribute matches the grid breakpoints:
    // - < 410px: 1 column (100vw minus padding/gap)
    // - 410-750px: 2 columns (50vw minus gap/padding)
    // - 750-1370px: 3 columns (33.33vw minus gap/padding)
    // - 1370-2500px: 4 columns (25vw minus gap/padding)
    // - >= 2500px: 5 columns (20vw minus gap/padding)
    const srcset = `${this.escapeHtml(image280)} 280w, ${this.escapeHtml(image388)} 388w, ${this.escapeHtml(image538)} 538w, ${this.escapeHtml(image745)} 745w`;
    const sizes =
      '(max-width: 409px) calc(100vw - 3.6em), (max-width: 749px) calc(50vw - 2.6em - 7.5px), (max-width: 1369px) calc(33.33vw - 2.27em - 10px), (max-width: 2499px) calc(25vw - 2.1em - 11.25px), calc(20vw - 2em - 12px)';

    // Use 388px as default src (good middle ground for initial load)
    // Add fetchpriority="high" for first row cards to improve LCP
    // Add loading="lazy" for non-first-row images to improve initial load
    const fetchPriorityAttr = isFirstRow ? ' fetchpriority="high"' : '';
    const loadingAttr = isFirstRow ? '' : ' loading="lazy"';
    const imgTag = `<img class="card-image" src="${this.escapeHtml(image388)}" srcset="${srcset}" sizes="${sizes}" alt="${altText}" title="${altText}"${fetchPriorityAttr}${loadingAttr} />`;
    const imageHtml =
      card.set_code && card.collector_number
        ? `<a href="/card/${this.escapeHtml(card.set_code)}/${this.escapeHtml(card.collector_number)}" class="card-page-link">${imgTag}</a>`
        : imgTag;

    // Truncate oracle text without cutting a mana symbol in half — matches Python create_card_html
    let oracleHtml = '';
    if (card.oracle_text) {
      if (card.oracle_text.length > 200) {
        let truncated = card.oracle_text.substring(0, 200);
        // If we're in the middle of a mana symbol (unclosed brace), back up to before it
        if ((truncated.match(/\{/g) || []).length > (truncated.match(/\}/g) || []).length) {
          truncated = truncated.substring(0, truncated.lastIndexOf('{'));
        }
        oracleHtml = `<div class="card-text">${this.formatCardText(truncated, false, true)}...</div>`;
      } else {
        oracleHtml = `<div class="card-text">${this.formatCardText(card.oracle_text, false, true)}</div>`;
      }
    }

    return `
       <div class="card-item" data-card-id="${this.escapeHtml(cardId)}">
           ${imageHtml}
           <div class="card-name-mana-row">
               <div class="card-name">${this.escapeHtml(card.name || 'Unknown Card')}</div>
               ${card.mana_cost ? `<div class="card-mana">${this.formatCardText(card.mana_cost, false, false)}</div>` : ''}
           </div>
           ${card.type_line ? `<div class="card-type">${this.escapeHtml(card.type_line)}</div>` : ''}
           ${oracleHtml}
           ${(() => {
             const hasPowerToughness =
               card.power !== null &&
               card.power !== undefined &&
               card.toughness !== null &&
               card.toughness !== undefined;
             return card.set_name || hasPowerToughness
               ? `
           <div class="card-set-power-row">
               ${card.set_name ? `<div class="card-set">${this.escapeHtml(card.set_name)}</div>` : '<div class="card-set"></div>'}
               ${hasPowerToughness ? `<div class="card-power-toughness">${this.escapeHtml(card.power)} / ${this.escapeHtml(card.toughness)}</div>` : ''}
           </div>
           `
               : '';
           })()}
       </div>
   `;
  }

  handleCardClick(cardItem) {
    try {
      const cardId = cardItem.getAttribute('data-card-id');
      console.log('Clicked card with ID:', cardId);
      console.log('Available cards in cardsData:', Array.from(this.cardsData.keys()));
      console.log('Looking for card with ID:', cardId);

      const cardData = this.cardsData.get(cardId);

      if (cardData) {
        console.log('Selected card:', cardData.name);
        this.showCardModal(cardData);
      } else {
        console.error('Card data not found for ID:', cardId);
        console.error('Available card IDs:', Array.from(this.cardsData.keys()));
      }
    } catch (e) {
      console.error('Error handling card click:', e);
    }
  }

  showCardModal(card) {
    const modalOverlay = document.getElementById('modalOverlay');
    const modalContent = document.getElementById('modalContent');

    // Create modal content
    const imageLarge = this.buildImageUrl(card, '745');
    // Build image element
    let imageHtml = '';
    if (imageLarge) {
      const imgTag = `<img class="modal-image" src="${this.escapeHtml(imageLarge)}" width="745" height="1040" alt="${this.escapeHtml(card.name || 'Card Image')}" />`;
      if (card.set_code && card.collector_number) {
        // Build manapool.com referral URL
        // Set codes and collector numbers from our database are safe for URLs
        const manapoolUrl = `https://manapool.com/card/${card.set_code.toLowerCase()}/${card.collector_number}?ref=sylvan-librarian`;
        imageHtml = `<div class="modal-image-wrapper"><a href="${manapoolUrl}" target="_blank" rel="noopener" class="modal-image-link">${imgTag}</a></div>`;
      } else {
        imageHtml = `<div class="modal-image-wrapper">${imgTag}</div>`;
      }
    }

    modalContent.innerHTML = `
      <button class="modal-close" onclick="cardSearch.closeModal()">&times;</button>
      ${imageHtml}
      <div class="modal-card-info">
        <div class="modal-card-name-mana-row">
          <div class="modal-card-name">${this.escapeHtml(card.name || 'Unknown Card')}</div>
          ${card.mana_cost ? `<div class="modal-card-mana">${this.formatCardText(card.mana_cost, true, false)}</div>` : ''}
        </div>
        ${card.type_line ? `<div class="modal-card-type">${this.escapeHtml(card.type_line)}</div>` : ''}
        ${card.oracle_text ? `<div class="modal-card-text">${this.formatCardText(card.oracle_text, true, true)}</div>` : ''}
        ${(() => {
          const hasPowerToughness =
            card.power !== null && card.power !== undefined && card.toughness !== null && card.toughness !== undefined;
          return card.set_name || hasPowerToughness
            ? `
        <div class="modal-card-set-power-row">
          ${card.set_name ? `<div class="modal-card-set">${this.escapeHtml(card.set_name)}</div>` : '<div class="modal-card-set"></div>'}
          ${hasPowerToughness ? `<div class="modal-card-power-toughness">${this.escapeHtml(card.power)} / ${this.escapeHtml(card.toughness)}</div>` : ''}
        </div>
        `
            : '';
        })()}
      </div>
    `;

    // Show modal
    modalOverlay.style.display = 'flex';

    // Reset scroll position to top for both modal content and card info
    // (different elements scroll on different viewport sizes)
    modalContent.scrollTop = 0;
    const modalCardInfo = modalContent.querySelector('.modal-card-info');
    modalCardInfo.scrollTop = 0;

    // Prevent background scrolling more comprehensively
    this.preventBackgroundScroll();

    // Add click outside to close
    modalOverlay.onclick = e => {
      if (e.target === modalOverlay) {
        this.closeModal();
      }
    };

    // Add escape key to close
    document.addEventListener('keydown', this.handleEscapeKey);
  }

  closeModal() {
    const modalOverlay = document.getElementById('modalOverlay');
    modalOverlay.style.display = 'none';

    // Restore background scrolling
    this.restoreBackgroundScroll();

    document.removeEventListener('keydown', this.handleEscapeKey);
  }

  handleEscapeKey = e => {
    if (e.key === 'Escape') {
      this.closeModal();
    }
  };

  preventBackgroundScroll() {
    // Store the current scroll position
    this.scrollPosition = window.pageYOffset || document.documentElement.scrollTop;

    // Prevent scrolling on body
    document.body.style.overflow = 'hidden';
    document.body.style.position = 'fixed';
    document.body.style.top = `-${this.scrollPosition}px`;
    document.body.style.width = '100%';

    // Prevent touch events on the body (for mobile)
    document.body.addEventListener('touchmove', this.preventTouchMove, { passive: false });
  }

  restoreBackgroundScroll() {
    // Restore body styles
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.width = '';

    // Restore scroll position
    if (this.scrollPosition !== undefined) {
      window.scrollTo(0, this.scrollPosition);
    }

    // Remove touch event listener
    document.body.removeEventListener('touchmove', this.preventTouchMove);
  }

  preventTouchMove = e => {
    // Allow touch events only on the modal content
    const modalContent = document.getElementById('modalContent');
    if (modalContent && !modalContent.contains(e.target)) {
      e.preventDefault();
    }
  };

  showLoading(query) {
    console.debug('Showing loading');
    // Do not toggle card/result visibility; only update the status container
    if (this.statusMessage) {
      const inner = query ? `Searching <code class="raw-query">${this.escapeHtml(query)}</code>…` : 'Loading…';
      this.statusMessage.innerHTML = `<div class="results-count">${inner}</div>`;
    }
    this.clearResultsContainer();
  }

  showError(message) {
    console.log('Showing error:', message);
    if (this.statusMessage) {
      this.statusMessage.innerHTML = `<div class="error-message">${this.escapeHtml(message)}</div>`;
    }
    this.clearResultsContainer();
  }

  clearResultsContainer() {
    this.resultsContainer.innerHTML = '';
  }

  // Truncates an overly long query explanation (e.g. a giant OR chain) at a word boundary,
  // Scryfall-style, rather than letting the status line wrap across many lines.
  truncateExplanation(explanation) {
    if (explanation.length <= MAX_EXPLANATION_LENGTH) {
      return explanation;
    }
    const cut = explanation.slice(0, MAX_EXPLANATION_LENGTH);
    const lastSpace = cut.lastIndexOf(' ');
    return `${cut.slice(0, lastSpace > 0 ? lastSpace : MAX_EXPLANATION_LENGTH)}…`;
  }

  // Renders the single status line for a completed search: result count merged with the
  // server's human-readable explanation of the parsed query (falls back to echoing the raw
  // query text when no explanation is available, e.g. an empty/trivial query).
  showResults(count, query, explanation, elapsed) {
    console.log(`Showing results: count: ${count}, query: ${query}, explanation: ${explanation}, elapsed: ${elapsed}`);
    const formattedCount = count.toLocaleString();
    const uniqueValue = this.uniqueDropdown.value;
    const itemType = uniqueValue + (count !== 1 ? 's' : '');

    let msg;
    if (query) {
      const truncatedExplanation = explanation ? this.truncateExplanation(explanation) : explanation;
      if (truncatedExplanation) {
        msg =
          count === 0
            ? `No ${itemType} found where ${truncatedExplanation}`
            : `${formattedCount} ${itemType} where ${truncatedExplanation}`;
      } else {
        msg =
          count === 0
            ? `No ${itemType} found matching "${query}"`
            : `Found ${formattedCount} ${itemType} matching "${query}"`;
      }
      if (typeof elapsed === 'number') {
        msg += ` (completed in ${elapsed}ms)`;
      }
    } else {
      msg = `Showing a random selection of ${formattedCount} ${itemType}`;
    }
    if (this.statusMessage) {
      const cssClass = count === 0 ? 'no-results' : 'results-count';
      // Format the status message safely: formatCardText escapes HTML and replaces recognized mana tokens
      this.statusMessage.innerHTML = `<div class="${cssClass}">${this.formatCardText(msg)}</div>`;
    }
  }

  async loadRandomCards() {
    this.currentController?.abort();
    const controller = new AbortController();
    this.currentController = controller;
    this.currentRequestUrl = null;

    this.showLoading();
    try {
      const response = await fetch('/random_search?num_cards=12&shape=columnar', {
        method: 'GET',
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (!response.ok) {
        this.clearMessages();
        return;
      }
      const data = await response.json();
      if (controller.signal.aborted) return;
      this.displayResults(data, null, null);
    } catch (error) {
      if (error.name === 'AbortError') return;
      this.clearMessages();
    } finally {
      if (this.currentController === controller) {
        this.currentController = null;
        this.currentRequestUrl = null;
      }
    }
  }

  clearResults() {
    // Disconnect observer to clean up
    if (this.imageObserver) {
      this.imageObserver.disconnect();
    }
    this.resultsContainer.innerHTML = '';
    this.currentCardCount = 0; // Reset card count
    this.clearMessages();
  }

  clearSearch() {
    clearTimeout(this.debounceTimeout);
    this.currentController?.abort();
    this.lastCompletedUrl = null;

    // Clear the search input
    this.searchInput.value = '';

    // Clear results
    this.clearResults();

    // Clear URL parameters
    this.updateURL(
      '',
      this.orderDropdown.value,
      this.isAscending ? 'asc' : 'desc',
      this.uniqueDropdown.value,
      this.preferDropdown.value
    );

    // Focus back on search input
    this.searchInput.focus();
  }

  clearMessages() {
    if (this.statusMessage) {
      this.statusMessage.innerHTML = '';
    }
  }

  escapeHtml(text) {
    if (text === null || text === undefined) return '';
    // Single-pass string replace — no DOM element allocation on every call.
    // Regex and replacement callback are hoisted to module-level constants so they
    // are not re-allocated per call.  Single quotes don't need escaping: all
    // attributes use double quotes and single quotes are safe in HTML text content.
    return String(text).replace(HTML_ESCAPE_RE, htmlEscapeChar);
  }

  convertManaSymbolsToText(text) {
    if (!text) return '';

    // Use cached regex pattern and map for performance
    // Reset regex state before use (important for 'g' flag)
    this.manaTextRegex.lastIndex = 0;

    // Replace all symbols in a single pass using a callback function
    // Only replace if the symbol exists in our map, otherwise return unchanged
    return text.replace(this.manaTextRegex, match => {
      const replacement = this.manaTextMap.get(match);
      if (replacement === undefined) {
        return match;
      }
      return replacement;
    });
  }

  formatCardText(text, isModal = false, convertNewlines = false) {
    if (typeof isModal === 'object' && isModal !== null) {
      convertNewlines = isModal.convertNewlines || false;
      isModal = isModal.isModal || false;
    }
    if (text === null || text === undefined || text === '') return '';

    const symbolClass = isModal ? 'modal-mana-symbol' : 'mana-symbol';
    const escaped = this.escapeHtml(text);

    // Use cached regex pattern and map for performance
    // Reset regex state before use (important for 'g' flag)
    this.manaSymbolsRegex.lastIndex = 0;

    // Replace all symbols in a single pass using a callback function
    // Only replace if the symbol exists in our map, otherwise return unchanged
    const formatted = escaped.replace(this.manaSymbolsRegex, match => {
      const replacement = this.manaSymbolsMap.get(match);
      if (replacement === undefined) {
        return match;
      }
      return `<span class="${symbolClass} ${replacement}"></span>`;
    });

    return convertNewlines ? formatted.replace(/\n/g, '<br>') : formatted;
  }

  convertManaSymbols(manaCost, isModal = false) {
    return this.formatCardText(manaCost, isModal, false);
  }

  formatOracleText(oracleText, isModal = false) {
    return this.formatCardText(oracleText, isModal, true);
  }

  updateOrderToggleAppearance() {
    if (this.isAscending) {
      this.orderToggle.classList.remove('descending');
    } else {
      this.orderToggle.classList.add('descending');
    }
  }

  /**
   * Build search URL from (query, order, direction, unique, prefer).
   * Used for replaceState and pushState.
   */
  buildSearchUrlFromParams(query, order, direction, unique, prefer) {
    const url = new URL(window.location);
    const defaults = {
      orderby: 'edhrec',
      direction: 'asc',
      unique: 'card',
      prefer: 'default',
    };

    if (query && query.trim()) {
      url.searchParams.set('q', query.trim());
      if (order !== defaults.orderby) url.searchParams.set('orderby', order);
      else url.searchParams.delete('orderby');
      if (direction !== defaults.direction) url.searchParams.set('direction', direction);
      else url.searchParams.delete('direction');
      if (unique !== defaults.unique) url.searchParams.set('unique', unique);
      else url.searchParams.delete('unique');
      if (unique !== UNIQUE_PRINTING && prefer !== defaults.prefer) url.searchParams.set('prefer', prefer);
      else url.searchParams.delete('prefer');
    } else {
      url.searchParams.delete('q');
      url.searchParams.delete('orderby');
      url.searchParams.delete('direction');
      url.searchParams.delete('unique');
      url.searchParams.delete('prefer');
    }
    return url.href;
  }

  /** Current search URL from form state (for dwell timer and updateURL). */
  buildCurrentSearchUrl() {
    const query = this.searchInput.value.trim();
    const order = this.orderDropdown.value;
    const direction = this.isAscending ? 'asc' : 'desc';
    const unique = this.uniqueDropdown.value;
    const prefer = this.preferDropdown.value;
    return this.buildSearchUrlFromParams(query, order, direction, unique, prefer);
  }

  updateURL(query, order, direction, unique, prefer) {
    const newUrl = this.buildSearchUrlFromParams(query, order, direction, unique, prefer);
    const state = window.history.state;
    const arrivalTime = state && state.arrivalTime;
    const alreadySaved = state && state.saved === true;
    let stayTime = 0;
    if (typeof arrivalTime === 'number') {
      stayTime = Date.now() - arrivalTime;
    }
    const stayedLongEnough = stayTime > DWELL_MS;
    const isNewUrl = newUrl !== window.location.href;
    if (!alreadySaved && stayedLongEnough && isNewUrl) {
      const pushedUrl = window.location.href;
      window.history.pushState({ arrivalTime: arrivalTime, saved: true }, '', pushedUrl);
      console.log(`+Pushing ${pushedUrl} to history`);
    } else {
      console.log(
        `-Not pushing history: stayTime: ${stayTime}, newUrl: ${newUrl}, window.location.href: ${window.location.href}`
      );
    }
    window.history.replaceState({ arrivalTime: Date.now() }, '', newUrl);
  }

  updatePreferVisibility() {
    const isPrinting = this.uniqueDropdown.value === UNIQUE_PRINTING;
    this.preferDropdown.style.display = isPrinting ? 'none' : '';
    this.preferDropdown.disabled = isPrinting;
  }

  handleOrderChange() {
    const query = this.searchInput.value;
    const order = this.orderDropdown.value;
    const unique = this.uniqueDropdown.value;
    const prefer = this.preferDropdown.value;
    const direction = this.isAscending ? 'asc' : 'desc';
    this.updateURL(query, order, direction, unique, prefer);
    this.performSearch(query);
  }

  handleUniqueChange() {
    const query = this.searchInput.value;
    const order = this.orderDropdown.value;
    const unique = this.uniqueDropdown.value;
    const prefer = this.preferDropdown.value;
    const direction = this.isAscending ? 'asc' : 'desc';
    this.updateURL(query, order, direction, unique, prefer);
    this.performSearch(query);
  }

  handlePreferChange() {
    const query = this.searchInput.value;
    const order = this.orderDropdown.value;
    const unique = this.uniqueDropdown.value;
    const prefer = this.preferDropdown.value;
    const direction = this.isAscending ? 'asc' : 'desc';
    this.updateURL(query, order, direction, unique, prefer);
    this.performSearch(query);
  }

  toggleOrderDirection() {
    this.isAscending = !this.isAscending;
    this.updateOrderToggleAppearance();

    const query = this.searchInput.value;
    const order = this.orderDropdown.value;
    const unique = this.uniqueDropdown.value;
    const prefer = this.preferDropdown.value;
    const direction = this.isAscending ? 'asc' : 'desc';
    this.directionInput.value = direction;
    this.updateURL(query, order, direction, unique, prefer);
    this.performSearch(query);
  }
}

/* Theme switching functionality */
class ThemeManager {
  constructor() {
    this.themeToggle = document.getElementById('themeToggle');
    this.themeIcon = document.getElementById('themeIcon');
    this.currentTheme = localStorage.getItem('theme') || 'dark';

    this.init();
  }

  init() {
    // Apply saved theme
    this.applyTheme(this.currentTheme);

    // Add click event listener
    if (this.themeToggle) {
      this.themeToggle.addEventListener('click', e => {
        e.preventDefault();
        e.stopPropagation();
        this.toggleTheme();
      });
    }
  }

  toggleTheme() {
    this.currentTheme = this.currentTheme === 'light' ? 'dark' : 'light';
    this.applyTheme(this.currentTheme);
    this.saveTheme();
  }

  applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    this.updateIcon(theme);
  }

  updateIcon(theme) {
    if (this.themeIcon) {
      this.themeIcon.textContent = theme === 'light' ? '🌙' : '☀️';
    }
  }

  saveTheme() {
    localStorage.setItem('theme', this.currentTheme);
  }
}

// Apply initial theme immediately to prevent flash
(function () {
  let savedTheme = 'dark';
  try {
    const theme = localStorage.getItem('theme');
    if (theme) savedTheme = theme;
  } catch (e) {
    // localStorage may be unavailable; fallback to default theme
  }
  document.documentElement.setAttribute('data-theme', savedTheme);
})();

window.cardSearchMain = function () {
  window.cardSearch = new CardSearch();
  window.themeManager = new ThemeManager();
};
// Auto-initialize when this script runs via defer (DOM is fully parsed at this point).
window.cardSearchMain();
