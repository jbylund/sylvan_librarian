# Font Licensing Investigation

**Date**: October 19, 2025  
**Status**: ⚠️ **Action Required**  
**Priority**: High - Legal Compliance

---

## Overview

This document investigates the licensing situation for all fonts used in Arcane Tutor to ensure compliance with legal requirements and intellectual property rights.

---

## Fonts in Use

### 1. Mana Font ✅ **Compliant**

**Purpose**: Display Magic: The Gathering mana symbols  
**Source**: [mana-font on npm](https://www.npmjs.com/package/mana-font)  
**License**: SIL Open Font License (OFL)  
**Status**: ✅ **Fully Compliant**

**Details**:

- License allows free use, modification, and redistribution
- We are subsetting and hosting our own optimized version
- Attribution is maintained in documentation
- No legal concerns

**Documentation**: [docs/font_optimization.md](../fonts/font_optimization.md)

---

### 2. Beleren Font ✅ **Compliant**

**Purpose**: Card titles and type lines  
**Source**: [@saeris/typeface-beleren-bold](https://github.com/Saeris/typeface-beleren-bold) npm package  
**Version**: 1.0.1  
**License**: MIT License  
**Status**: ✅ **Fully Compliant**

**Details**:

- Licensed under MIT by Drake Costa
- Allows free use, modification, and redistribution
- We are subsetting and hosting our own optimized version
- Attribution is maintained in documentation
- No legal concerns

**License Text**:

```
Copyright (c) 2018 Drake Costa

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
```

**Documentation**: [docs/beleren_font.md](../fonts/beleren_font.md)

---

### 3. MPlantin Font ⚠️ **POTENTIAL ISSUE**

**Purpose**: Oracle text on cards  
**Source**: `fonts/mplantin.otf` (local file in repository)  
**License**: Commercial font by Monotype  
**Status**: ⚠️ **REQUIRES INVESTIGATION**

**Concerns**:

1. **Commercial Font**: MPlantin (Plantin MT) is a commercial font owned by Monotype, not a free or open-source font.

1. **Redistribution**: The font file `fonts/mplantin.otf` is checked into the repository, which may violate licensing terms if we don't have redistribution rights.

1. **Documentation Claims**: The file [docs/mplantin_font.md](../fonts/mplantin_font.md) states:

   > "MPlantin (Plantin MT) is a commercial font by Monotype. The font file in this repository (`fonts/mplantin.otf`) is used under license for this project."

1. **License Verification Needed**: We need to verify:
   - Do we actually have a valid license for MPlantin?
   - Does the license permit redistribution in open-source projects?
   - Does the license permit web font subsetting and hosting?
   - Is the license valid for all contributors and users?

---

## Risk Assessment

### Mana Font: ✅ **No Risk**

- Open license (OFL)
- Properly attributed
- Legal to use and redistribute

### Beleren Font: ✅ **No Risk**

- Open license (MIT)
- Properly attributed
- Legal to use and redistribute

### MPlantin Font: ⚠️ **HIGH RISK**

**Legal Risks**:

1. **Copyright Infringement**: Distributing a commercial font without proper license
1. **License Violation**: Using font beyond scope of any existing license
1. **Trademark Issues**: Potential issues with Monotype's intellectual property
1. **Repository Liability**: All users cloning the repository may be receiving unlicensed content

**Impact on Compliance**:

- This could affect our overall legal compliance standing
- May need to be addressed before claiming full compliance
- Could be flagged in legal review or audit

---

## Recommended Actions

### Immediate (High Priority)

1. **Verify License Status**
   - [ ] Check if project maintainer has valid MPlantin license
   - [ ] Review license terms for redistribution rights
   - [ ] Verify web font subsetting and hosting is permitted
   - [ ] Document license details if valid

1. **If No Valid License: Remove MPlantin**
   - [ ] Remove `fonts/mplantin.otf` from repository
   - [ ] Update `.gitignore` to prevent future commits
   - [ ] Remove font from git history (optional, for clean slate)
   - [ ] Update documentation to remove MPlantin references

1. **Find Alternative Solution**

   Choose one of these options:

   **Option A: Use Free Alternative Font**
   - Replace with similar serif font (Georgia, Garamond, etc.)
   - Update CSS to use system fonts only
   - Document the change

   **Option B: Use Licensed Adobe Fonts** (if available)
   - Use Adobe Fonts (Typekit) integration
   - Requires Adobe Creative Cloud subscription
   - Fonts served from Adobe's servers, not ours
   - Complies with Adobe's terms of service

   **Option C: License MPlantin Properly**
   - Purchase web font license from Monotype
   - Ensure license permits open-source usage
   - Document license in repository
   - May be expensive and still restrict redistribution

   **Option D: No Custom Oracle Text Font** (Simplest)
   - Use system serif fonts only: `font-family: Georgia, 'Times New Roman', serif;`
   - Remove all MPlantin references
   - Still maintains readable oracle text
   - No licensing concerns

---

## Recommended Solution: Option D (System Fonts)

**Rationale**:

1. **Immediate Compliance**: No licensing issues
1. **No Cost**: Free for all users
1. **Simplest Implementation**: Just update CSS
1. **Good UX**: Georgia is a high-quality serif font
1. **Cross-Platform**: Available on all systems
1. **Performance**: No font download needed

**Implementation**:

1. Remove `fonts/mplantin.otf`
1. Update CSS to: `font-family: Georgia, 'Palatino Linotype', 'Book Antiqua', Palatino, serif;`
1. Remove MPlantin documentation
1. Update legal documentation to reflect font changes

---

## Implementation Steps

### Step 1: Remove MPlantin Font

```bash
# Remove font file
git rm fonts/mplantin.otf

# Update .gitignore to prevent future additions
echo "fonts/*.otf" >> .gitignore
echo "fonts/*.ttf" >> .gitignore
```

### Step 2: Update CSS

In `api/index.html`, replace:

```css
font-family: 'MPlantin', Georgia, serif;
```

With:

```css
font-family: Georgia, 'Palatino Linotype', 'Book Antiqua', Palatino, serif;
```

Also remove the link to MPlantin CSS from CDN:

```html
<!-- Remove this -->
<link
  href="https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mplantin/mplantin-subset.css"
  rel="stylesheet"
  type="text/css"
  media="print"
  onload="this.media='all'"
/>
```

### Step 3: Update Documentation

Files to update:

- [x] Remove or archive `docs/mplantin_font.md`
- [x] Update `docs/legal.md` to remove MPlantin references
- [x] Update `docs/beleren_font.md` to remove MPlantin cross-references
- [x] Update `docs/compliance_review.md`
- [x] Update `docs/about.md`
- [x] Update any other documentation mentioning MPlantin

### Step 4: Update Scripts

Remove or archive:

- `scripts/subset_mplantin_font.py` (if it exists)
- Any Makefile targets for MPlantin

### Step 5: Verify Changes

- [ ] Test that oracle text displays correctly with Georgia font
- [ ] Verify no broken font links in browser console
- [ ] Check that all documentation is updated
- [ ] Ensure legal compliance documentation is accurate

---

## Alternative Fonts Comparison

| Font                           | License    | Quality | Cost | Compliance Risk |
| ------------------------------ | ---------- | ------- | ---- | --------------- |
| **Georgia** (system)           | Free       | High    | $0   | ✅ None         |
| **Palatino** (system)          | Free       | High    | $0   | ✅ None         |
| **Garamond** (system)          | Free       | Medium  | $0   | ✅ None         |
| **Libre Baskerville** (Google) | OFL        | High    | $0   | ✅ None         |
| **Crimson Text** (Google)      | OFL        | High    | $0   | ✅ None         |
| **MPlantin** (commercial)      | Commercial | High    | $$$  | ⚠️ High         |

---

## Updated Font Stack Recommendation

For oracle text, use this font stack:

```css
.card-text,
.modal-card-text {
  font-family: Georgia, 'Palatino Linotype', 'Book Antiqua', Palatino, serif;
  /* Fallback chain prioritizes Georgia, then Palatino variants */
}
```

This provides:

- ✅ Professional serif appearance
- ✅ Excellent readability
- ✅ No licensing concerns
- ✅ Universal availability
- ✅ No external font loading
- ✅ Better performance

---

## Impact on Legal Compliance

**Before MPlantin Removal**:

- ⚠️ Potential copyright infringement risk
- ⚠️ Commercial font in open-source repository
- ⚠️ Unclear licensing status

**After MPlantin Removal**:

- ✅ All fonts properly licensed
- ✅ No commercial font concerns
- ✅ Full compliance with open-source principles
- ✅ No redistribution restrictions

**Compliance Score Impact**:

- Current: May reduce compliance from 93% to lower due to licensing concern
- After fix: Maintains or improves 93% compliance rating
- Risk Level: Reduced from Medium to Very Low

---

## Conclusion

**Recommendation**: Remove MPlantin font and use system fonts (Georgia) for oracle text.

**Benefits**:

1. ✅ Immediate legal compliance
1. ✅ No licensing costs
1. ✅ Simpler implementation
1. ✅ Better performance (no font download)
1. ✅ Universal compatibility
1. ✅ Maintains professional appearance

**Action Required**: Repository maintainer should decide on approach and implement changes.

---

## Related Documentation

- [font_optimization.md](../fonts/font_optimization.md) - Mana font subsetting
- [beleren_font.md](../fonts/beleren_font.md) - Beleren font integration
- [legal.md](../legal/legal.md) - Overall legal compliance
- [legal_compliance_final_status.md](../legal/legal_compliance_final_status.md) - Compliance status

---

**Last Updated**: October 19, 2025  
**Next Action**: Decision needed on MPlantin font approach  
**Priority**: High - impacts legal compliance standing
