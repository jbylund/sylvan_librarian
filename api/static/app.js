const UNIQUE_PRINTING = 'printing';
const DWELL_MS = 2500; // milliseconds user must stay on results before adding a history entry
const MAX_EXPLANATION_LENGTH = 140; // truncate very long query explanations (e.g. giant OR chains)

// Hoisted so escapeHtml() doesn't allocate a new RegExp or callback on every call.
const HTML_ESCAPE_RE = /[&<>"]/g;
const HTML_ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
const htmlEscapeChar = c => HTML_ESCAPE_MAP[c];

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

    // Add resize listener to update columns dynamically
    window.addEventListener('resize', () => {
      this.updateGridColumns(this.currentCardCount);
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
    const autocompleted = this.autoCompleteQuery(query);
    const balanced = this.balanceQuery(autocompleted);
    const normalized = balanced.trim().replace(/\s+/g, ' ');
    return normalized;
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

  balanceQuery(query) {
    // Balance quotes and parentheses for typeahead searches using a stack
    const charToMirror = {
      '(': ')',
      "'": "'", // single quote is own mirror
      '"': '"', // double quote is own mirror
      ')': '(',
    };
    const quoteChars = new Set(["'", '"']);

    const stack = [];

    // Process each character in the query
    for (let i = 0; i < query.length; i++) {
      const char = query[i];

      // When inside a quoted string, only the matching closing quote ends it.
      // All other characters (including other quote types and parentheses) are ignored.
      if (stack.length > 0 && quoteChars.has(stack[stack.length - 1])) {
        if (char === stack[stack.length - 1]) {
          stack.pop();
        }
        continue;
      }

      const mirroredChar = charToMirror[char];

      if (!mirroredChar) {
        continue;
      }

      if (stack.length > 0 && stack[stack.length - 1] === mirroredChar) {
        stack.pop();
      } else {
        stack.push(char);
      }
    }

    // Build the closing characters from the stack in reverse order
    let closing = '';
    while (stack.length > 0) {
      const char = stack.pop();
      closing += charToMirror[char];
    }

    return query + closing;
  }

  // Returns an error string if the query is structurally invalid, or null if it looks ok.
  validateQuery(query) {
    // Strip quoted strings so we don't match content inside them.
    const q = query.replace(/"[^"]*"|'[^']*'/g, '""');

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

    const normalizedQuery = this._processQuery(query);

    const validationError = this.validateQuery(normalizedQuery);
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
    const url = `/search?q=${encodeURIComponent(normalizedQuery)}&orderby=${order}&direction=${orderDirection}&unique=${unique}&prefer=${prefer}`;

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
    const cards = data.cards || [];
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
      // Convert mana symbols in oracle text to Unicode for alt text first, then truncate
      const oracleTextWithSymbols = this.convertManaSymbolsToText(card.oracle_text);
      const maxLength = 300;
      const truncatedText =
        oracleTextWithSymbols.length > maxLength
          ? oracleTextWithSymbols.substring(0, maxLength) + '...'
          : oracleTextWithSymbols;
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
        ? `<a href="/card/${card.set_code}/${card.collector_number}" class="card-page-link">${imgTag}</a>`
        : imgTag;

    return `
       <div class="card-item" data-card-id="${this.escapeHtml(cardId)}">
           ${imageHtml}
           <div class="card-name-mana-row">
               <div class="card-name">${this.escapeHtml(card.name || 'Unknown Card')}</div>
               ${card.mana_cost ? `<div class="card-mana">${this.convertManaSymbols(card.mana_cost, false)}</div>` : ''}
           </div>
           ${card.type_line ? `<div class="card-type">${this.escapeHtml(card.type_line)}</div>` : ''}
           ${card.oracle_text ? `<div class="card-text">${this.formatOracleText(card.oracle_text.substring(0, 200), false)}${card.oracle_text.length > 200 ? '...' : ''}</div>` : ''}
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
          ${card.mana_cost ? `<div class="modal-card-mana">${this.convertManaSymbols(card.mana_cost, true)}</div>` : ''}
        </div>
        ${card.type_line ? `<div class="modal-card-type">${this.escapeHtml(card.type_line)}</div>` : ''}
        ${card.oracle_text ? `<div class="modal-card-text">${this.formatOracleText(card.oracle_text, true)}</div>` : ''}
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
      const inner = query
        ? `Searching <code class="raw-query">${this.escapeHtml(query)}</code>…`
        : 'Loading…';
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
        msg = count === 0 ? `No ${itemType} found where ${truncatedExplanation}` : `${formattedCount} ${itemType} where ${truncatedExplanation}`;
      } else {
        msg = count === 0 ? `No ${itemType} found matching "${query}"` : `Found ${formattedCount} ${itemType} matching "${query}"`;
      }
      if (typeof elapsed === 'number') {
        msg += ` (completed in ${elapsed}ms)`;
      }
    } else {
      msg = `Showing a random selection of ${formattedCount} ${itemType}`;
    }
    if (this.statusMessage) {
      const cssClass = count === 0 ? 'no-results' : 'results-count';
      this.statusMessage.innerHTML = `<div class="${cssClass}">${this.escapeHtml(msg)}</div>`;
    }
  }

  async loadRandomCards() {
    this.currentController?.abort();
    const controller = new AbortController();
    this.currentController = controller;
    this.currentRequestUrl = null;

    this.showLoading();
    try {
      const response = await fetch('/random_search?num_cards=12', {
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

  convertManaSymbols(manaCost, isModal = false) {
    if (!manaCost) return '';

    const symbolClass = isModal ? 'modal-mana-symbol' : 'mana-symbol';

    // Use cached regex pattern and map for performance
    // Reset regex state before use (important for 'g' flag)
    this.manaSymbolsRegex.lastIndex = 0;

    // Replace all symbols in a single pass using a callback function
    // Only replace if the symbol exists in our map, otherwise return unchanged
    return manaCost.replace(this.manaSymbolsRegex, match => {
      const replacement = this.manaSymbolsMap.get(match);
      if (replacement === undefined) {
        return match;
      }
      return `<span class="${symbolClass} ${replacement}"></span>`;
    });
  }

  formatOracleText(oracleText, isModal = false) {
    if (!oracleText) return '';

    // First convert mana symbols
    let formatted = this.convertManaSymbols(oracleText, isModal);

    // Then convert newlines to HTML line breaks
    formatted = formatted.replace(/\n/g, '<br>');

    return formatted;
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
