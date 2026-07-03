const HTML_ESCAPE_RE = /[&<>"]/g;
const HTML_ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
const INITIAL_PAGE_TITLE = document.title;

function escapeHtml(str) {
  if (str == null) return '';
  return String(str).replace(HTML_ESCAPE_RE, c => HTML_ESCAPE_MAP[c]);
}

function buildImageUrl(card, size) {
  const face = card.face_idx || 1;
  return `https://d1hot9ps2xugbc.cloudfront.net/img/${card.set_code}/${card.collector_number}/${face}/${size}.webp`;
}

const MANA_SYMBOLS = new Map([
  ['{W}', 'ms ms-w ms-cost'],
  ['{U}', 'ms ms-u ms-cost'],
  ['{B}', 'ms ms-b ms-cost'],
  ['{R}', 'ms ms-r ms-cost'],
  ['{G}', 'ms ms-g ms-cost'],
  ['{C}', 'ms ms-c ms-cost'],
  ['{0}', 'ms ms-0 ms-cost'],
  ['{1}', 'ms ms-1 ms-cost'],
  ['{2}', 'ms ms-2 ms-cost'],
  ['{3}', 'ms ms-3 ms-cost'],
  ['{4}', 'ms ms-4 ms-cost'],
  ['{5}', 'ms ms-5 ms-cost'],
  ['{6}', 'ms ms-6 ms-cost'],
  ['{7}', 'ms ms-7 ms-cost'],
  ['{8}', 'ms ms-8 ms-cost'],
  ['{9}', 'ms ms-9 ms-cost'],
  ['{10}', 'ms ms-10 ms-cost'],
  ['{11}', 'ms ms-11 ms-cost'],
  ['{12}', 'ms ms-12 ms-cost'],
  ['{13}', 'ms ms-13 ms-cost'],
  ['{14}', 'ms ms-14 ms-cost'],
  ['{15}', 'ms ms-15 ms-cost'],
  ['{16}', 'ms ms-16 ms-cost'],
  ['{X}', 'ms ms-x ms-cost'],
  ['{Y}', 'ms ms-y ms-cost'],
  ['{Z}', 'ms ms-z ms-cost'],
  ['{T}', 'ms ms-tap'],
  ['{Q}', 'ms ms-untap'],
  ['{E}', 'ms ms-energy'],
  ['{P}', 'ms ms-p ms-cost'],
  ['{S}', 'ms ms-s ms-cost'],
  ['{CHAOS}', 'ms ms-chaos'],
  ['{PW}', 'ms ms-pw'],
  ['{∞}', 'ms ms-infinity'],
  ['{W/U}', 'ms ms-wu ms-cost'],
  ['{U/B}', 'ms ms-ub ms-cost'],
  ['{B/R}', 'ms ms-br ms-cost'],
  ['{R/G}', 'ms ms-rg ms-cost'],
  ['{G/W}', 'ms ms-gw ms-cost'],
  ['{W/B}', 'ms ms-wb ms-cost'],
  ['{U/R}', 'ms ms-ur ms-cost'],
  ['{B/G}', 'ms ms-bg ms-cost'],
  ['{R/W}', 'ms ms-rw ms-cost'],
  ['{G/U}', 'ms ms-gu ms-cost'],
  ['{2/W}', 'ms ms-2w ms-cost'],
  ['{2/U}', 'ms ms-2u ms-cost'],
  ['{2/B}', 'ms ms-2b ms-cost'],
  ['{2/R}', 'ms ms-2r ms-cost'],
  ['{2/G}', 'ms ms-2g ms-cost'],
  ['{W/P}', 'ms ms-wp ms-cost'],
  ['{U/P}', 'ms ms-up ms-cost'],
  ['{B/P}', 'ms ms-bp ms-cost'],
  ['{R/P}', 'ms ms-rp ms-cost'],
  ['{G/P}', 'ms ms-gp ms-cost'],
  ['{W/U/P}', 'ms ms-wup ms-cost'],
  ['{W/B/P}', 'ms ms-wbp ms-cost'],
  ['{U/B/P}', 'ms ms-ubp ms-cost'],
  ['{U/R/P}', 'ms ms-urp ms-cost'],
  ['{B/R/P}', 'ms ms-brp ms-cost'],
  ['{B/G/P}', 'ms ms-bgp ms-cost'],
  ['{R/W/P}', 'ms ms-rwp ms-cost'],
  ['{R/G/P}', 'ms ms-rgp ms-cost'],
  ['{G/W/P}', 'ms ms-gwp ms-cost'],
  ['{G/U/P}', 'ms ms-gup ms-cost'],
]);
const MANA_RE = /\{[^}]{1,5}\}/g;

function convertManaSymbols(text) {
  if (!text) return '';
  return text.replace(MANA_RE, match => {
    const cls = MANA_SYMBOLS.get(match);
    return cls ? `<span class="modal-mana-symbol ${cls}"></span>` : escapeHtml(match);
  });
}

function formatOracleText(text) {
  if (!text) return '';
  return convertManaSymbols(escapeHtml(text)).replace(/\n/g, '<br>');
}

