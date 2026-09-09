"""Tests for the copy_images_to_s3 script."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from scripts.copy_images_to_s3 import (
    download_image,
    fetch_cards_from_db,
    list_image_prefixes,
    list_prefix_keys,
    make_listing_session,
    parse_image_key,
)


def test_download_image_success() -> None:
    """Test successful image download."""
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / "test.png"

        # Mock requests.get
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.iter_content = Mock(return_value=[b"chunk1", b"chunk2"])

        with patch("scripts.copy_images_to_s3.requests.get", return_value=mock_response):
            result = download_image("https://example.com/image.png", output_path)

        assert result is True
        assert output_path.exists()

        # Check content was written
        content = output_path.read_bytes()
        assert content == b"chunk1chunk2"


def test_download_image_failure() -> None:
    """Test failed image download."""
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / "test.png"

        # Mock requests.get to raise a RequestException
        with patch("scripts.copy_images_to_s3.requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("Network error")
            result = download_image("https://example.com/image.png", output_path)

        assert result is False
        assert not output_path.exists()


def test_fetch_cards_from_db() -> None:
    """Test fetching cards from database."""
    # Mock connection and cursor
    mock_conn = Mock()
    mock_cursor = Mock()
    mock_conn.cursor.return_value.__enter__ = Mock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = Mock(return_value=False)

    # Mock query results
    mock_cursor.fetchall.return_value = [
        {
            "card_set_code": "iko",
            "collector_number": "123",
            "image_location_uuid": "a7af8350-9a51-437c-a55e-19f3e07acfa9",
        },
        {
            "card_set_code": "thb",
            "collector_number": "42a",
            "image_location_uuid": "b8bf9461-0b62-548d-b66f-20g4f08bdbga",
        },
    ]

    cards = fetch_cards_from_db(mock_conn, limit=10, set_code="iko")

    assert len(cards) == 2
    assert cards[0]["card_set_code"] == "iko"
    assert cards[0]["collector_number"] == "123"
    assert cards[1]["card_set_code"] == "thb"


def test_parse_image_key_extracts_all_four_parts() -> None:
    """A well-formed image key yields set, collector number, face and size."""
    assert parse_image_key("img/iko/1/1/280.webp") == ("iko", "1", "1", "280")
    # Collector numbers are not always plain integers.
    assert parse_image_key("img/oarc/10\u2605/2/745.webp") == ("oarc", "10\u2605", "2", "745")


def test_parse_image_key_rejects_non_webp() -> None:
    """Keys that are not WebP images are ignored."""
    assert parse_image_key("img/iko/1/1/280.png") is None
    assert parse_image_key("img/iko/1/1/original.jpg") is None


def test_parse_image_key_rejects_wrong_depth() -> None:
    """Keys that do not match the four-part layout are ignored."""
    # Pre-face layout, missing the face segment.
    assert parse_image_key("img/iko/1/280.webp") is None
    # An extra segment.
    assert parse_image_key("img/iko/1/1/extra/280.webp") is None


def test_make_listing_session_skips_timestamp_parsing() -> None:
    """The listing session leaves LastModified as the raw string S3 sent.

    Parsing it costs about two thirds of the CPU in a large listing, and no
    caller reads the value.
    """
    raw = "2026-01-01T00:00:00.000Z"
    parser = make_listing_session().get_component("response_parser_factory").create_parser("rest-xml")
    assert parser._timestamp_parser(raw) == raw


def test_list_prefix_keys_pages_and_skips_unparseable() -> None:
    """Every page is consumed, and keys outside the image layout are dropped."""
    pages = [
        {"Contents": [{"Key": "img/iko/1/1/280.webp"}, {"Key": "img/iko/1/1/388.webp"}]},
        {"Contents": [{"Key": "img/iko/2/1/280.webp"}, {"Key": "img/iko/index.html"}]},
        {},
    ]
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = pages

    found = list_prefix_keys(client, "bucket", "img/iko/")

    assert found == {("iko", "1", "1", "280"), ("iko", "1", "1", "388"), ("iko", "2", "1", "280")}
    client.get_paginator.assert_called_once_with("list_objects_v2")


def test_list_image_prefixes_short_circuits_for_one_set() -> None:
    """Naming a single set needs no prefix discovery request."""
    client = Mock()

    assert list_image_prefixes(client, "bucket", "iko") == ["img/iko/"]
    client.get_paginator.assert_not_called()


def test_list_image_prefixes_collects_common_prefixes() -> None:
    """Without a set filter, prefixes come from a delimited listing."""
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = [
        {"CommonPrefixes": [{"Prefix": "img/iko/"}, {"Prefix": "img/akh/"}]},
        {"CommonPrefixes": [{"Prefix": "img/plst/"}]},
    ]

    assert list_image_prefixes(client, "bucket", None) == ["img/iko/", "img/akh/", "img/plst/"]
