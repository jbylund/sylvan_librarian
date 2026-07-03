/**
 * @jest-environment jsdom
 */
'use strict';

const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Load CardSearch class
// app.js calls window.cardSearchMain() at module load time, so we need a
// minimal DOM and a resolved commonCardTypesPromise in place before loading.
// ---------------------------------------------------------------------------

function buildDOM() {
  document.body.innerHTML = `
    <div class="header"><h1>Sylvan Librarian</h1></div>
    <form class="search-container">
      <input id="searchInput" type="text" />
    </form>
    <select id="orderDropdown"><option value="edhrec" selected>EDHREC</option></select>
    <select id="uniqueDropdown"><option value="card" selected>Card</option></select>
    <select id="preferDropdown"><option value="default" selected>Default</option></select>
    <button id="orderToggle"></button>
    <input id="directionInput" value="asc" />
    <div id="results"></div>
    <div id="statusMessage"></div>
  `;
}

buildDOM();
window.commonCardTypesPromise = Promise.resolve({ types: {}, keywords: {} });
global.fetch = jest.fn();
Object.defineProperty(global, 'performance', {
  value: { now: jest.fn(() => 100), clearResourceTimings: jest.fn(), getEntriesByType: jest.fn(() => []) },
  configurable: true,
  writable: true,
});

const appCode = fs.readFileSync(path.resolve(__dirname, 'app.js'), 'utf8');
// eslint-disable-next-line no-new-func
const { CardSearch, CatalogMap } = Function(appCode + '; return {CardSearch, CatalogMap};')();

// ---------------------------------------------------------------------------
// Live fixture: fetched from https://sylvan-librarian.com/get_common_card_types
// 381 types, alphabetically sorted by type_name, as returned by the endpoint.
// ---------------------------------------------------------------------------

const LIVE_CARD_TYPES = require('./fixtures/common_card_types.json');

// Derived fixture: new catalog format expected by the /get_catalog endpoint
const LIVE_TYPES_MAP = Object.fromEntries(LIVE_CARD_TYPES.map(({ t, n }) => [t, n]));
const LIVE_CATALOG = { types: LIVE_TYPES_MAP, keywords: {} };

// ---------------------------------------------------------------------------
// Reference implementation (the old filter+sort approach)
// ---------------------------------------------------------------------------

