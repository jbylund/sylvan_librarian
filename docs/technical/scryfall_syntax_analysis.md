# Scryfall Search Syntax Analysis

## Comprehensive List of Scryfall Search Functionality

Based on the official Scryfall syntax documentation and analysis of the existing codebase, here are the search features:

### 1. Basic Text Search

- **name:** - Card name searches
- **o:** or **oracle:** - Oracle text searches
- **t:** or **type:** - Type line searches
- **flavor:** - Flavor text searches
- **a:** or **artist:** - Artist searches
- **lore:** - Lore/story searches

### 2. Card Properties

- **cmc:** - Converted mana cost (numeric)
- **power:** or **pow:** - Creature power (numeric)
- **toughness:** or **tou:** - Creature toughness (numeric)
- **loyalty:** - Planeswalker loyalty (numeric)
- **mana:** or **m:** - Mana cost searches
- **devotion:** - Mana symbol devotion (numeric)

### 3. Color and Identity

- **c:** or **color:** - Card colors
- **id:** or **identity:** or **coloridentity:** - Color identity
- **colorless** - Colorless cards

### 4. Rarity and Sets

- **r:** or **rarity:** - Card rarity (common, uncommon, rare, mythic)
- **s:** or **set:** - Set code
- **e:** or **edition:** - Set searches
- **cn:** or **number:** - Collector number
- **border:** - Border color (black, white, borderless, etc.)
- **frame:** - Frame version

### 5. Legality and Formats

- **f:** or **format:** - Format legality
- **legal:** - Legal in format
- **banned:** - Banned in format
- **restricted:** - Restricted in format

### 6. Card Layout and Features

- **layout:** - Card layout (normal, split, flip, etc.)
- **is:** - Special properties (permanent, spell, historic, etc.)
- **keyword:** or **k:** - Keyword abilities
- **produces:** - Mana production
- **watermark:** - Watermark searches ✅ Implemented

### 7. Prices and Market

- **usd:** - USD price (numeric)
- **eur:** - EUR price (numeric)
- **tix:** - MTGO ticket price (numeric)

### 8. Dates and Releases ✅ Implemented

- **year:** - Release year (numeric) with optimized date range queries
- **date:** - Specific release dates with all comparison operators

### 9. Game Mechanics

- **devotion:** - Mana symbol devotion counting

### 10. Advanced Features

- **cube:** - Cube inclusion
- **commander:** or **cmd:** - Commander-related searches
- **papersets:** - Paper set inclusion
- **is:booster** - Available in booster packs
- **is:spotlight** - Spotlight cards
- **is:timeshifted** - Timeshifted cards
- **is:colorshifted** - Colorshifted cards
- **is:futureshifted** - Futureshifted cards

### 11. Operators and Logic

- **Comparison operators**: `=`, `<`, `>`, `<=`, `>=`, `!=`, `<>`
- **Logic operators**: `AND`, `OR`, `NOT`, `-` (negation)
- **Parentheses**: `()` for grouping
- **Quotes**: `"text"` for exact phrases

### 12. Special Syntax

- **Regular expressions**: `/pattern/`

### 13. Oracle Tags

- **ot:** or **oracle_tags:** - Oracle tags

## Analysis Notes

This list represents the full scope of official Scryfall search functionality.
The current Scryfall OS implementation supports most of these features, with watermark search recently added to the supported feature set.

### Scryfall OS Extensions

**Enhanced Features**: Scryfall OS includes additional functionality beyond official Scryfall:

- **Arithmetic operations**: `cmc+power>5`, `power-toughness=0` (Scryfall OS extension)

### Excluded Features

**Alchemy-Only Features**: The following features are specific to Magic: The Gathering Arena's Alchemy format and are not implemented in Scryfall OS:

- `spellpower:` - Spell power (Alchemy format only)
- `spellresistance:` - Spell resistance (Alchemy format only)

These features are excluded because Scryfall OS focuses on traditional paper Magic and other formats where these mechanics don't apply.
