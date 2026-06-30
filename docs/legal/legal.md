# Legal Compliance & Data Sources

## Overview

Sylvan Librarian is an open-source Magic: The Gathering card search engine that respects intellectual property rights and complies with relevant policies and guidelines.

**Related Documentation:**
- **[legal_compliance_summary.md](../legal/legal_compliance_summary.md)** - Quick overview and recommendations
- [terms_of_service.md](../user/terms_of_service.md) - User agreement and service terms
- [privacy_policy.md](../user/privacy_policy.md) - Data collection and privacy practices
- [compliance_review.md](../legal/compliance_review.md) - Detailed compliance status and checklist
- [about.md](../user/about.md) - Project mission and how we differ from Scryfall

## Data Sources

### Primary Data Source

Sylvan Librarian uses card data from **Scryfall's official bulk data API** (https://api.scryfall.com/bulk-data), which provides comprehensive Magic: The Gathering card information.

- **Data Provider**: Scryfall (https://scryfall.com)
- **API Documentation**: https://scryfall.com/docs/api
- **Usage**: We use Scryfall's bulk data exports for card information including names, types, oracle text, mana costs, and pricing data
- **Compliance**: We comply with Scryfall's API Terms of Service and rate limiting guidelines

### Card Images

Card images are processed from Scryfall's PNG images and served via our own infrastructure:

- **Image Source**: Derived from Scryfall's PNG card images
- **Storage**: Amazon S3 bucket
- **Delivery Method**: CloudFront CDN at `d1hot9ps2xugbc.cloudfront.net`
- **Attribution**: We acknowledge Scryfall as the original source of the PNG images
- **Compliance**: Card images are used in accordance with Scryfall's API Terms of Service and Wizards of the Coast's Fan Content Policy
- **Rights**: All card artwork is © Wizards of the Coast LLC

### Font Assets

- **Mana symbols**: Custom font implementation for displaying mana costs
- **Beleren font**: Used for card text display (respecting font licensing)
- **MPlantin font**: Used for flavor text and card names

## Intellectual Property Attribution

### Wizards of the Coast

Magic: The Gathering is a trademark of Wizards of the Coast LLC, a subsidiary of Hasbro, Inc.

**All card data, names, artwork, and game content are:**
- © Wizards of the Coast LLC
- Portions of Sylvan Librarian are unofficial Fan Content permitted under the Wizards of the Coast Fan Content Policy
- Not approved/endorsed by Wizards of the Coast
- Portions of the materials used are property of Wizards of the Coast. © Wizards of the Coast LLC.

### Scryfall Attribution

We acknowledge and thank Scryfall (https://scryfall.com) for:
- Providing comprehensive bulk data APIs
- Maintaining high-quality card information
- Supporting the Magic: The Gathering community

Sylvan Librarian is an **independent implementation** and is **not affiliated with, endorsed by, or sponsored by Scryfall**.

## Compliance with Policies

### Wizards of the Coast Fan Content Policy

This project operates under the Wizards of the Coast Fan Content Policy:
- URL: https://company.wizards.com/en/legal/fancontentpolicy
- We do not claim ownership of Wizards of the Coast's intellectual property
- This is a non-commercial, community-driven project
- We clearly state that this is fan-made content not officially sanctioned by Wizards

### Scryfall API Terms of Service

We comply with Scryfall's API Terms of Service:
- Respect rate limiting guidelines
- Use bulk data exports rather than excessive API calls
- Provide proper attribution to Scryfall
- Do not claim ownership of Scryfall's data or branding

## Differentiation from Scryfall

Sylvan Librarian is intentionally designed to be different from Scryfall:

### Technical Differences
- **Original codebase**: All code is written from scratch
- **Different database schema**: Custom PostgreSQL schema optimized for our use case
- **Independent search implementation**: Original query parser and search algorithms
- **Unique features**: Arithmetic expressions in queries, different sorting options

### Visual Differences
- **Different color scheme**: Blue gradient theme inspired by Tolarian Academy (distinct from Scryfall's purple/blue palette)
- **Original layout**: Custom card display and grid system
- **Different UI components**: Custom search interface and controls
- **Original iconography**: Custom theme toggle and UI elements

### Naming and Branding
- **Project name**: "Sylvan Librarian" - a unique name that doesn't claim to be Scryfall
- **No trademark confusion**: We do not claim to be Scryfall or use their branding
- **Clear attribution**: We acknowledge Scryfall as a data source

## License

This project is licensed under the ISC License (see package.json).
However, this license applies only to our original code and does not grant any rights to:
- Wizards of the Coast's intellectual property
- Scryfall's data or branding
- Third-party fonts or assets

## Trademark Usage

### "Magic: The Gathering"
We use the trademark "Magic: The Gathering™" in accordance with Wizards of the Coast's Fan Content Policy:
- We acknowledge it as a trademark of Wizards of the Coast LLC
- We do not claim any ownership or affiliation
- We use it only to describe the type of content our application searches

### "Scryfall"
We acknowledge "Scryfall" as a trademark/brand:
- Our project name "Sylvan Librarian" is unique and doesn't cause confusion
- We are not affiliated with or endorsed by Scryfall
- We do not claim to be Scryfall or use their exact branding

## User Privacy

Currently, Sylvan Librarian:
- Does not collect personal information
- Does not use cookies for tracking
- Uses localStorage only for theme preference
- Does not share data with third parties

A formal Privacy Policy will be added as the project evolves.

## Terms of Service

Users of Sylvan Librarian should understand:
- This is provided "as-is" without warranty
- Card data accuracy depends on upstream sources (Scryfall)
- This is a community project not endorsed by Wizards of the Coast
- Users should respect all intellectual property rights

A formal Terms of Service will be added as the project evolves.

## Contact and Takedown Policy

If you believe this project infringes on your intellectual property rights:
- Please open an issue on GitHub: https://github.com/jbylund/sylvan_librarian/issues
- Or contact the repository owner directly
- We will respond promptly to any legitimate concerns

## Changelog

- **2025-01**: Initial legal.md created documenting data sources and compliance approach
- This document will be updated as the project evolves and policies are refined

## Future Compliance Work

Planned documentation improvements:
- [ ] Formal Terms of Service document
- [ ] Formal Privacy Policy document
- [ ] Detailed API usage guidelines
- [ ] Regular compliance audits
- [ ] Legal review as project scales

---

**Last Updated**: January 2025

This document reflects our good-faith effort to comply with all applicable policies and respect intellectual property rights.
We welcome feedback and will address any concerns promptly.
