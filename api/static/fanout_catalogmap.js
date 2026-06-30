class FanoutCatalogMap {
  constructor(mapping) {
    // One flat array sorted alphabetically (lowercase) for binary search.
    this._words = Object.entries(mapping)
      .map(([v, n]) => ({ v, n, lower: v.toLowerCase() }))
      .sort((a, b) => a.lower.localeCompare(b.lower));

    // Two-level trie: nodes store { start, end, best } where start/end are
    // indices into this._words and best is the highest-n word in that range.
    this._d1 = {}; // keyed by single char
    this._d2 = {}; // keyed by two-char string

    const words = this._words;
    let i = 0;
    while (i < words.length) {
      const c1 = words[i].lower[0];
      const d1start = i;
      while (i < words.length && words[i].lower[0] === c1) i++;
      const d1end = i;
      this._d1[c1] = { start: d1start, end: d1end, best: this._bestInRange(d1start, d1end) };

      let j = d1start;
      while (j < d1end) {
        const c2 = words[j].lower[1] ?? '';
        const key = c1 + c2;
        const d2start = j;
        while (j < d1end && (words[j].lower[1] ?? '') === c2) j++;
        const d2end = j;
        this._d2[key] = { start: d2start, end: d2end, best: this._bestInRange(d2start, d2end) };
      }
    }
  }

  _bestInRange(start, end) {
    let best = null;
    for (let i = start; i < end; i++) {
      if (!best || this._words[i].n > best.n) best = this._words[i];
    }
    return best?.v ?? null;
  }

  _lowerBound(prefix, start, end) {
    let lo = start,
      hi = end;
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
    const c1 = prefix[0];
    const node1 = this._d1[c1];
    if (!node1) return null;
    if (prefix.length === 1) return node1.best;

    const key = prefix.slice(0, 2);
    const node2 = this._d2[key];
    if (!node2) return null;
    if (prefix.length === 2) return node2.best;

    const { start, end } = node2;
    const pos = this._lowerBound(prefix, start, end);
    let best = null;
    for (let i = pos; i < end; i++) {
      if (!this._words[i].lower.startsWith(prefix)) break;
      if (!best || this._words[i].n > best.n) best = this._words[i];
    }
    return best?.v ?? null;
  }
}
