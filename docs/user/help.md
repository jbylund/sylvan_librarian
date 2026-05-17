# Arcane Tutor - User Help Guide

## Quick Start

Arcane Tutor is a powerful Magic: The Gathering card search engine.
This guide will help you find exactly the cards you're looking for.

## Basic Searches

### Search by Card Name

Simply type the card name in the search box:

```
Lightning Bolt
```

For partial matches, just type part of the name:

```
bolt
```

### Search by Type

Use `type:` or `t:` to find cards by their type:

```
type:Dragon
t:Planeswalker
t:instant
```

### Search by Color

Use `color:` or `c:` to find cards by color:

```
c:red
c:UR       (blue and red)
c:WUG      (white, blue, and green)
```

Use `id:` or `identity:` for color identity (includes mana symbols in text):

```
id:WU      (white/blue identity)
```

## Advanced Searches

### Numeric Comparisons

Search by mana cost, power, toughness, or loyalty using comparison operators:

```
cmc=3                    (converted mana cost exactly 3)
cmc<=2                   (2 or less mana)
power>=5                 (power 5 or greater)
toughness<3              (toughness less than 3)
loyalty>4                (loyalty greater than 4)
```

### Text Searches

Search within oracle text (rules text) or flavor text:

```
oracle:flying
o:"draw a card"          (exact phrase)
flavor:brother
```

### Rarity and Sets

Filter by rarity or specific sets:

```
rarity:mythic
r>=rare                  (rare or mythic)
set:MH2                  (Modern Horizons 2)
```

### Price Searches

Find cards within your budget:

```
usd<5                    (less than $5)
usd>=10                  (at least $10)
eur<2                    (less than €2)
```

## Combining Searches

Use `AND`, `OR`, and `NOT` to combine searches:

```
type:Dragon AND color:red
c:R OR c:G
type:instant NOT color:blue
```

Use parentheses for complex queries:

```
(type:instant OR type:sorcery) AND cmc<=2
```

## Common Search Patterns

### Budget Commander Creatures

```
type:legendary type:creature usd<5 identity:WU
```

### Efficient Removal Spells

```
(type:instant OR type:sorcery) oracle:destroy oracle:creature cmc<=3
```

### Card Draw in Blue

```
color:blue oracle:"draw" (type:instant OR type:sorcery)
```

### Mana Dorks (Mana Producing Creatures)

```
type:creature produces:any cmc<=2
```

### High Power Creatures

```
type:creature power>=8 cmc<=6
```

## Special Features

### Regular Expressions

Use `/pattern/` for regex searches:

```
name:/^Lightning/        (names starting with "Lightning")
```

### Arithmetic Expressions

Arcane Tutor supports math in queries (unique feature!):

```
cmc+1<power              (undercosted creatures)
power-toughness=0        (square creatures)
power+toughness>10       (total stats greater than 10)
```

### Format Legality

Search by format legality:

```
format:modern
legal:commander
banned:standard
```

### Keywords

Search for specific keyword abilities:

```
keyword:flying
keyword:vigilance
k:trample
```

## Tips for Better Searches

1. **Use quotes** for exact phrases: `oracle:"draw three cards"`
1. **Combine filters** to narrow results: `type:creature c:red cmc<=3 power>=2`
1. **Use negation** to exclude cards: `type:creature NOT color:black`
1. **Try shortcuts**: Use `t:` instead of `type:`, `c:` instead of `color:`
1. **Experiment with operators**: `>=`, `<=`, `!=` all work for numeric searches

## Sorting Results

Use the dropdown menus to sort your results:

- **Order By**: Choose what to sort by (EDHREC, CMC, Power, Rarity, Price)
- **Direction**: Toggle ascending/descending with the arrow button
- **Unique Mode**: Choose between unique cards, artwork, or printings
- **Prefer**: Select which printing to show (oldest, newest, cheapest, etc.)

## Need More Help?

- **Full Syntax Reference**: See [docs/scryfall_syntax_analysis.md](../technical/scryfall_syntax_analysis.md) for complete technical documentation
- **Functionality Analysis**: Check [docs/scryfall_functionality_analysis.md](../technical/scryfall_functionality_analysis.md) for detailed feature list
- **About Arcane Tutor**: Learn about the project at [about.md](../user/about.md)
- **Legal & Compliance**: Review our [legal.md](../legal/legal.md) for data sources and attribution
- **GitHub Issues**: Report problems or request features at [github.com/jbylund/arcane_tutor](https://github.com/jbylund/arcane_tutor/issues)

## Examples to Try

```
# Find cheap red creatures for aggro decks
type:creature c:red cmc<=3 power>=2 usd<1

# Find expensive blue counterspells
type:instant color:blue oracle:counter oracle:spell usd>5

# Find legendary creatures for commander under $10
type:legendary type:creature usd<10

# Find artifacts that cost 2 or less
type:artifact cmc<=2

# Find cards with "enters the battlefield" triggers
oracle:"enters the battlefield" type:creature

# Find planeswalkers in your colors
type:planeswalker id:WUG

# Find cards for mana fixing
produces:any type:land

# Find big creatures that are undercosted
type:creature cmc<=6 power>=8
```

---

**About**: Learn more about [Arcane Tutor](../user/about.md), our [legal compliance](../legal/legal.md), and [data sources](legal.md#data-sources).
