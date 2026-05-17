# Issue: Design and Implement Custom Logo for Arcane Tutor

## Overview

This issue tracks the design and implementation of a custom logo for Arcane Tutor.
This is the final remaining item from the legal compliance checklist and represents a cosmetic enhancement that will further differentiate the project from Scryfall.

**Priority**: Low (cosmetic enhancement)  
**Impact**: Not required for legal compliance  
**Category**: Visual Design & UI  
**Status**: TODO

---

## Background

Arcane Tutor currently uses a text-only header displaying "Arcane Tutor" in the web interface.
While this is legally compliant and clearly differentiates from Scryfall, a custom logo would enhance the project's visual identity and branding.

**Related Documentation**:

- [legal_compliance_final_status.md](../legal/legal_compliance_final_status.md) - Final compliance review
- [compliance_review.md](../legal/compliance_review.md) - Detailed compliance status
- [about.md](../user/about.md) - Project mission and differentiation

---

## Requirements

### Legal Requirements

1. **Original Design**: Logo must be 100% original, not copied or derived from Scryfall's branding
1. **No Trademark Confusion**: Must not cause confusion with Scryfall or other MTG-related brands
1. **Distinct from Scryfall**: Should have a clearly different visual style from Scryfall's logo
1. **Proper Attribution**: If using any third-party elements (fonts, graphics), ensure proper licensing

### Design Requirements

