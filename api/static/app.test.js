/**
 * @jest-environment jsdom
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { TextEncoder } = require('util');

global.TextEncoder = TextEncoder;

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
const { CardSearch, CatalogMap, columnsToRows } = Function(
  appCode + '; return {CardSearch, CatalogMap, columnsToRows};'
)();

// ---------------------------------------------------------------------------
// Live fixture: fetched from https://sylvan-librarian.com/get_common_card_types
// 381 types, alphabetically sorted by type_name, as returned by the endpoint.
// ---------------------------------------------------------------------------

const LIVE_CARD_TYPES = require('./fixtures/common_card_types.json');
const BALANCE_QUERIES = require('./fixtures/balance_queries.json');
const CARD_HTML_CASES = require('./fixtures/card_html_cases.json');
const CARD_TEXT_ESCAPING_CASES = require('./fixtures/card_text_escaping_cases.json');
const ACCEPTED_QUERIES = require('./fixtures/accepted_queries.json');

const cardCode = fs.readFileSync(path.resolve(__dirname, 'card.js'), 'utf8');
const cardModuleCode = cardCode.replace(/\(function initTheme[\s\S]*?\}\)\(\);/, '').replace(/\bmain\(\);/, '');
const cardModule = Function(
  cardModuleCode + '; return { formatCardText, convertManaSymbols, formatOracleText, renderCardFace, escapeHtml };'
)();

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

describe('columnsToRows', () => {
  test('inverts a columnar payload into an array of card objects', () => {
    const columnar = {
      name: ['Elvish Mystic', 'Counterspell'],
      power: ['1', null],
      toughness: ['1', null],
    };
    expect(columnsToRows(columnar)).toEqual([
      { name: 'Elvish Mystic', power: '1', toughness: '1' },
      { name: 'Counterspell', power: null, toughness: null },
    ]);
  });

  test('passes row-shaped payloads through untouched', () => {
    const rows = [{ name: 'Elvish Mystic' }];
    expect(columnsToRows(rows)).toBe(rows);
  });

  test('handles empty and missing payloads', () => {
    expect(columnsToRows(undefined)).toEqual([]);
    expect(columnsToRows(null)).toEqual([]);
    expect(columnsToRows([])).toEqual([]);
    expect(columnsToRows({})).toEqual([]);
  });
});

describe('CardSearch convertManaSymbols', () => {
  const manaSpan = css => `<span class="mana-symbol ${css}"></span>`;
  const modalManaSpan = css => `<span class="modal-mana-symbol ${css}"></span>`;

  it('converts basic mana symbols', () => {
    expect(search.convertManaSymbols('{W}{U}{B}')).toBe(
      manaSpan('ms ms-w ms-cost') + manaSpan('ms ms-u ms-cost') + manaSpan('ms ms-b ms-cost')
    );
  });

  it('converts colorless mana', () => {
    expect(search.convertManaSymbols('{C}')).toBe(manaSpan('ms ms-c ms-cost'));
  });

  it('converts two-color hybrid mana', () => {
    expect(search.convertManaSymbols('{W/U}')).toBe(manaSpan('ms ms-wu ms-cost'));
  });

  it('converts 2-hybrid mana', () => {
    expect(search.convertManaSymbols('{2/W}')).toBe(manaSpan('ms ms-2w ms-cost'));
  });

  it('converts phyrexian mana', () => {
    expect(search.convertManaSymbols('{W/P}')).toBe(manaSpan('ms ms-wp ms-cost'));
  });

  it('converts three-color phyrexian mana', () => {
    expect(search.convertManaSymbols('{W/U/P}')).toBe(manaSpan('ms ms-wup ms-cost'));
  });

  it('converts single-digit numerics', () => {
    expect(search.convertManaSymbols('{1}{2}{3}')).toBe(
      manaSpan('ms ms-1 ms-cost') + manaSpan('ms ms-2 ms-cost') + manaSpan('ms ms-3 ms-cost')
    );
  });

  it('converts double-digit numerics', () => {
    expect(search.convertManaSymbols('{10}{11}{16}')).toBe(
      manaSpan('ms ms-10 ms-cost') + manaSpan('ms ms-11 ms-cost') + manaSpan('ms ms-16 ms-cost')
    );
  });

  it('converts variable mana symbols', () => {
    expect(search.convertManaSymbols('{X}')).toBe(manaSpan('ms ms-x ms-cost'));
  });

  it('converts tap and untap symbols', () => {
    expect(search.convertManaSymbols('{T}{Q}')).toBe(manaSpan('ms ms-tap') + manaSpan('ms ms-untap'));
  });

  it('converts energy and snow symbols', () => {
    expect(search.convertManaSymbols('{E}{S}')).toBe(manaSpan('ms ms-energy') + manaSpan('ms ms-s ms-cost'));
  });

  it('preserves unknown symbols', () => {
    expect(search.convertManaSymbols('{UNKNOWN}')).toBe('{UNKNOWN}');
  });

  it('uses modal symbol class for modal rendering', () => {
    expect(search.convertManaSymbols('{R}', true)).toBe(modalManaSpan('ms ms-r ms-cost'));
  });

  it('handles repeated symbols', () => {
    expect(search.convertManaSymbols('{W}{W}{W}')).toBe(
      manaSpan('ms ms-w ms-cost') + manaSpan('ms ms-w ms-cost') + manaSpan('ms ms-w ms-cost')
    );
  });

  it('safely escapes hostile html tags and attributes while converting mana', () => {
    expect(search.convertManaSymbols('<script>alert(1)</script>{W}<img src=x onerror=1>')).toBe(
      `&lt;script&gt;alert(1)&lt;/script&gt;${manaSpan('ms ms-w ms-cost')}&lt;img src=x onerror=1&gt;`
    );
  });

  it('returns empty string for empty input', () => {
    expect(search.convertManaSymbols('')).toBe('');
  });
});

describe('CardSearch formatCardText and formatOracleText', () => {
  const manaSpan = css => `<span class="mana-symbol ${css}"></span>`;
  const modalManaSpan = css => `<span class="modal-mana-symbol ${css}"></span>`;

  it.each(CARD_TEXT_ESCAPING_CASES)('matches shared fixture for $id', caseItem => {
    const raw = caseItem.input;
    expect(search.formatCardText(raw, false, false)).toBe(caseItem.non_modal_no_newlines);
    expect(search.formatCardText(raw, false, true)).toBe(caseItem.non_modal_newlines);
    expect(search.formatCardText(raw, true, false)).toBe(caseItem.modal_no_newlines);
    expect(search.formatCardText(raw, true, true)).toBe(caseItem.modal_newlines);

    // Also verify options object parameter format
    expect(search.formatCardText(raw, { isModal: false, convertNewlines: false })).toBe(caseItem.non_modal_no_newlines);
    expect(search.formatCardText(raw, { isModal: true, convertNewlines: true })).toBe(caseItem.modal_newlines);
  });

  it('handles null, undefined, and empty string', () => {
    expect(search.formatCardText(null)).toBe('');
    expect(search.formatCardText(undefined)).toBe('');
    expect(search.formatCardText('')).toBe('');
    expect(search.formatOracleText(null)).toBe('');
    expect(search.formatOracleText(undefined)).toBe('');
    expect(search.formatOracleText('')).toBe('');
  });

  it('formatOracleText escapes html and converts newlines to <br>', () => {
    const raw = 'Deal 3 damage to <script>alert(1)</script>.\nPay {R} & "draw".';
    expect(search.formatOracleText(raw, false)).toBe(
      `Deal 3 damage to &lt;script&gt;alert(1)&lt;/script&gt;.<br>Pay ${manaSpan('ms ms-r ms-cost')} &amp; &quot;draw&quot;.`
    );
    expect(search.formatOracleText(raw, true)).toBe(
      `Deal 3 damage to &lt;script&gt;alert(1)&lt;/script&gt;.<br>Pay ${modalManaSpan('ms ms-r ms-cost')} &amp; &quot;draw&quot;.`
    );
  });
});

describe('card.js formatting and rendering', () => {
  const modalManaSpan = css => `<span class="modal-mana-symbol ${css}"></span>`;

  it.each(CARD_TEXT_ESCAPING_CASES)('matches shared fixture for $id in card.js', caseItem => {
    const raw = caseItem.input;
    expect(cardModule.formatCardText(raw, false, false)).toBe(caseItem.non_modal_no_newlines);
    expect(cardModule.formatCardText(raw, false, true)).toBe(caseItem.non_modal_newlines);
    expect(cardModule.formatCardText(raw, true, false)).toBe(caseItem.modal_no_newlines);
    expect(cardModule.formatCardText(raw, true, true)).toBe(caseItem.modal_newlines);
  });

  it('convertManaSymbols and formatOracleText in card.js default to modal styling', () => {
    expect(cardModule.convertManaSymbols('{R}')).toBe(modalManaSpan('ms ms-r ms-cost'));
    expect(cardModule.formatOracleText('{R}\n{G}')).toBe(
      `${modalManaSpan('ms ms-r ms-cost')}<br>${modalManaSpan('ms ms-g ms-cost')}`
    );
  });

  it('renderCardFace escapes all hostile fields', () => {
    const hostileCard = {
      name: 'Hostile <script>alert("name")</script>',
      mana_cost: '{W}<img src=x onerror=alert("cost")>',
      type_line: 'Creature <b onclick="alert(1)">Inert</b>',
      oracle_text: 'Deal 3 damage to <script>alert("oracle")</script>.\nPay {R} & "draw".',
      power: '<svg/onload=alert(1)>',
      toughness: '4" onmouseover="alert(2)',
      set_name: 'Dangerous <iframe src="evil.com">Set</iframe>',
      set_code: 'tst',
      collector_number: '1',
    };

    const rendered = cardModule.renderCardFace(hostileCard);

    expect(rendered).not.toContain('<script>');
    expect(rendered).not.toContain('<img src=x onerror=');
    expect(rendered).not.toContain('<svg');
    expect(rendered).not.toContain('<iframe');
    expect(rendered).not.toContain('onclick="alert(1)"');

    expect(rendered).toContain('&lt;script&gt;alert(&quot;name&quot;)&lt;/script&gt;');
    expect(rendered).toContain(`${modalManaSpan('ms ms-w ms-cost')}&lt;img src=x onerror=alert(&quot;cost&quot;)&gt;`);
    expect(rendered).toContain(
      `Deal 3 damage to &lt;script&gt;alert(&quot;oracle&quot;)&lt;/script&gt;.<br>Pay ${modalManaSpan('ms ms-r ms-cost')} &amp; &quot;draw&quot;.`
    );
  });
});

describe('CardSearch convertManaSymbolsToText', () => {
  it('converts mana symbols to emoji', () => {
    expect(search.convertManaSymbolsToText('{W}{U}{B}{R}{G}')).toBe('☀️💧💀🔥🌳');
  });

  it('converts tap and untap symbols to arrows', () => {
    expect(search.convertManaSymbolsToText('{T}{Q}')).toBe('↻↺');
  });

  it('converts numerics to circled numbers', () => {
    expect(search.convertManaSymbolsToText('{1}{2}{3}')).toBe('①②③');
  });

  it('passes through unknown symbols', () => {
    expect(search.convertManaSymbolsToText('{UNKNOWN}')).toBe('{UNKNOWN}');
  });

  it('returns empty string for empty input', () => {
    expect(search.convertManaSymbolsToText('')).toBe('');
  });
});

describe('CardSearch showResults', () => {
  // beforeEach() at the top of this file replaces showResults with a jest.fn() mock on
  // `search` (an existence check only), so real behavior needs its own un-mocked instance.
  const manaSpan = css => `<span class="mana-symbol ${css}"></span>`;
  const temurIcons = manaSpan('ms ms-g ms-cost') + manaSpan('ms ms-u ms-cost') + manaSpan('ms ms-r ms-cost');

  let realSearch;

  beforeEach(() => {
    buildDOM();
    realSearch = new CardSearch();
  });

  it('renders {G}{U}{R}-style server tokens as mana-font icons', () => {
    realSearch.showResults(1, 'c:temur', 'the color is Temur ({G}{U}{R})', 5);
    expect(realSearch.statusMessage.innerHTML).toBe(
      `<div class="results-count">1 card where the color is Temur (${temurIcons}) (completed in 5ms)</div>`
    );
  });

  it('escapes HTML in the raw query when there is no explanation to fall back to', () => {
    realSearch.showResults(0, '<script>alert(1)</script>', '', 2);
    expect(realSearch.statusMessage.innerHTML).toBe(
      '<div class="no-results">No cards found matching "&lt;script&gt;alert(1)&lt;/script&gt;" (completed in 2ms)</div>'
    );
  });

  it('pluralizes the item type based on count', () => {
    realSearch.showResults(0, 'c:temur', 'the color is Temur ({G}{U}{R})', 3);
    expect(realSearch.statusMessage.innerHTML).toBe(
      `<div class="no-results">No cards found where the color is Temur (${temurIcons}) (completed in 3ms)</div>`
    );
  });

  it('shows a random-selection message when there is no query', () => {
    realSearch.showResults(12, null, null, null);
    expect(realSearch.statusMessage.innerHTML).toBe(
      '<div class="results-count">Showing a random selection of 12 cards</div>'
    );
  });

  it('omits the elapsed-time suffix when elapsed is not a number', () => {
    realSearch.showResults(1, 'c:temur', 'the color is Temur ({G}{U}{R})', null);
    expect(realSearch.statusMessage.innerHTML).toBe(
      `<div class="results-count">1 card where the color is Temur (${temurIcons})</div>`
    );
  });
});

describe('CardSearch balanceQuery', () => {
  it.each(BALANCE_QUERIES)('matches parity fixture for $input', ({ input, suffix }) => {
    expect(search.balanceSuffix(input)).toBe(suffix);
    expect(search.balanceQuery(input)).toBe(suffix === null ? input : input + suffix);
  });
});

describe('CardSearch blankOpaqueSpans', () => {
  it('blanks the body of a closed mana symbol the same way it blanks quotes and regexes', () => {
    // Before this, only '"'/"'"/'/' were handled here, unlike scanSpans — a "fourth opinion" gap
    // matching the exact bug class this blanking exists to prevent, just left open for braces.
    expect(search.blankOpaqueSpans('mana:{2/W} and')).toBe('mana:{} and');
  });

  it('still blanks quotes and regexes', () => {
    expect(search.blankOpaqueSpans('oracle:"and or"')).toBe('oracle:""');
    expect(search.blankOpaqueSpans('o:/and:/')).toBe('o://');
  });
});

describe('CardSearch collapseWhitespaceOutsideSpans', () => {
  it('preserves internal whitespace inside a regex or quoted string', () => {
    expect(search.collapseWhitespaceOutsideSpans('o:/a  b/')).toBe('o:/a  b/');
    expect(search.collapseWhitespaceOutsideSpans('oracle:"draw  a  card"')).toBe('oracle:"draw  a  card"');
  });

  it('collapses runs of whitespace outside spans to a single space', () => {
    expect(search.collapseWhitespaceOutsideSpans('t:elf   o:bolt')).toBe('t:elf o:bolt');
  });
});

describe('CardSearch _balanceAndNormalize', () => {
  // A plain `.replace(/\s+/g, ' ')` over the whole query would silently rewrite what a regex or
  // quoted value actually matches before it ever reaches the server.
  it('does not collapse whitespace inside a regex or quoted string', () => {
    expect(search._balanceAndNormalize('o:/a  b/')).toBe('o:/a  b/');
    expect(search._balanceAndNormalize('oracle:"draw  a  card"')).toBe('oracle:"draw  a  card"');
  });

  it('still trims and collapses whitespace outside spans', () => {
    expect(search._balanceAndNormalize('  t:elf   o:bolt  ')).toBe('t:elf o:bolt');
  });
});

describe('CardSearch createCardHTML no-JS parity', () => {
  // Keep in sync with normalize_card_html in api/tests/test_noscript_parity.py.
  // Strips the loading-hint attributes (fetchpriority/loading logic intentionally
  // differs between the JS and no-JS paths) and inter-tag whitespace (template
  // indentation differs).
  function normalizeCardHtml(html) {
    return html
      .replaceAll(' fetchpriority="high"', '')
      .replaceAll(' loading="lazy"', '')
      .replace(/>\s+</g, '><')
      .trim();
  }

  it.each(CARD_HTML_CASES)('matches parity fixture for $id', ({ card, index, html }) => {
    expect(normalizeCardHtml(search.createCardHTML(card, index, false))).toBe(html);
  });
});

describe('CardSearch validateQuery', () => {
  it('rejects queries over the UTF-8 byte limit without echoing them', () => {
    const overLimit = 'a'.repeat(3501);
    expect(search.validateQuery(overLimit)).toBe('Search query exceeds the maximum allowed length.');
    expect(search.validateQuery(overLimit)).not.toContain(overLimit);
  });

  it('rejects queries with an unmatched closing parenthesis', () => {
    expect(search.validateQuery('hello)(')).toBe('Failed to parse query: "hello)("');
    expect(search.validateQuery('hello)')).toBe('Failed to parse query: "hello)"');
  });

  it('accepts balanced queries', () => {
    expect(search.validateQuery('(name:test)')).toBeNull();
    expect(search.validateQuery('oracle:"draw a card"')).toBeNull();
  });

  it('still reports a field with no value', () => {
    expect(search.validateQuery('t:')).toBe('Failed to parse query: "t:"');
    expect(search.validateQuery('(t:)')).toBe('Failed to parse query: "(t:)"');
    expect(search.validateQuery('name:test and')).toBe('Failed to parse query: "name:test and"');
  });

  it('alreadyBalanced skips the balance re-check, but not the other structural checks', () => {
    // A caller vouching for balance it hasn't actually verified is a caller bug, not something
    // validateQuery should paper over — the option exists for callers that just ran scanSpans.
    expect(search.validateQuery('hello)', { alreadyBalanced: true })).toBeNull();
    expect(search.validateQuery('t:', { alreadyBalanced: true })).toBe('Failed to parse query: "t:"');
  });
});

// The backend runs this same fixture through both parsers in test_balance_parity.py, so the client
// cannot start rejecting a query the API would have answered — the failure mode in #908, where a
// ':' inside a regex read as a field with no value and the request was never sent.
describe('CardSearch accepts every shared accepted query', () => {
  it.each(ACCEPTED_QUERIES)('%s', query => {
    expect(search.validateQuery(query)).toBeNull();
    expect(search.balanceQuery(query)).toBe(query);
  });
});

describe('CardSearch performSearch', () => {
  it('shows an error and skips the http request for over-limit queries', async () => {
    global.fetch.mockClear();

    await search.performSearch('a'.repeat(3501));

    expect(search.showError).toHaveBeenCalledWith(
      expect.stringContaining('Search query exceeds the maximum allowed length.')
    );
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('shows an error and skips the http request for unbalance-able queries', async () => {
    global.fetch.mockClear();

    await search.performSearch('hello)');

    expect(search.showError).toHaveBeenCalledWith(expect.stringContaining('Invalid Search Query'));
    expect(global.fetch).not.toHaveBeenCalled();
  });

  // performSearch used to call scanSpans up to three times per keystroke: once for the empty-span
  // guard, again for balancing, and a third time inside validateQuery's own balanceSuffix check —
  // even though performSearch already knows the string is balanceable by the time it calls
  // validateQuery. All three now share the one scan performSearch runs up front.
  it('scans spans only once for the empty-span guard, balancing, and validation', async () => {
    const spy = jest.spyOn(search, 'scanSpans');

    await search.performSearch('name:bolt');

    expect(spy).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });

  // A span with nothing in it is not a query yet. Balancing would close it into something that is
  // valid but useless — `o://` matches every card with an unindexable empty pattern — so the request
  // waits for the next keystroke, and unlike an unbalance-able query this is not an error.
  it.each(['o:/', "o:'", 'o:"', 'mana:{', '(o:/', "t:elf o:'", 'o:/ ', 'mana:{  '])(
    'waits instead of searching on %s',
    async query => {
      global.fetch.mockClear();
      search.showError.mockClear();

      await search.performSearch(query);

      expect(global.fetch).not.toHaveBeenCalled();
      expect(search.showError).not.toHaveBeenCalled();
    }
  );

  // Backspacing a searched query down into an empty span used to leave the previous "Searching …"
  // on screen forever, because handleSearch had already aborted the fetch and the guard returned
  // before any status update.
  it('does not leave a stale loading message when the query decays into an empty span', async () => {
    global.fetch.mockClear();
    search.clearMessages.mockClear();

    await search.performSearch('o:/');

    expect(search.clearMessages).toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it.each(['o:/a', "o:'a", 'mana:{W', 'o:/^{T}:/'])('still searches once the span has content: %s', async query => {
    global.fetch.mockClear();

    await search.performSearch(query);

    expect(global.fetch).toHaveBeenCalled();
  });
});

describe('CardSearch getColumnsFromViewportWidth', () => {
  it.each([
    [400, 1],
    [500, 2],
    [800, 3],
    [1400, 4],
    [2600, 5],
    [409, 1],
    [410, 2],
    [749, 2],
    [750, 3],
    [1369, 3],
    [1370, 4],
    [2499, 4],
    [2500, 5],
  ])('at width %p returns %p columns', (width, expectedColumns) => {
    window.innerWidth = width;
    expect(search.getColumnsFromViewportWidth()).toBe(expectedColumns);
  });
});

describe('CardSearch double-faced flip', () => {
  const dfc = { name: 'Front // Back', set_code: 'mid', collector_number: '5' };

  test('buildImageUrl face parameter overrides the card face', () => {
    expect(search.buildImageUrl(dfc, '388')).toContain('/mid/5/1/388.webp');
    expect(search.buildImageUrl(dfc, '388', 2)).toContain('/mid/5/2/388.webp');
  });

  test('buildSrcset builds all four sizes for the requested face', () => {
    const srcset = search.buildSrcset(dfc, 2);
    for (const size of ['280', '388', '538', '745']) {
      expect(srcset).toContain(`/mid/5/2/${size}.webp ${size}w`);
    }
  });

  test('probeBackFace skips cards without a // name', () => {
    const onExists = jest.fn();
    search.probeBackFace({ name: 'Lightning Bolt', set_code: 'm15', collector_number: '1' }, onExists);
    expect(onExists).not.toHaveBeenCalled();
    expect(search.backFaceKeys.size).toBe(0);
  });

  test('probeBackFace uses the cached probe result', () => {
    const onExists = jest.fn();
    search.backFaceKeys.set('mid/5', true);
    search.probeBackFace(dfc, onExists);
    expect(onExists).toHaveBeenCalledTimes(1);

    search.backFaceKeys.set('mid/5', false);
    search.probeBackFace(dfc, onExists);
    expect(onExists).toHaveBeenCalledTimes(1);
  });

  test('flip button toggles the image between faces', () => {
    jest.useFakeTimers();
    try {
      search.resultsContainer.innerHTML =
        '<div class="card-item" data-card-id="0"><img class="card-image" src="x" srcset="y" /></div>';
      search.attachGridFlipButton('0', dfc);

      const item = search.resultsContainer.querySelector('.card-item');
      const button = item.querySelector('.card-flip-button');
      expect(button).not.toBeNull();

      button.click();
      jest.runAllTimers();
      const img = item.querySelector('.card-image');
      expect(img.src).toContain('/mid/5/2/388.webp');
      expect(img.srcset).toContain('/mid/5/2/745.webp 745w');

      button.click();
      jest.runAllTimers();
      expect(img.src).toContain('/mid/5/1/388.webp');
    } finally {
      jest.useRealTimers();
    }
  });

  test('attachGridFlipButton injects at most one button', () => {
    search.resultsContainer.innerHTML =
      '<div class="card-item" data-card-id="0"><img class="card-image" src="x" /></div>';
    search.attachGridFlipButton('0', dfc);
    search.attachGridFlipButton('0', dfc);
    expect(search.resultsContainer.querySelectorAll('.card-flip-button')).toHaveLength(1);
  });
});
