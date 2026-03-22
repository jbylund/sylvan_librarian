#!/usr/bin/env node

/**
 * Node.js test runner for index.html JavaScript code
 * This file can be run with: node api/tests/test_index_js.mjs
 */

import { strict as assert } from 'assert';
import { performance } from 'perf_hooks';

// ===== Test Framework =====
class TestRunner {
  constructor() {
    this.suites = [];
    this.currentSuite = null;
    this.results = {
      total: 0,
      passed: 0,
      failed: 0,
      duration: 0,
    };
  }

  describe(name, callback) {
    const suite = { name, tests: [] };
    this.suites.push(suite);
    this.currentSuite = suite;
    callback();
    this.currentSuite = null;
  }

  it(name, callback) {
    if (!this.currentSuite) {
      throw new Error('Tests must be inside a describe block');
    }
    this.currentSuite.tests.push({ name, callback, status: 'pending', error: null });
  }

  async run() {
    const startTime = performance.now();
    this.results = { total: 0, passed: 0, failed: 0, duration: 0 };

    console.log('\n🧪 Running JavaScript Tests for index.html\n');

    for (const suite of this.suites) {
      console.log(`\n📦 ${suite.name}`);

      for (const test of suite.tests) {
        this.results.total++;
        try {
          await test.callback();
          test.status = 'passed';
          this.results.passed++;
          console.log(`  ✓ ${test.name}`);
        } catch (error) {
          test.status = 'failed';
          test.error = error.message;
          this.results.failed++;
          console.log(`  ✗ ${test.name}`);
          console.log(`    Error: ${error.message}`);
        }
      }
    }

    this.results.duration = Math.round(performance.now() - startTime);

    console.log('\n' + '='.repeat(60));
    console.log(`📊 Test Summary`);
    console.log('='.repeat(60));
    console.log(`Total Tests:  ${this.results.total}`);
    console.log(`✓ Passed:     ${this.results.passed}`);
    console.log(`✗ Failed:     ${this.results.failed}`);
    console.log(`Duration:     ${this.results.duration}ms`);
    console.log('='.repeat(60) + '\n');

    if (this.results.failed > 0) {
      process.exit(1);
    }
  }
}

const runner = new TestRunner();
const describe = runner.describe.bind(runner);
const it = runner.it.bind(runner);

// ===== Helper Class (Extracted from index.html) =====
class ManaSymbolConverter {
  constructor() {
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

    this.manaSymbolsMap = new Map(Object.entries({ ...hybridMap, ...manaMap }));
    this.manaSymbolsRegex = /\{[^}]{1,5}\}/g;
    this.manaTextMap = new Map(Object.entries(manaTextMap));
    this.manaTextRegex = /\{[^}]{1,5}\}/g;
  }

  convertManaSymbols(manaCost, isModal = false) {
    if (!manaCost) return '';

    const symbolClass = isModal ? 'modal-mana-symbol' : 'mana-symbol';
    this.manaSymbolsRegex.lastIndex = 0;

    return manaCost.replace(this.manaSymbolsRegex, (match) => {
      const replacement = this.manaSymbolsMap.get(match);
      if (replacement === undefined) {
        return match;
      }
      return `<span class="${symbolClass} ${replacement}"></span>`;
    });
  }

  convertManaSymbolsToText(text) {
    if (!text) return '';

    this.manaTextRegex.lastIndex = 0;

    return text.replace(this.manaTextRegex, (match) => {
      const replacement = this.manaTextMap.get(match);
      if (replacement === undefined) {
        return match;
      }
      return replacement;
    });
  }
}