1. **Consistent with Theme**: Should complement the blue gradient theme (Tolarian Academy inspired: #2b8fdf, #3da8f5)
1. **Scalable**: Should work at multiple sizes (favicon, header, documentation)
1. **Professional**: Should convey the project's focus on powerful card search capabilities
1. **MTG-Themed**: Should evoke Magic: The Gathering without using copyrighted WotC artwork
1. **Open Source Friendly**: Should be created with tools/methods that keep the source editable

### Technical Requirements

1. **Format**: SVG (scalable vector graphics) for web use
1. **Favicon**: Generate 16x16, 32x32, and 64x64 PNG versions for browser favicon
1. **Header**: Appropriate size for web header (recommend max 200px height)
1. **File Size**: Optimized for web delivery (SVG < 50KB, PNG favicon < 10KB each)
1. **Accessibility**: Should have good contrast in both light and dark themes

---

## Design Concepts to Consider

### Theme Ideas

1. **Arcane/Magical**: Represent the "Arcane" aspect
   - Spell book or grimoire
   - Magical symbols or runes
   - Mystical energy effects

1. **Tutor Reference**: Represent the "Tutor" aspect (MTG reference to tutoring/searching cards)
   - Magnifying glass searching through cards
   - Open book with card imagery
   - Library or study theme (aligns with Tolarian Academy inspiration)

1. **Combined Concept**: Merge both aspects
   - Magical tome being searched through
   - Arcane symbols combined with search/discovery imagery
   - Blue/mystical energy representing both magic and search power

### Color Palette

**Primary Colors** (matching current theme):

- Blue gradient: #2b8fdf → #3da8f5
- Accent: Lighter blue tints for highlights
- Contrast: Dark blue/navy for depth

**Avoid**:

- Scryfall's purple/blue palette
- Direct copies of existing MTG card art colors

---

## Implementation Tasks

### Phase 1: Design

- [ ] Research existing MTG-related logos to avoid similarity
- [ ] Sketch initial logo concepts (3-5 variations)
- [ ] Share concepts for feedback (GitHub issue discussion)
- [ ] Refine selected concept
- [ ] Create final logo design in SVG format

### Phase 2: Technical Implementation

- [ ] Create SVG file (optimized for web)
- [ ] Generate favicon sizes (16x16, 32x32, 64x64 PNG)
- [ ] Update `api/index.html` to use logo instead of text
- [ ] Update `api/favicon.ico` with new favicon
- [ ] Test logo appearance in light and dark themes
- [ ] Verify logo scales properly at different viewport sizes

### Phase 3: Documentation

- [ ] Add logo source files to repository (with editable format)
- [ ] Document logo design in [about.md](../user/about.md)
- [ ] Update README.md to show logo
- [ ] Add logo usage guidelines (if needed for community use)
- [ ] Update [legal.md](../legal/legal.md) with logo licensing information

### Phase 4: Verification

- [ ] Verify logo is distinct from Scryfall's branding
- [ ] Confirm logo works in all browser contexts
- [ ] Test accessibility (contrast ratios, screen reader descriptions)
- [ ] Update compliance documentation marking logo as complete

---

## Design Options

### Option 1: Community Design Contest

**Pros**:

- Multiple design variations to choose from
- Community engagement and ownership
- Potentially professional-quality results

**Cons**:

- Takes longer to organize and execute
- Need clear judging criteria
- Requires community participation

**Implementation**:

1. Create separate GitHub issue for design contest
1. Define submission guidelines and requirements
1. Set submission deadline (e.g., 2-4 weeks)
1. Community voting or maintainer selection
1. Award recognition to winning designer

### Option 2: Direct Design by Maintainer/Contributor

**Pros**:

- Faster implementation
- Direct control over design direction
- Immediate iteration possible

**Cons**:

- Limited to available design skills
- Less community involvement
- Single design perspective

**Implementation**:

1. Maintainer or volunteer designs logo
1. Share draft for feedback
1. Iterate based on feedback
1. Implement final design

### Option 3: Professional Designer (If Budget Available)

**Pros**:

- Highest quality professional result
- Clear design brief and deliverables
- Multiple concepts and iterations

**Cons**:

- Requires budget for design work
- May take longer for back-and-forth
- Need to ensure open source licensing

**Implementation**:

1. Write design brief
1. Engage designer (freelance or agency)
1. Review and iterate on concepts
1. Finalize and implement

---

## Acceptance Criteria

A logo is considered complete when:

1. ✅ Logo is original and distinct from Scryfall
1. ✅ Logo is implemented in the web interface header
1. ✅ Favicon is updated with logo-based icon
1. ✅ Logo works well in both light and dark themes
1. ✅ Logo is properly documented with licensing information
1. ✅ Logo source files are committed to repository
1. ✅ Compliance documentation updated to mark logo complete

---

## Timeline

**Suggested Timeline** (flexible based on chosen approach):

- Week 1-2: Design concept development
- Week 3: Implementation and testing
- Week 4: Documentation and finalization

**Note**: This is a low-priority enhancement and can be completed whenever resources permit.

---

## Resources

### Design Tools (Open Source Options)

- **Inkscape**: Free vector graphics editor for SVG creation
- **GIMP**: Free raster graphics editor for PNG export
- **Figma**: Free web-based design tool (limited free tier)

### Inspiration Sources

- MTG card backs (for thematic inspiration, not copying)
- Library/academy imagery (aligns with Tolarian Academy theme)
- Search/discovery iconography
- Magical/arcane symbol references (generic, not specific card art)

### Testing Tools

- **WebAIM Contrast Checker**: Verify accessibility
- **Favicon Generator**: Create multiple favicon sizes
- **SVG Optimizer (SVGO)**: Optimize SVG file size

---

## Related Issues

- Original Legal Compliance Checklist (parent issue)
- Visual Design & UI improvements

---

## Questions for Discussion

1. Which design approach should we use (community contest, direct design, professional)?
1. What design concept best represents "Arcane Tutor"?
1. Should the logo include text ("Arcane Tutor") or be icon-only?
1. What file formats do we need beyond SVG and PNG?

---

## Labels

- `enhancement`
- `design`
- `low-priority`
- `good-first-issue` (if community design contest)
- `legal-compliance`

---

**Created**: October 19, 2025  
**Related Compliance Item**: Visual Design & UI - Original logo and branding  
**Compliance Impact**: Low (cosmetic enhancement only)  
**Legal Status**: Current text-only header is fully compliant