function renderCardFace(card) {
  const imageLarge = buildImageUrl(card, '745');
  const imgTag = `<img class="modal-image" src="${escapeHtml(imageLarge)}" width="745" height="1040" alt="${escapeHtml(card.name || '')}" />`;
  let imageHtml;
  if (card.set_code && card.collector_number) {
    // Build manapool.com referral URL — set codes and collector numbers from our database are safe for URLs
    const manapoolUrl = `https://manapool.com/card/${card.set_code.toLowerCase()}/${card.collector_number}?ref=sylvan-librarian`;
    imageHtml = `<div class="modal-image-wrapper"><a href="${manapoolUrl}" target="_blank" rel="noopener" class="modal-image-link">${imgTag}</a></div>`;
  } else {
    imageHtml = `<div class="modal-image-wrapper">${imgTag}</div>`;
  }

  const hasPT = card.power != null && card.toughness != null;

  return `
    ${imageHtml}
    <div class="modal-card-info">
      <div class="modal-card-name-mana-row">
        <div class="modal-card-name">${escapeHtml(card.name || 'Unknown Card')}</div>
        ${card.mana_cost ? `<div class="modal-card-mana">${convertManaSymbols(card.mana_cost)}</div>` : ''}
      </div>
      ${card.type_line ? `<div class="modal-card-type">${escapeHtml(card.type_line)}</div>` : ''}
      ${card.oracle_text ? `<div class="modal-card-text">${formatOracleText(card.oracle_text)}</div>` : ''}
      ${
        card.set_name || hasPT
          ? `
      <div class="modal-card-set-power-row">
        ${card.set_name ? `<div class="modal-card-set">${escapeHtml(card.set_name)}</div>` : '<div class="modal-card-set"></div>'}
        ${hasPT ? `<div class="modal-card-power-toughness">${escapeHtml(card.power)} / ${escapeHtml(card.toughness)}</div>` : ''}
      </div>`
          : ''
      }
    </div>
  `;
}

function formatUsd(value) {
  return value == null ? '' : ` ($${Number(value).toFixed(2)})`;
}

// Escapes \ and " for embedding in a double-quoted exact-name query literal (e.g. !"...").
// The query grammar (api/parsing/hand_parser.py) treats \ as an escape char inside quoted
// strings, so a name containing a literal " — e.g. Kongming, "Sleeping Dragon" — would otherwise
// terminate the string early and get parsed as several unrelated AND clauses.
function escapeExactName(name) {
  return name.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

// Printings that share the same illustration_id are collapsed into a single thumbnail — the one
// with the highest prefer_score represents the group, badged with how many others it stands in
// for. Groups are then sorted by that max prefer_score, highest first.
function groupPrintingsByArt(cards) {
  const groups = new Map();
  for (const card of cards) {
    const key = card.illustration_id || `${card.set_code}/${card.collector_number}`;
    const group = groups.get(key);
    if (!group) {
      groups.set(key, { representative: card, count: 1 });
    } else {
      group.count += 1;
      if ((card.prefer_score ?? -Infinity) > (group.representative.prefer_score ?? -Infinity)) {
        group.representative = card;
      }
    }
  }
  return [...groups.values()].sort(
    (a, b) => (b.representative.prefer_score ?? -Infinity) - (a.representative.prefer_score ?? -Infinity)
  );
}

function renderPrintingsStrip(groups) {
  return groups
    .map(({ representative: card, count }) => {
      const thumb = buildImageUrl(card, '280');
      const url = `/card/${card.set_code}/${card.collector_number}`;
      const label = escapeHtml(`${card.set_name || card.set_code || ''}${formatUsd(card.price_usd)}`);
      const badge = count > 1 ? `<span class="printing-thumb-count">+${count - 1}</span>` : '';
      return `<a href="${escapeHtml(url)}" class="printing-thumb" title="${label}"><img src="${escapeHtml(thumb)}" alt="${label}" loading="lazy" />${badge}</a>`;
    })
    .join('');
}

async function main() {
  const parts = window.location.pathname.split('/').filter(Boolean);
  if (parts.length < 3 || parts[0] !== 'card') {
    document.getElementById('card-loading').textContent = 'Invalid card URL.';
    return;
  }
  const [, rawSetCode, collectorNumber] = parts;
  const setCode = rawSetCode.toLowerCase();

  let card;
  try {
    const resp = await fetch(`/search?q=${encodeURIComponent(`set:${setCode} cn:${collectorNumber}`)}&unique=printing`);
    const data = await resp.json();
    card = data.cards?.[0];
  } catch (_) {
    document.getElementById('card-loading').textContent = 'Failed to load card.';
    return;
  }

  if (!card) {
    document.getElementById('card-loading').textContent = 'Card not found.';
    return;
  }

  const siteName = document.getElementById('site-title')?.textContent?.trim() || INITIAL_PAGE_TITLE;
  document.title = `${card.name} - ${siteName}`;

  const cardFace = document.getElementById('card-face');
  cardFace.innerHTML = renderCardFace(card);
  cardFace.style.display = '';
  document.getElementById('card-loading').style.display = 'none';

  try {
    const printingFields = 'set_code,collector_number,set_name,illustration_id,price_usd,prefer_score';
    const resp = await fetch(
      `/search?q=${encodeURIComponent(`!"${escapeExactName(card.name)}"`)}&unique=printing&fields=${printingFields}`
    );
    const data = await resp.json();
    const others = (data.cards || []).filter(p => !(p.set_code === setCode && p.collector_number === collectorNumber));
    if (others.length > 0) {
      const groups = groupPrintingsByArt(others);
      document.getElementById('printings-list').innerHTML = renderPrintingsStrip(groups);
      document.getElementById('other-printings').style.display = '';
    }
  } catch (_) {
    // Other printings are non-critical; fail silently.
  }
}

(function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  const toggle = document.getElementById('themeToggle');
  const icon = document.getElementById('themeIcon');
  if (!toggle || !icon) return;
  function updateIcon() {
    icon.textContent = document.documentElement.getAttribute('data-theme') === 'dark' ? '☀️' : '🌙';
  }
  updateIcon();
  toggle.addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    updateIcon();
  });
})();

main();
