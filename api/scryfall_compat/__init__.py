"""Scryfall-compatible `/cards/*` API.

The routes in `routes.py` answer the same paths as api.scryfall.com with the same parameters and
the same response objects, so a client can be pointed at this host by swapping its base URL.
`objects.py` holds the payload construction — card reconstruction, List/Catalog/error envelopes,
and the `format=text` rendering — with no knowledge of Falcon or of the database.
"""

from api.scryfall_compat.routes import ScryfallCardsRoutes

__all__ = ["ScryfallCardsRoutes"]
