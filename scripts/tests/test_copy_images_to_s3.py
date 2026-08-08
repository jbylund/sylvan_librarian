"""Tests for the copy_images_to_s3 script."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from scripts.copy_images_to_s3 import (
    download_image,
    fetch_cards_from_db,
    get_db_cards,
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


def test_get_db_cards_emits_back_face_images() -> None:
    """A card with a physical back face yields face-2 images alongside the front's.

    The back image feeds the site's flip button; single-faced cards (and split/adventure
    faces, which carry no per-face images) must stay front-only.
    """
    rows = [
        {"card_set_code": "mid", "collector_number": "5", "png_url": "https://x/front.png", "back_png_url": "https://x/back.png"},
        {"card_set_code": "m21", "collector_number": "1", "png_url": "https://x/solo.png", "back_png_url": None},
    ]
    args = Mock(limit=None, set_code=None)
    with (
        patch("scripts.copy_images_to_s3.get_database_connection"),
        patch("scripts.copy_images_to_s3.fetch_cards_from_db", return_value=rows),
    ):
        images = get_db_cards(args)

    keys = {image.get_s3_key() for image in images}
    assert "img/mid/5/1/388.webp" in keys
    assert "img/mid/5/2/388.webp" in keys
    assert "img/m21/1/1/388.webp" in keys
    assert not any("/m21/1/2/" in key for key in keys)
    assert len(images) == 12  # (front+back) * 4 sizes + front-only * 4 sizes


def test_get_db_cards_back_face_carries_its_own_png_url() -> None:
    """The face-2 image downloads the back PNG, not the front's."""
    rows = [
        {"card_set_code": "mid", "collector_number": "5", "png_url": "https://x/front.png", "back_png_url": "https://x/back.png"},
    ]
    args = Mock(limit=None, set_code=None)
    with (
        patch("scripts.copy_images_to_s3.get_database_connection"),
        patch("scripts.copy_images_to_s3.fetch_cards_from_db", return_value=rows),
    ):
        images = get_db_cards(args)

    by_face = {image.face_idx: image.png_url for image in images}
    assert by_face == {"1": "https://x/front.png", "2": "https://x/back.png"}