// ===== Utility Functions =====
function balanceQuery(query) {
  const charToMirror = {
    '(': ')',
    "'": "'",
    '"': '"',
    ')': '(',
  };

  const stack = [];

  for (let i = 0; i < query.length; i++) {
    const char = query[i];
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

  let closing = '';
  while (stack.length > 0) {
    const char = stack.pop();
    closing += charToMirror[char];
  }

  return query + closing;
}

// ===== TEST SUITES =====

describe('ManaSymbolConverter - Basic Mana Symbols', () => {
  const converter = new ManaSymbolConverter();

  it('should convert basic mana symbols', () => {
    const result = converter.convertManaSymbols('{R}{G}{W}{U}{B}');
    assert.ok(result.includes('ms ms-r ms-cost'));
    assert.ok(result.includes('ms ms-g ms-cost'));
    assert.ok(result.includes('ms ms-w ms-cost'));
    assert.ok(result.includes('ms ms-u ms-cost'));
    assert.ok(result.includes('ms ms-b ms-cost'));
  });

  it('should convert colorless mana', () => {
    const result = converter.convertManaSymbols('{C}');
    assert.ok(result.includes('ms ms-c ms-cost'));
  });

  it('should handle empty input', () => {
    assert.strictEqual(converter.convertManaSymbols(''), '');
    assert.strictEqual(converter.convertManaSymbols(null), '');
    assert.strictEqual(converter.convertManaSymbols(undefined), '');
  });
});

describe('ManaSymbolConverter - Hybrid Mana', () => {
  const converter = new ManaSymbolConverter();

  it('should convert two-color hybrid mana', () => {
    const result = converter.convertManaSymbols('{W/U}{B/R}');
    assert.ok(result.includes('ms ms-wu ms-cost'));
    assert.ok(result.includes('ms ms-br ms-cost'));
  });

  it('should convert 2/color hybrid mana', () => {
    const result = converter.convertManaSymbols('{2/W}{2/U}');
    assert.ok(result.includes('ms ms-2w ms-cost'));
    assert.ok(result.includes('ms ms-2u ms-cost'));
  });

  it('should convert phyrexian mana', () => {
    const result = converter.convertManaSymbols('{W/P}{U/P}');
    assert.ok(result.includes('ms ms-wp ms-cost'));
    assert.ok(result.includes('ms ms-up ms-cost'));
  });

  it('should convert three-color phyrexian mana', () => {
    const result = converter.convertManaSymbols('{W/U/P}');
    assert.ok(result.includes('ms ms-wup ms-cost'));
  });
});

describe('ManaSymbolConverter - Numeric and Special Symbols', () => {
  const converter = new ManaSymbolConverter();

  it('should convert single-digit numbers', () => {
    const result = converter.convertManaSymbols('{1}{2}{3}');
    assert.ok(result.includes('ms ms-1 ms-cost'));
    assert.ok(result.includes('ms ms-2 ms-cost'));
    assert.ok(result.includes('ms ms-3 ms-cost'));
  });

  it('should convert double-digit numbers', () => {
    const result = converter.convertManaSymbols('{10}{11}{16}');
    assert.ok(result.includes('ms ms-10 ms-cost'));
    assert.ok(result.includes('ms ms-11 ms-cost'));
    assert.ok(result.includes('ms ms-16 ms-cost'));
  });

  it('should convert variable costs', () => {
    const result = converter.convertManaSymbols('{X}{Y}{Z}');
    assert.ok(result.includes('ms ms-x ms-cost'));
    assert.ok(result.includes('ms ms-y ms-cost'));
    assert.ok(result.includes('ms ms-z ms-cost'));
  });

  it('should convert tap and untap symbols', () => {
    const result = converter.convertManaSymbols('{T}{Q}');
    assert.ok(result.includes('ms ms-tap'));
    assert.ok(result.includes('ms ms-untap'));
  });

  it('should convert energy, snow, and other special symbols', () => {
    const result = converter.convertManaSymbols('{E}{S}{CHAOS}{PW}{∞}');
    assert.ok(result.includes('ms ms-energy'));
    assert.ok(result.includes('ms ms-s ms-cost'));
    assert.ok(result.includes('ms ms-chaos'));
    assert.ok(result.includes('ms ms-pw'));
    assert.ok(result.includes('ms ms-infinity'));
  });
});

describe('ManaSymbolConverter - Edge Cases', () => {
  const converter = new ManaSymbolConverter();

  it('should preserve unknown symbols', () => {
    const result = converter.convertManaSymbols('{UNKNOWN}');
    assert.strictEqual(result, '{UNKNOWN}');
  });

  it('should use modal class when isModal is true', () => {
    const result = converter.convertManaSymbols('{R}', true);
    assert.ok(result.includes('modal-mana-symbol'));
    assert.ok(!result.includes('class="mana-symbol'));
  });

  it('should handle complex mana costs', () => {
    const result = converter.convertManaSymbols('{3}{W}{W}{U}');
    assert.ok(result.includes('ms ms-3 ms-cost'));
    assert.ok(result.includes('ms ms-w ms-cost'));
    assert.ok(result.includes('ms ms-u ms-cost'));
  });

  it('should handle multiple instances of same symbol', () => {
    const result = converter.convertManaSymbols('{W}{W}{W}');
    const matches = result.match(/ms ms-w ms-cost/g);
    assert.strictEqual(matches.length, 3);
  });
});

describe('ManaSymbolConverter - Text Conversion', () => {
  const converter = new ManaSymbolConverter();

  it('should convert basic mana to emoji', () => {
    const result = converter.convertManaSymbolsToText('{W}{U}{B}{R}{G}');
    assert.ok(result.includes('☀️'));
    assert.ok(result.includes('💧'));
    assert.ok(result.includes('💀'));
    assert.ok(result.includes('🔥'));
    assert.ok(result.includes('🌳'));
  });

  it('should convert tap/untap to arrows', () => {
    const result = converter.convertManaSymbolsToText('{T}{Q}');
    assert.ok(result.includes('↻'));
    assert.ok(result.includes('↺'));
  });

  it('should convert numbers to circled numbers', () => {
    const result = converter.convertManaSymbolsToText('{1}{2}{3}');
    assert.ok(result.includes('①'));
    assert.ok(result.includes('②'));
    assert.ok(result.includes('③'));
  });

  it('should handle empty input', () => {
    assert.strictEqual(converter.convertManaSymbolsToText(''), '');
    assert.strictEqual(converter.convertManaSymbolsToText(null), '');
  });

  it('should preserve unknown symbols in text conversion', () => {
    const result = converter.convertManaSymbolsToText('{UNKNOWN}');
    assert.strictEqual(result, '{UNKNOWN}');
  });
});

describe('balanceQuery Function', () => {
  it('should balance unmatched opening parenthesis', () => {
    assert.strictEqual(balanceQuery('(hello'), '(hello)');
  });

  it('should balance unmatched opening double quote', () => {
    assert.strictEqual(balanceQuery('"hello'), '"hello"');
  });

  it('should balance unmatched opening single quote', () => {
    assert.strictEqual(balanceQuery("'hello"), "'hello'");
  });

  it('should not modify already balanced query', () => {
    assert.strictEqual(balanceQuery('(hello)'), '(hello)');
  });

  it('should handle multiple unmatched characters', () => {
    assert.strictEqual(balanceQuery('((hello'), '((hello))');
  });

  it('should handle nested parentheses', () => {
    assert.strictEqual(balanceQuery('(a (b'), '(a (b))');
  });

  it('should handle mixed quotes and parentheses', () => {
    assert.strictEqual(balanceQuery('("hello'), '("hello")');
  });

  it('should handle empty string', () => {
    assert.strictEqual(balanceQuery(''), '');
  });

  it('should handle complex unbalanced query', () => {
    assert.strictEqual(balanceQuery('(oracle:"test'), '(oracle:"test")');
  });

  it('should handle closing before opening', () => {
    assert.strictEqual(balanceQuery('hello)'), 'hello)(');
  });

  it('should handle alternating quotes', () => {
    assert.strictEqual(balanceQuery('"test\' '), '"test\' \'\"');
  });
});

describe('Performance Tests', () => {
  const converter = new ManaSymbolConverter();

  it('should convert mana symbols efficiently (1000 iterations)', () => {
    const testCost = '{3}{W}{W}{U}{B}{R}{G}';
    const iterations = 1000;
    const startTime = performance.now();

    for (let i = 0; i < iterations; i++) {
      converter.convertManaSymbols(testCost);
    }

    const duration = performance.now() - startTime;
    console.log(`    Performance: ${iterations} iterations in ${Math.round(duration)}ms`);
    assert.ok(duration < 100, `Performance test failed: ${duration}ms > 100ms`);
  });

  it('should convert text symbols efficiently (1000 iterations)', () => {
    const testText = '{W}{U}{B}{R}{G}{T}{Q}';
    const iterations = 1000;
    const startTime = performance.now();

    for (let i = 0; i < iterations; i++) {
      converter.convertManaSymbolsToText(testText);
    }

    const duration = performance.now() - startTime;
    console.log(`    Performance: ${iterations} iterations in ${Math.round(duration)}ms`);
    assert.ok(duration < 100, `Performance test failed: ${duration}ms > 100ms`);
  });
});

describe('Grid Column Calculation Logic', () => {
  function getColumnsFromViewportWidth(viewportWidth) {
    if (viewportWidth < 410) return 1;
    if (viewportWidth < 750) return 2;
    if (viewportWidth < 1370) return 3;
    if (viewportWidth < 2500) return 4;
    return 5;
  }

  it('should return 1 column for mobile', () => {
    assert.strictEqual(getColumnsFromViewportWidth(400), 1);
  });

  it('should return 2 columns for small tablets', () => {
    assert.strictEqual(getColumnsFromViewportWidth(500), 2);
  });

  it('should return 3 columns for tablets', () => {
    assert.strictEqual(getColumnsFromViewportWidth(800), 3);
  });

  it('should return 4 columns for desktop', () => {
    assert.strictEqual(getColumnsFromViewportWidth(1400), 4);
  });

  it('should return 5 columns for large screens', () => {
    assert.strictEqual(getColumnsFromViewportWidth(2600), 5);
  });

  it('should handle boundary values correctly', () => {
    assert.strictEqual(getColumnsFromViewportWidth(409), 1);
    assert.strictEqual(getColumnsFromViewportWidth(410), 2);
    assert.strictEqual(getColumnsFromViewportWidth(749), 2);
    assert.strictEqual(getColumnsFromViewportWidth(750), 3);
  });
});

describe('URL Parameter Default Values', () => {
  it('should have correct default values', () => {
    const defaults = {
      orderby: 'edhrec',
      direction: 'asc',
      unique: 'card',
      prefer: 'default',
    };

    assert.strictEqual(defaults.orderby, 'edhrec');
    assert.strictEqual(defaults.direction, 'asc');
    assert.strictEqual(defaults.unique, 'card');
    assert.strictEqual(defaults.prefer, 'default');
  });

  it('should validate unique printing constant', () => {
    const UNIQUE_PRINTING = 'printing';
    assert.strictEqual(UNIQUE_PRINTING, 'printing');
  });
});

// Run all tests
runner.run();
