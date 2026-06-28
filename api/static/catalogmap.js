class CatalogMap {
  constructor(mapping) {
    this._map = new Map();
    for (const [v, n] of Object.entries(mapping)) {
      const letter = v[0].toLowerCase();
      if (!this._map.has(letter)) this._map.set(letter, []);
      this._map.get(letter).push({ v, n });
    }
    for (const bucket of this._map.values()) {
      bucket.sort((a, b) => a.v.localeCompare(b.v));
    }
  }

  get bool() {
    return this._map.size > 0;
  }

  get size() {
    let n = 0;
    for (const bucket of this._map.values()) {
      n += bucket.length;
    }
    return n;
  }

  getBestMatch(prefix) {
    const bucket = this._map.get(prefix[0]) ?? [];
    let best = null;
    for (const entry of bucket) {
      const lower = entry.v.toLowerCase();
      if (lower.slice(0, prefix.length) > prefix) break;
      if (lower.startsWith(prefix) && (!best || entry.n > best.n)) best = entry;
    }
    return best?.v ?? null;
  }
}
