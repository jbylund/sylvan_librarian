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

const LIVE_CARD_TYPES = [
  { t: 'Adventure', n: 310 },
  { t: 'Advisor', n: 406 },
  { t: 'Aetherborn', n: 62 },
  { t: 'Ajani', n: 59 },
  { t: 'Alien', n: 99 },
  { t: 'Ally', n: 398 },
  { t: 'Aminatou', n: 7 },
  { t: 'Angel', n: 1050 },
  { t: 'Angrath', n: 11 },
  { t: 'Antelope', n: 23 },
  { t: 'Ape', n: 110 },
  { t: 'Arcane', n: 184 },
  { t: 'Archer', n: 275 },
  { t: 'Archon', n: 76 },
  { t: 'Arlinn', n: 19 },
  { t: 'Artifact', n: 10947 },
  { t: 'Artificer', n: 752 },
  { t: 'Ashiok', n: 16 },
  { t: 'Assassin', n: 392 },
  { t: 'Assembly-Worker', n: 36 },
  { t: 'Astartes', n: 46 },
  { t: 'Atog', n: 24 },
  { t: 'Aura', n: 3246 },
  { t: 'Aurochs', n: 6 },
  { t: 'Avatar', n: 571 },
  { t: 'Azra', n: 16 },
  { t: 'Background', n: 70 },
  { t: 'Badger', n: 20 },
  { t: 'Bahamut', n: 6 },
  { t: 'Barbarian', n: 125 },
  { t: 'Bard', n: 127 },
  { t: 'Basic', n: 4196 },
  { t: 'Basilisk', n: 40 },
  { t: 'Basri', n: 7 },
  { t: 'Bat', n: 98 },
  { t: 'Bear', n: 147 },
  { t: 'Beast', n: 1207 },
  { t: 'Beholder', n: 16 },
  { t: 'Berserker', n: 350 },
  { t: 'Bird', n: 1042 },
  { t: 'Bison', n: 8 },
  { t: 'Boar', n: 135 },
  { t: 'Bobblehead', n: 21 },
  { t: 'Bolas', n: 30 },
  { t: 'Book', n: 169 },
  { t: 'Bringer', n: 6 },
  { t: 'Brushwagg', n: 7 },
  { t: 'Calix', n: 5 },
  { t: 'Camel', n: 9 },
  { t: 'Carrier', n: 22 },
  { t: 'Cartouche', n: 13 },
  { t: 'Case', n: 24 },
  { t: 'Cat', n: 919 },
  { t: 'Cave', n: 44 },
  { t: 'Centaur', n: 148 },
  { t: 'Chandra', n: 81 },
  { t: 'Chimera', n: 39 },
  { t: 'Citizen', n: 200 },
  { t: 'Class', n: 69 },
  { t: 'Cleric', n: 1572 },
  { t: 'Clown', n: 6 },
  { t: 'Clue', n: 42 },
  { t: 'Cockatrice', n: 16 },
  { t: 'Construct', n: 632 },
  { t: 'Crab', n: 94 },
  { t: 'Creature', n: 45967 },
  { t: 'Crocodile', n: 60 },
  { t: 'Curse', n: 102 },
  { t: 'Cyberman', n: 16 },
  { t: 'Cyclops', n: 69 },
  { t: 'Dack', n: 6 },
  { t: 'Dalek', n: 16 },
  { t: 'Daretti', n: 13 },
  { t: 'Dauthi', n: 25 },
  { t: 'Davriel', n: 6 },
  { t: 'Demigod', n: 37 },
  { t: 'Demon', n: 684 },
  { t: 'Desert', n: 103 },
  { t: 'Detective', n: 181 },
  { t: 'Devil', n: 148 },
  { t: 'Dihada', n: 6 },
  { t: 'Dinosaur', n: 623 },
  { t: 'Djinn', n: 179 },
  { t: 'Doctor', n: 120 },
  { t: 'Dog', n: 315 },
  { t: 'Domri', n: 15 },
  { t: 'Dovin', n: 10 },
  { t: 'Dragon', n: 1499 },
  { t: 'Drake', n: 249 },
  { t: 'Dreadnought', n: 7 },
  { t: 'Drix', n: 5 },
  { t: 'Drone', n: 111 },
  { t: 'Druid', n: 1140 },
  { t: 'Dryad', n: 158 },
  { t: 'Dwarf', n: 309 },
  { t: 'Efreet', n: 74 },
  { t: 'Egg', n: 28 },
  { t: 'Elder', n: 277 },
  { t: 'Eldrazi', n: 473 },
  { t: 'Elemental', n: 1772 },
  { t: 'Elephant', n: 197 },
  { t: 'Elf', n: 2096 },
  { t: 'Elk', n: 102 },
  { t: 'Ellywick', n: 5 },
  { t: 'Elspeth', n: 42 },
  { t: 'Enchantment', n: 9913 },
  { t: 'Equipment', n: 1644 },
  { t: 'Eternal', n: 5 },
  { t: 'Eye', n: 23 },
  { t: 'Faerie', n: 420 },
  { t: 'Fish', n: 124 },
  { t: 'Flagbearer', n: 5 },
  { t: 'Food', n: 33 },
  { t: 'Forest', n: 1180 },
  { t: 'Fox', n: 105 },
  { t: 'Fractal', n: 11 },
  { t: 'Freyalise', n: 8 },
  { t: 'Frog', n: 181 },
  { t: 'Fungus', n: 188 },
  { t: 'Gamma', n: 32 },
  { t: 'Gargoyle', n: 83 },
  { t: 'Garruk', n: 37 },
  { t: 'Gate', n: 180 },
  { t: 'Giant', n: 629 },
  { t: 'Gideon', n: 34 },
  { t: 'Gith', n: 5 },
  { t: 'Glimmer', n: 35 },
  { t: 'Gnome', n: 65 },
  { t: 'Goat', n: 27 },
  { t: 'Goblin', n: 1506 },
  { t: 'God', n: 277 },
  { t: 'Golem', n: 434 },
  { t: 'Gorgon', n: 51 },
  { t: 'Gremlin', n: 26 },
  { t: 'Griffin', n: 101 },
  { t: 'Grist', n: 12 },
  { t: 'Hag', n: 16 },
  { t: 'Halfling', n: 162 },
  { t: 'Harpy', n: 20 },
  { t: 'Hellion', n: 45 },
  { t: 'Hero', n: 491 },
  { t: 'Hippo', n: 21 },
  { t: 'Hippogriff', n: 17 },
  { t: 'Homarid', n: 16 },
  { t: 'Homunculus', n: 72 },
  { t: 'Horror', n: 934 },
  { t: 'Horse', n: 142 },
  { t: 'Huatli', n: 11 },
  { t: 'Human', n: 10567 },
  { t: 'Hydra', n: 234 },
  { t: 'Hyena', n: 7 },
  { t: 'Illusion', n: 258 },
  { t: 'Imp', n: 131 },
  { t: 'Incarnation', n: 150 },
  { t: 'Infinity', n: 7 },
  { t: 'Inhuman', n: 12 },
  { t: 'Inquisitor', n: 6 },
  { t: 'Insect', n: 608 },
  { t: 'Instant', n: 10724 },
  { t: 'Island', n: 1127 },
  { t: 'Jace', n: 76 },
  { t: 'Jackal', n: 55 },
  { t: 'Jaya', n: 15 },
  { t: 'Jellyfish', n: 74 },
  { t: 'Juggernaut', n: 68 },
  { t: 'Kaito', n: 24 },
  { t: 'Karn', n: 29 },
  { t: 'Kasmina', n: 11 },
  { t: 'Kavu', n: 111 },
  { t: 'Kaya', n: 34 },
  { t: 'Kindred', n: 183 },
  { t: 'Kiora', n: 11 },
  { t: 'Kirin', n: 23 },
  { t: 'Kithkin', n: 156 },
  { t: 'Knight', n: 1329 },
  { t: 'Kobold', n: 26 },
  { t: 'Kor', n: 207 },
  { t: 'Koth', n: 9 },
  { t: 'Kraken', n: 118 },
  { t: 'Kree', n: 14 },
  { t: 'Lair', n: 12 },
  { t: 'Lamia', n: 7 },
  { t: 'Land', n: 11551 },
  { t: 'Leech', n: 32 },
  { t: 'Legendary', n: 13532 },
  { t: 'Lemur', n: 7 },
  { t: 'Lesson', n: 120 },
  { t: 'Leviathan', n: 92 },
  { t: 'Lhurgoyf', n: 67 },
  { t: 'Licid', n: 14 },
  { t: 'Liliana', n: 84 },
  { t: 'Lizard', n: 339 },
  { t: 'Locus', n: 8 },
  { t: 'Lolth', n: 6 },
  { t: 'Lord', n: 168 },
  { t: 'Lukka', n: 17 },
  { t: 'Manticore', n: 22 },
  { t: 'Masticore', n: 22 },
  { t: 'Mercenary', n: 195 },
  { t: 'Merfolk', n: 796 },
  { t: 'Metathran', n: 14 },
  { t: 'Mine', n: 26 },
  { t: 'Minion', n: 112 },
  { t: 'Minotaur', n: 210 },
  { t: 'Minsc', n: 7 },
  { t: 'Mite', n: 7 },
  { t: 'Mole', n: 21 },
  { t: 'Monger', n: 7 },
  { t: 'Mongoose', n: 8 },
  { t: 'Monk', n: 364 },
  { t: 'Monkey', n: 40 },
  { t: 'Moogle', n: 16 },
  { t: 'Moonfolk', n: 54 },
  { t: 'Mordenkainen', n: 5 },
  { t: 'Mount', n: 68 },
  { t: 'Mountain', n: 1139 },
  { t: 'Mouse', n: 45 },
  { t: 'Mutant', n: 464 },
  { t: 'Myr', n: 127 },
  { t: 'Mystic', n: 11 },
  { t: 'Nahiri', n: 27 },
  { t: 'Narset', n: 18 },
  { t: 'Necron', n: 48 },
  { t: 'Nephilim', n: 10 },
  { t: 'Nightmare', n: 239 },
  { t: 'Nightstalker', n: 22 },
  { t: 'Ninja', n: 317 },
  { t: 'Nissa', n: 50 },
  { t: 'Nixilis', n: 25 },
  { t: 'Noble', n: 584 },
  { t: 'Noggle', n: 9 },
  { t: 'Nomad', n: 70 },
  { t: 'Nymph', n: 58 },
  { t: 'Octopus', n: 100 },
  { t: 'Ogre', n: 284 },
  { t: 'Oko', n: 15 },
  { t: 'Omen', n: 32 },
  { t: 'Ooze', n: 188 },
  { t: 'Orc', n: 269 },
  { t: 'Orgg', n: 10 },
  { t: 'Otter', n: 51 },
  { t: 'Ouphe', n: 35 },
  { t: 'Ox', n: 55 },
  { t: 'Pangolin', n: 7 },
  { t: 'Peasant', n: 88 },
  { t: 'Pegasus', n: 73 },
  { t: 'Performer', n: 17 },
  { t: 'Pest', n: 11 },
  { t: 'Phelddagrif', n: 6 },
  { t: 'Phoenix', n: 105 },
  { t: 'Phyrexian', n: 1234 },
  { t: 'Pilot', n: 91 },
  { t: 'Pirate', n: 456 },
  { t: 'Plains', n: 1109 },
  { t: 'Plan', n: 12 },
  { t: 'Planeswalker', n: 1379 },
  { t: 'Planet', n: 25 },
  { t: 'Plant', n: 238 },
  { t: 'Possum', n: 6 },
  { t: 'Power-Plant', n: 26 },
  { t: 'Praetor', n: 101 },
  { t: 'Processor', n: 16 },
  { t: 'Quintorius', n: 6 },
  { t: 'Rabbit', n: 78 },
  { t: 'Raccoon', n: 39 },
  { t: 'Ral', n: 28 },
  { t: 'Ranger', n: 176 },
  { t: 'Rat', n: 337 },
  { t: 'Rebel', n: 161 },
  { t: 'Rhino', n: 135 },
  { t: 'Robot', n: 198 },
  { t: 'Rogue', n: 1367 },
  { t: 'Room', n: 59 },
  { t: 'Rowan', n: 12 },
  { t: 'Rune', n: 5 },
  { t: 'Saga', n: 439 },
  { t: 'Saheeli', n: 18 },
  { t: 'Salamander', n: 37 },
  { t: 'Samurai', n: 153 },
  { t: 'Samut', n: 7 },
  { t: 'Sarkhan', n: 23 },
  { t: 'Satyr', n: 70 },
  { t: 'Scarecrow', n: 81 },
  { t: 'Scientist', n: 123 },
  { t: 'Scorpion', n: 39 },
  { t: 'Scout', n: 675 },
  { t: 'Seal', n: 5 },
  { t: 'Serpent', n: 146 },
  { t: 'Shade', n: 84 },
  { t: 'Shaman', n: 1420 },
  { t: 'Shapeshifter', n: 486 },
  { t: 'Shark', n: 45 },
  { t: 'Sheep', n: 15 },
  { t: 'Shrine', n: 35 },
  { t: 'Siren', n: 46 },
  { t: 'Skeleton', n: 245 },
  { t: 'Skrull', n: 5 },
  { t: 'Slith', n: 17 },
  { t: 'Sliver', n: 328 },
  { t: 'Sloth', n: 13 },
  { t: 'Slug', n: 22 },
  { t: 'Snake', n: 481 },
  { t: 'Snow', n: 262 },
  { t: 'Soldier', n: 2327 },
  { t: 'Soltari', n: 22 },
  { t: 'Sorcerer', n: 127 },
  { t: 'Sorcery', n: 10624 },
  { t: 'Sorin', n: 38 },
  { t: 'Spacecraft', n: 58 },
  { t: 'Specter', n: 92 },
  { t: 'Spellshaper', n: 94 },
  { t: 'Sphere', n: 23 },
  { t: 'Sphinx', n: 258 },
  { t: 'Spider', n: 376 },
  { t: 'Spike', n: 24 },
  { t: 'Spirit', n: 1694 },
  { t: 'Spy', n: 30 },
  { t: 'Squid', n: 20 },
  { t: 'Squirrel', n: 75 },
  { t: 'Starfish', n: 11 },
  { t: 'Stone', n: 7 },
  { t: 'Surrakar', n: 8 },
  { t: 'Survivor', n: 29 },
  { t: 'Swamp', n: 1129 },
  { t: 'Symbiote', n: 30 },
  { t: 'Synth', n: 14 },
  { t: 'Tamiyo', n: 27 },
  { t: 'Teferi', n: 46 },
  { t: 'Teyo', n: 7 },
  { t: 'Tezzeret', n: 30 },
  { t: 'Thalakos', n: 7 },
  { t: 'Thopter', n: 91 },
  { t: 'Thrull', n: 60 },
  { t: 'Tibalt', n: 14 },
  { t: 'Tiefling', n: 35 },
  { t: 'Time', n: 168 },
  { t: 'Tower', n: 26 },
  { t: 'Town', n: 20 },
  { t: 'Toy', n: 24 },
  { t: 'Trap', n: 43 },
  { t: 'Treasure', n: 12 },
  { t: 'Treefolk', n: 279 },
  { t: 'Trilobite', n: 7 },
  { t: 'Troll', n: 147 },
  { t: 'Turtle', n: 213 },
  { t: 'Tyranid', n: 76 },
  { t: 'Tyvar', n: 11 },
  { t: 'Ugin', n: 24 },
  { t: 'Unicorn', n: 97 },
  { t: "Urza'S", n: 95 },
  { t: 'Utrom', n: 13 },
  { t: 'Vampire', n: 1301 },
  { t: 'Vedalken', n: 175 },
  { t: 'Vehicle', n: 500 },
  { t: 'Villain', n: 321 },
  { t: 'Vivien', n: 31 },
  { t: 'Volver', n: 6 },
  { t: 'Vraska', n: 28 },
  { t: 'Wall', n: 514 },
  { t: 'Warlock', n: 430 },
  { t: 'Warrior', n: 2907 },
  { t: 'Weasel', n: 5 },
  { t: 'Weird', n: 31 },
  { t: 'Werewolf', n: 189 },
  { t: 'Whale', n: 34 },
  { t: 'Will', n: 14 },
  { t: 'Wizard', n: 3107 },
  { t: 'Wolf', n: 220 },
  { t: 'Wolverine', n: 14 },
  { t: 'Wombat', n: 5 },
  { t: 'World', n: 42 },
  { t: 'Worm', n: 26 },
  { t: 'Wraith', n: 79 },
  { t: 'Wrenn', n: 18 },
  { t: 'Wurm', n: 290 },
  { t: 'Yanggu', n: 7 },
  { t: 'Yanling', n: 6 },
  { t: 'Yeti', n: 29 },
  { t: 'Zariel', n: 5 },
  { t: 'Zombie', n: 1649 },
  { t: 'Zubera', n: 7 },
];

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
