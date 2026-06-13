# Contributing to Arcane Tutor

Thanks for your interest in contributing! Arcane Tutor is a community-driven,
open-source Magic: The Gathering card search engine.

## Getting Started

See the [Developer Quick Start](../README.md#developer-quick-start) in the README
for prerequisites and setup instructions. The short version:

```bash
make dev-up    # start services (Docker)
make test      # run the full test suite (~1,800 tests)
make lint      # ruff + prettier
```

## Reporting Issues

Open an issue using one of the [issue templates](https://github.com/jbylund/arcane_tutor/issues/new/choose).
For search bugs, please include the exact query you ran and what you expected —
a link to the equivalent [Scryfall](https://scryfall.com) search is especially helpful,
since Scryfall syntax compatibility is a core goal.

Security issues should be reported per the [security policy](../SECURITY.md) rather
than as public issues.

## Pull Requests

1. Fork the repo and create a branch from `main`.
1. Make your change, including tests for new behavior. Parser changes should
   keep the parity suite green (`api/parsing/tests/`).
1. Run `make test` and `make lint` before pushing. Auto-fix most lint issues with:
   ```bash
   python -m ruff check --fix --unsafe-fixes .
   python -m ruff format .
   ```
1. Open a pull request describing what changed and why.

### Code Style

- **Python:** `ruff`, line length 132, Google docstring convention (config in `pyproject.toml`)
- **Rust:** standard `rustfmt` in `card_engine/`
- **HTML/JS:** `prettier` (config in `.prettierrc`)

### Documentation

Docs live in [docs/](README.md), organized by topic. When adding documentation,
place it in the appropriate topical directory, link it from
[docs/README.md](README.md), and use relative links.

## Legal Notes

By contributing, you agree your contributions are licensed under the
[ISC License](../LICENSE). Card data and artwork are © Wizards of the Coast LLC —
see [legal.md](legal/legal.md) for attribution and compliance
details before adding anything that touches card data, images, or fonts.
