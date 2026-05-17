# About Arcane Tutor

## Project Mission

Arcane Tutor is an open-source Magic: The Gathering card search engine designed to provide a fast, powerful, and transparent alternative for the MTG community.
Our goal is to create a feature-rich search tool that respects intellectual property rights while offering unique capabilities and complete openness.

## Why Arcane Tutor Exists

### Open Source Philosophy

We believe in:

- **Transparency**: All code is open and available for review
- **Community ownership**: Anyone can contribute, fork, or self-host
- **Educational value**: Learn how card search engines work
- **Data sovereignty**: Run your own instance with your own data

### Unique Features

Arcane Tutor offers capabilities beyond traditional card search:

1. **Arithmetic Expressions**: Search with math like `cmc+1<power` or `power-toughness=0`
1. **No Pagination Limits**: Fetch more results than typical 175 card/page limits
1. **Performance Optimizations**: Custom PostgreSQL schema for fast queries
1. **Local Deployment**: Run your own instance via Docker
1. **Extensibility**: Open source means you can add your own features

### Alternative Implementation

While we use Scryfall's excellent card data (with attribution and compliance), Arcane Tutor is a completely independent implementation:

- All code written from scratch
- Original query parser and search algorithms
- Custom database schema
- Different UI/UX approach
- Unique feature set

## How We Differ from Scryfall

Arcane Tutor is not a clone of Scryfall.
Here's how we're different:

### Technical Differences

- **Original Codebase**: 100% original code, no copied implementation
- **Independent Database**: Custom PostgreSQL schema optimized for our use cases
- **Different Architecture**: Falcon-based API with bjoern WSGI server
- **Unique Parser**: Custom pyparsing-based query DSL implementation
- **Extended Features**: Arithmetic expressions and other unique capabilities

### Visual Design

- **Different Color Scheme**: Blue gradient theme (Tolarian Academy inspired)
- **Original Layout**: Custom card grid and modal display
- **Unique UI Components**: Original search controls and dropdowns
- **Different Typography**: Custom font choices (Beleren, MPlantin)

### Philosophy

- **Open Source First**: Complete transparency and community ownership
- **Self-Hostable**: Run your own instance with Docker
- **No Limits**: Designed for power users who need more data
- **Extensible**: Fork and customize for your needs

## Technology Stack

See [README.md](../README.md#code-organization) for detailed information about:

- Backend architecture (Python, Falcon, bjoern)
- Database design (PostgreSQL with optimized schema)
- Frontend implementation
- Deployment options (Docker)

## Data Sources & Attribution

Arcane Tutor uses card data from Scryfall's official bulk data API and serves card images via our own S3/CloudFront infrastructure.
All Magic: The Gathering content is © Wizards of the Coast LLC and used under their Fan Content Policy.

For complete details on data sources, attribution, and intellectual property, see [docs/legal.md](../legal/legal.md).

## Project Status

### Current Features ✅

- Complete search syntax support (matching Scryfall)
- Arithmetic expressions in queries
- Optimized database performance
- Docker deployment support
- Card tagging system
- Multiple sort and display options
- Light/dark theme support

### In Development 🚧

- Double-faced card support improvements
- Comprehensive tagging features
- Additional search operators (`cube:`, `papersets:`)
- Enhanced documentation

## Legal & Compliance

We take compliance seriously with original code, proper attribution, and adherence to all relevant policies.

See our compliance documentation:

- [legal.md](../legal/legal.md) - Data sources, attribution, and IP rights
- [terms_of_service.md](../user/terms_of_service.md) - User terms
- [privacy_policy.md](../user/privacy_policy.md) - Privacy practices
- [compliance_review.md](../legal/compliance_review.md) - Detailed compliance status (93% complete)

**Compliance Status**: ✅ Excellent standing - All critical legal and compliance items addressed.

## Contributing

Arcane Tutor is community-driven and welcomes contributions.
See [README.md](../README.md#developer-quick-start) for:

- Developer setup instructions
- How to report issues
- Pull request guidelines
- Testing procedures

## Acknowledgments

We are deeply grateful to:

- **[Scryfall](https://scryfall.com)** for comprehensive card data and public APIs
- **Wizards of the Coast** for creating Magic: The Gathering and supporting fan content
- **Open Source Community** for the tools that make this possible

See [legal.md](legal.md#scryfall-attribution) for detailed attribution.

## Contact & Support

- **GitHub Repository**: [github.com/jbylund/arcane_tutor](https://github.com/jbylund/arcane_tutor)
- **Issues & Bugs**: [GitHub Issues](https://github.com/jbylund/arcane_tutor/issues)
- **Documentation**: See [docs/](.) directory
- **Legal Inquiries**: Open an issue or contact repository owner

## License

Arcane Tutor code is licensed under the ISC License (see package.json).
This license applies only to our original code - see [legal.md](legal.md#license) for details on third-party intellectual property.

## Future Vision

We aim to:

- Continue expanding search capabilities
- Improve performance and user experience
- Add more unique features not found elsewhere
- Maintain strong compliance and attribution
- Grow the community of contributors
- Support self-hosted deployments

---

**Arcane Tutor**: An open-source Magic: The Gathering card search engine by the community, for the community.

_Last Updated: October 2025_
