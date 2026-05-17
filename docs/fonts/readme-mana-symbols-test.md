# Mana Symbol Test Page

## Overview

The `mana-symbols-test.html` page is a diagnostic tool created to help identify rendering issues with mana symbols, particularly hybrid mana symbols.

## Purpose

This page displays all mana symbols used in Scryfall OS in a comprehensive table format, making it easy to:

- Identify symbols that are not rendering correctly
- Compare the emoji representation with the mana font representation
- Verify that all symbols in the codebase have proper mappings
- Test new symbols before adding them to the main application

## Features

### Display Table

The page shows three columns for each symbol:

1. **Raw Mana Symbol**: The actual symbol text (e.g., `{W/U}`, `{R}`, `{T}`)
1. **Emoji Representation**: The emoji/unicode character used in tooltips and text-only contexts
1. **Symbol Representation**: The visual icon from the Mana font (should display as a styled icon)

### Filters

- **Show All** (default): Displays all 64 symbols
- **Regular Only**: Shows only non-hybrid symbols (34 symbols)
- **Hybrid Only**: Shows hybrid and phyrexian mana symbols (30 symbols)
- **Phyrexian**: Shows only phyrexian-related symbols

### Search

The search box allows you to filter symbols by:

- Symbol text (e.g., "W/U", "tap", "phyrexian")
- Emoji representation
- CSS class names

### Statistics

Live counters show:

- Total symbols currently displayed
- Count of regular symbols
- Count of hybrid symbols

## Symbol Categories

### Regular Mana Symbols (34)

- Basic colors: `{W}`, `{U}`, `{B}`, `{R}`, `{G}`, `{C}`
- Numbers: `{0}` through `{16}`
- Variables: `{X}`, `{Y}`, `{Z}`
- Special: `{T}` (tap), `{Q}` (untap), `{E}` (energy), `{P}` (phyrexian), `{S}` (snow), `{CHAOS}`, `{PW}`, `{∞}`

### Hybrid Mana Symbols (30)

- Two-color hybrid: `{W/U}`, `{U/B}`, `{B/R}`, `{R/G}`, `{G/W}`, `{W/B}`, `{U/R}`, `{B/G}`, `{R/W}`, `{G/U}`
- Generic hybrid (2/color): `{2/W}`, `{2/U}`, `{2/B}`, `{2/R}`, `{2/G}`
- Phyrexian mana: `{W/P}`, `{U/P}`, `{B/P}`, `{R/P}`, `{G/P}`
- Three-color phyrexian: `{W/U/P}`, `{W/B/P}`, `{U/B/P}`, `{U/R/P}`, `{B/R/P}`, `{B/G/P}`, `{R/W/P}`, `{R/G/P}`, `{G/W/P}`, `{G/U/P}`

## Usage

### Local Development

1. Start a local web server in the `api` directory:

   ```bash
   cd api
   python -m http.server 8888
   ```

1. Open your browser to:
   ```
   http://localhost:8888/mana-symbols-test.html
   ```

### Production/Deployment

Access the page at `/mana-symbols-test.html` on your deployed instance.

## Troubleshooting

### Mana Font Symbols Not Displaying

If the third column (Symbol Representation) shows empty spaces:

1. **Check CDN Loading**: The page loads the Mana font from CloudFront CDN:

   ```
   https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana/mana-subset.css
   ```

1. **Check Browser Console**: Look for any errors related to loading the CSS or font files

1. **Verify Font Classes**: The CSS classes follow the pattern `ms ms-{symbol} ms-cost`
   - Example: `{W/U}` uses class `ms ms-wu ms-cost`

1. **Test in Different Browsers**: Some browsers may have issues with web fonts

### Adding New Symbols

When adding a new mana symbol to the application:

1. Update `manaMap` or `hybridMap` in `api/index.html`
1. Update `manaTextMap` in `api/index.html` with the emoji representation
1. Update the corresponding maps in `api/mana-symbols-test.html`
1. Verify the symbol displays correctly on the test page

## Technical Details

### Data Sources

The page uses the same symbol maps as the main application (`index.html`):

- `manaMap`: Regular mana symbols and their CSS classes
- `hybridMap`: Hybrid mana symbols and their CSS classes
- `manaTextMap`: Emoji/unicode representations for tooltips

### Styling

The page uses:

- CSS Grid for the table layout
- Same gradient theme as the main application
- Responsive design for mobile and desktop
- Hover effects for better UX

## Related Files

- `/api/index.html` - Main application (source of symbol definitions)
- `/api/tests/test_mana_symbol_performance_comparison.js` - Performance testing for symbol conversion
- `/scripts/subset_mana_font.py` - Font subsetting script
- `/api/tests/MANA_SYMBOL_PERFORMANCE.md` - Documentation on symbol rendering optimization

## Maintenance

This page should be updated whenever:

- New mana symbols are added to Magic: The Gathering
- Symbol mappings change in the main application
- The Mana font is updated to a new version
- CSS classes for symbols are modified