function filterSortMatch(types, prefix) {
  const matches = types.filter(type => type.t.toLowerCase().startsWith(prefix));
  matches.sort((a, b) => b.n - a.n);
  return matches[0] ?? null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Drain all pending microtasks and one macrotask turn. */
const flushPromises = () => new Promise(resolve => setTimeout(resolve, 0));

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

let search;

beforeEach(async () => {
  buildDOM();
  window.commonCardTypesPromise = Promise.resolve(LIVE_CATALOG);

  search = new CardSearch();
  for (const method of [
    'displayResults',
    'loadRandomCards',
    'showLoading',
    'showError',
    'showResults',
    'clearResults',
    'clearMessages',
    'updateOrderToggleAppearance',
    'updatePreferVisibility',
    'updateGridColumns',
    'updateURL',
  ]) {
    search[method] = jest.fn();
  }
  await flushPromises();
});

afterEach(() => {
  jest.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// CatalogMap constructor
// ---------------------------------------------------------------------------

describe('CatalogMap constructor', () => {
  it('size equals the number of input entries', () => {
    const catalog = new CatalogMap(LIVE_TYPES_MAP);
    expect(catalog.size).toBe(Object.keys(LIVE_TYPES_MAP).length);
  });

  it('bool is true for a non-empty input', () => {
    const catalog = new CatalogMap(LIVE_TYPES_MAP);
    expect(catalog.bool).toBe(true);
  });

  it('every entry is reachable via its own lowercased name as a prefix', () => {
    const catalog = new CatalogMap(LIVE_TYPES_MAP);
    for (const [name, n] of Object.entries(LIVE_TYPES_MAP)) {
      const match = catalog.getBestMatch(name.toLowerCase());
      expect(match).not.toBeNull();
      // The full name may still resolve to a more frequent entry sharing the
      // same prefix, so the match must be at least as frequent as the entry.
      expect(LIVE_TYPES_MAP[match]).toBeGreaterThanOrEqual(n);
    }
  });

  it('is insensitive to insertion order', () => {
    const forward = new CatalogMap(LIVE_TYPES_MAP);
    const reversed = new CatalogMap(Object.fromEntries(Object.entries(LIVE_TYPES_MAP).reverse()));
    for (const name of Object.keys(LIVE_TYPES_MAP)) {
      const lower = name.toLowerCase();
      for (const len of [1, 2, 3, lower.length]) {
        const prefix = lower.slice(0, len);
        expect(reversed.getBestMatch(prefix)).toBe(forward.getBestMatch(prefix));
      }
    }
  });

  it('returns an empty catalog for an empty input', () => {
    const catalog = new CatalogMap({});
    expect(catalog.size).toBe(0);
    expect(catalog.bool).toBe(false);
    expect(catalog.getBestMatch('a')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// CatalogMap getBestMatch
// ---------------------------------------------------------------------------

describe('CatalogMap getBestMatch', () => {
  let typeMap;

  beforeEach(() => {
    typeMap = new CatalogMap(LIVE_TYPES_MAP);
  });

  it('returns null for a prefix whose first character has no bucket', () => {
    expect(typeMap.getBestMatch('xx')).toBeNull();
  });

  it('returns null for a prefix that matches no type in the bucket', () => {
    expect(typeMap.getBestMatch('zz')).toBeNull();
  });

  it('returns the single matching type when only one matches', () => {
    const result = typeMap.getBestMatch('zu');
    expect(result).not.toBeNull();
    expect(result.toLowerCase()).toBe('zubera');
  });

  it('returns the most frequent match when multiple types share a prefix', () => {
    // "so" matches Soldier (2327), Sorcerer (127), Sorcery (10624), Sorin (38), Soltari (22)
    const result = typeMap.getBestMatch('so');
    expect(result.toLowerCase()).toBe('sorcery');
  });

  it('handles an exact full-name prefix', () => {
    const result = typeMap.getBestMatch('zombie');
    expect(result.toLowerCase()).toBe('zombie');
  });

  it('handles an empty CatalogMap without throwing', () => {
    expect(new CatalogMap({}).getBestMatch('dr')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Equivalence: getBestMatch vs filter+sort for all real prefixes
// ---------------------------------------------------------------------------

describe('getBestMatch equivalence with filter+sort', () => {
  let typeMap;

  beforeEach(() => {
    typeMap = new CatalogMap(LIVE_TYPES_MAP);
  });

  // Generate every 2+-char prefix that can be derived from the live dataset.
  const prefixes = new Set();
  for (const item of LIVE_CARD_TYPES) {
    const name = item.t.toLowerCase();
    for (let len = 2; len <= name.length; len++) {
      prefixes.add(name.slice(0, len));
    }
  }

  it.each([...prefixes])('prefix "%s": new matches old', prefix => {
    const expected = filterSortMatch(LIVE_CARD_TYPES, prefix);
    const actual = typeMap.getBestMatch(prefix);
    const normActual = actual === null ? null : actual.toLowerCase();
    const normExpected = expected === null ? null : expected.t.toLowerCase();
    expect(normActual).toEqual(normExpected);
  });

  it('returns null for no-match prefixes (sampling)', () => {
    const noMatchPrefixes = ['aa', 'zz', 'qq', 'xx', 'jj', 'bb', 'vv'];
    for (const prefix of noMatchPrefixes) {
      expect(typeMap.getBestMatch(prefix)).toBeNull();
      expect(filterSortMatch(LIVE_CARD_TYPES, prefix)).toBeNull();
    }
  });
});

// ---------------------------------------------------------------------------
// Integration: autoCompleteQuery uses the new path end-to-end
// ---------------------------------------------------------------------------

describe('autoCompleteQuery with typeMap', () => {
  it('typeMap is populated after fetchCommonCardTypes resolves', () => {
    expect(search.typeMap.size).toBeGreaterThan(0);
  });

  it('completes t:hydr to the most common hydra match', () => {
    const result = search.autoCompleteQuery('t:hydr');
    expect(result).toBe('t:hydra');
  });

  it('completes t:dr to Dragon (most frequent dr-prefix type)', () => {
    const result = search.autoCompleteQuery('t:dr');
    // Dragon (1499) is the most common type starting with "dr"
    expect(result).toBe('t:dragon');
  });

  it('preserves uppercase prefix capitalization', () => {
    const result = search.autoCompleteQuery('t:DRAG');
    expect(result).toBe('t:DRAGON');
  });

  it('preserves mixed-case prefix by appending remaining chars from match', () => {
    const result = search.autoCompleteQuery('t:Drag');
    expect(result).toBe('t:Dragon');
  });

  it('does not complete a prefix shorter than 2 chars', () => {
    expect(search.autoCompleteQuery('t:d')).toBe('t:d');
  });

  it('returns original query when prefix matches nothing', () => {
    expect(search.autoCompleteQuery('t:zz')).toBe('t:zz');
  });

  it('works inside a compound query', () => {
    const result = search.autoCompleteQuery('c:r t:drag');
    expect(result).toBe('c:r t:dragon');
  });
});
