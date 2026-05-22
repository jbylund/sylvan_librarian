"""Compression middleware for Falcon API responses."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from api.middlewares.compression.compressors import BrotliCompressor, GzipCompressor, ZstdCompressor
from api.middlewares.timing import record_span

if TYPE_CHECKING:
    import falcon

MIN_SIZE: int = 200

logger = logging.getLogger(__name__)


def parse_q_list(
    accept_encoding: str,
    server_priorities: dict[str, int],
) -> list[str]:
    """Parse the Accept-Encoding header and return a list of encodings sorted by client and server priority.

    Args:
        accept_encoding (str): The Accept-Encoding header value.
        server_priorities (dict[str, int]): Mapping of encoding names to server priorities.

    Returns:
        list[str]: List of encoding names sorted by priority.
    """
    # TODO: add client priority


class CompressionMiddleware:
    """Middleware for handling response compression using various algorithms."""

    def __init__(self: CompressionMiddleware) -> None:
        """Initialize the CompressionMiddleware and register available compressors."""
        self._compressors: dict[str, object] = {}
        self._add_compressor(BrotliCompressor())
        self._add_compressor(GzipCompressor())
        self._add_compressor(ZstdCompressor())

    def _add_compressor(self: CompressionMiddleware, compressor: object) -> None:
        """Register a compressor and its priority.

        Args:
            compressor: Compressor instance with 'encoding' and 'priority' attributes.
        """
        self._compressors[compressor.encoding] = compressor

    def _get_compressor(self: CompressionMiddleware, accept_encoding: str) -> object | None:
        """Select a compressor based on the Accept-Encoding header.

        Args:
            accept_encoding (str): The Accept-Encoding header value.

        Returns:
            object | None: The selected compressor or None if not found.
        """
        accept_encoding_header = accept_encoding
        # accept encoding looks like:
        # Accept-Encoding: br;q=1.0, gzip;q=0.8, *;q=0.1
        compressor_candidates = []
        for accept_encoding_item in accept_encoding_header.split(","):
            name, _, _ = accept_encoding_item.partition(";")
            name = name.strip().lower()
            compressor = self._compressors.get(name)
            if compressor is None:
                continue
            compressor_candidates.append(compressor)
        compressor = min(compressor_candidates, key=lambda v: v.priority) if compressor_candidates else None
        logger.info(
            "Server priorities: %s / Accept encoding: %s / Selected compressor: %s",
            {k: v.priority for k, v in self._compressors.items()},
            accept_encoding_header,
            compressor.encoding,
        )
        return compressor

    def process_response(
        self: CompressionMiddleware,
        req: falcon.Request,
        resp: falcon.Response,
        resource: object,
        req_succeeded: bool,
    ) -> None:
        """Post-processing of the response (after routing).

        Args:
            req: Request object.
            resp: Response object.
            resource: Resource object to which the request was routed. May be None if no route was found for the request.
            req_succeeded (bool): True if no exceptions were raised while the framework processed and routed the request; otherwise False.
        """
        del resource, req_succeeded
        if resp.complete:
            logger.debug("Will serve response from cache...")
            return
        accept_encoding = req.get_header("Accept-Encoding")
        if accept_encoding is None:
            return

        # If content-encoding is already set don't compress.
        if resp.get_header("Content-Encoding"):
            return

        # my accept encoding is "gzip, deflate, br, zstd"
        compressor = self._get_compressor(accept_encoding)
        if compressor is None:
            return

        if resp.stream:
            logger.info("Compressing stream")
            resp.stream = compressor.compress_stream(resp.stream)
            resp.content_length = None
        else:
            data = resp.render_body()
            # If there is no content or it is very short then don't compress.
            if data is None or len(data) < MIN_SIZE:
                logger.info("Skipping compression for short response")
                return
            size_before_compression = len(data)
            before_compression = time.monotonic()
            resp.data = compressed = compressor.compress(data)
            after_compression = time.monotonic()
            resp.text = None
            size_after_compression = len(compressed)
            compress_ms = 1000 * (after_compression - before_compression)
            logger.info(
                "%s: Compressed %s bytes to %s bytes using %s (%.2f x compression) in %.2f ms - %s",
                req.url,
                f"{size_before_compression:,}",
                f"{size_after_compression:,}",
                compressor.encoding,
                size_before_compression / size_after_compression,
                compress_ms,
                req.get_header("User-Agent"),
            )
            record_span(req, "compress", compress_ms)

        resp.set_header("Content-Encoding", compressor.encoding)
        resp.append_header("Vary", "Accept-Encoding")
