# Sylvan Librarian — Search Syntax

Enter any combination of filters below. Filters are AND-combined by default; see [Query Operators](#query-operators) for OR, negation, and grouping.

## Table of Contents

1. [Searchable Card Dimensions](#searchable-card-dimensions)
   - [Card Name](#card-name)
   - [Color](#color)
   - [Card Type](#card-type)
   - [Mana Value](#mana-value)
   - [Power and Toughness](#power-and-toughness)
   - [Oracle Text](#oracle-text)
   - [Format Legality](#format-legality)
   - [Keywords](#keywords)
   - [Rarity](#rarity)
   - [Set](#set)
   - [Price](#price)
   - [Loyalty](#loyalty)
   - [Mana Production](#mana-production)
2. [Query Operators](#query-operators)
3. [Arithmetic Expressions](#arithmetic-expressions)
4. [Sorting and Display](#sorting-and-display)

---

## Searchable Card Dimensions

### Card Name

Plain text searches card names. Partial matches work.

```
Lightning Bolt
bolt
```

### Color

Use `c:` or `color:` to filter by color. Use `id:` or `identity:` for color identity (includes mana symbols in oracle text).

Color letters: `W` (white), `U` (blue), `B` (black), `R` (red), `G` (green).

```
c:red          cards that are red
c:UR           cards that are blue and red
c:WUG          cards that are white, blue, and green
id:WU          cards with a white/blue identity
```

### Card Type

Use `t:` or `type:`. Partial words work. Stack multiple `t:` filters to require all types.

```
t:Dragon
t:instant
t:legendary t:creature
```

### Mana Value

Use `cmc` with a comparison operator (`=`, `<`, `>`, `<=`, `>=`, `!=`).

```
cmc=3
cmc<=2
cmc>=5
```

### Power and Toughness

Use `power` / `pow` and `toughness` / `tou`. Values can be compared to each other.

```
power>=5
toughness<3
pow>tou
```

### Oracle Text

Use `o:` or `oracle:` to search rules text. Quote phrases with spaces or punctuation. Use `flavor:` or `ft:` for flavor text.

```
o:flying
o:"draw a card"
o:"enters the battlefield"
flavor:brother
```

### Format Legality

Use `format:` or `f:` for cards legal in a format. Use `banned:` for banned cards, `legal:` as an alias for `format:`.

Supported formats: `standard`, `pioneer`, `modern`, `legacy`, `vintage`, `pauper`, `commander`, and more.

```
format:modern
f:commander
legal:standard
banned:legacy
```

### Keywords

Use `keyword:` or `k:` to search for keyword abilities.

```
keyword:flying
keyword:vigilance
k:trample
```

### Rarity

Use `r:` or `rarity:`. Comparison operators work — rarity order is common < uncommon < rare < mythic.

```
r:mythic
r:common
r>=rare
```

### Set

Use `set:` with the set code.

```
set:MH2
set:DMU
```

### Price

Use `usd` or `eur` with a comparison operator.

```
usd<5
usd>=10
eur<2
```

### Loyalty

Use `loyalty` or `loy` with a comparison operator.

```
t:planeswalker loyalty>4
loy=3
```

### Mana Production

Use `produces:` with color letters or `any`.

```
produces:any
produces:wu
t:land produces:any
```

---

## Query Operators

### AND / OR

All filters are implicitly AND. Use `OR` to require only one of several terms:

```
type:Dragon AND color:red
c:R OR c:G
```

### Negation

Prefix a keyword with `-` to exclude matches. `NOT` works as an alternative:

```
t:instant -c:blue
type:creature NOT color:black
```

### Grouping

Use parentheses to control how AND and OR interact:

```
(t:instant OR t:sorcery) cmc<=2
type:legendary (t:goblin OR t:elf)
```

### Exact Name

Prefix a name with `!` to match only that card:

```
!Fire
!"Lightning Bolt"
```

### Regular Expressions

Use `/pattern/` with `name:`, `type:`, or `oracle:`. Standard regex syntax applies.

```
name:/^Lightning/
o:/(flying|trample)/
```

---

## Arithmetic Expressions

Sylvan Librarian extends standard Scryfall syntax with arithmetic in numeric comparisons. Fields (`cmc`, `power`, `toughness`, `loyalty`) can be combined with `+`, `-`, `*`, `/` before comparing.

```
cmc+1<power          undercosted creatures
power-toughness=0    square creatures
power+toughness>10   high combined stats
```

---

## Sorting and Display

### Sort Order

Use the **Order By** dropdown to sort results by EDHREC rank, CMC, power, rarity, or price. Toggle ascending/descending with the **Direction** button.

### Grouping Results

Cards often have multiple printings. Use **Unique** to control what counts as one result:

- **Cards** (default) — one result per unique card, deduplicating across all printings
- **Art** — one result per unique illustration
- **Prints** — every printing shown separately

### Preferred Printing

When results are grouped by card or art, the **Prefer** setting controls which printing is surfaced: oldest, newest, cheapest (USD), most expensive (USD), and similar options for EUR.
