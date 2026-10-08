"""Response plumbing shared by every Scryfall-compatible surface.

The `/cards/*` routes and the reference routes (`/sets`, `/catalog`, `/symbology`) are separate
mixins on `APIResource`, but they answer with the same content type, the same `pretty` handling and
the same rule for errors: a handler returns a Scryfall error object rather than raising, and the
status rides on the payload so the generic Falcon error serializer never sees it. That rule lives
here so both surfaces cannot drift apart on it.
"""

from __future__ import annotations

from typing import Any

import falcon
import falcon.util
import orjson

# Spelled out rather than falcon.MEDIA_JSON, which omits the charset Scryfall sends.
JSON_CONTENT_TYPE = "application/json; charset=utf-8"


class ScryfallResponder:
    """Writes Scryfall-shaped JSON, honoring the status an error object carries."""

    def _scryfall_respond(
        self,
        falcon_response: falcon.Response | None,
        payload: dict[str, Any],
        *,
        pretty: bool = False,
    ) -> dict[str, Any] | None:
        """Write a JSON payload, honoring the error status it carries and `pretty`.

        Args:
            falcon_response: The response to write to.
            payload: The Scryfall object to serialize.
            pretty: Whether to emit indented JSON.

        Returns:
            The payload when the caller should let the framework serialize it, or None when this
            method already wrote the body.
        """
        if falcon_response is not None:
            falcon_response.content_type = JSON_CONTENT_TYPE
            is_error = payload.get("object") == "error"
            status = payload.get("status") if is_error else None
            if isinstance(status, int):
                falcon_response.status = falcon.util.code_to_http_status(status)
            # AN ERROR BODY IS ALWAYS INDENTED, whatever `pretty` says. Not a style choice: it is
            # what api.scryfall.com does. Measured 2026-08-16 across the whole surface -- every
            # `object: "error"` body comes back two-space-indented while every data body comes back
            # compact, and it does not negotiate: `Accept: application/json`, `Accept: text/html`, a
            # bare wildcard and an explicit `?pretty=false` all produce the same 130-byte indented
            # not-found. Scryfall renders errors through a different serializer than answers, and
            # this rendered both compact, so a client comparing bytes saw a different document for
            # every 4xx it received.
            if pretty or is_error:
                falcon_response.text = orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
                return None
        return payload

    def _respond_text(self, falcon_response: falcon.Response | None, body: str, content_type: str) -> None:
        """Write a non-JSON body.

        Args:
            falcon_response: The response to write to.
            body: The rendered document.
            content_type: Its media type.
        """
        if falcon_response is not None:
            falcon_response.content_type = content_type
            falcon_response.text = body
