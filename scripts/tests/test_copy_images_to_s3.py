"""Tests for the copy_images_to_s3 script."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from scripts.copy_images_to_s3 import (
    CardProcessorPool,
    download_image,
    fetch_cards_from_db,
    get_args,
    get_s3_cards,
    get_s3_client_kwargs,
)

TEST_WASABI_ENDPOINT = "https://s3.us-east-1.wasabisys.com"


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


def test_get_args_accepts_s3_compatible_endpoint_options() -> None:
    """Test parsing S3-compatible endpoint CLI options."""
    with patch(
        "sys.argv",
        [
            "copy_images_to_s3.py",
            "--bucket",
            "wasabi-bucket",
            "--endpoint-url",
            TEST_WASABI_ENDPOINT,
            "--region-name",
            "us-east-1",
        ],
    ):
        args = get_args()

    assert args.bucket == "wasabi-bucket"
    assert args.endpoint_url == TEST_WASABI_ENDPOINT
    assert args.region_name == "us-east-1"


def test_get_s3_client_kwargs_only_includes_explicit_overrides() -> None:
    """Test boto3 kwargs only include configured endpoint overrides."""
    args = Mock(endpoint_url=TEST_WASABI_ENDPOINT, region_name="us-east-1")

    assert get_s3_client_kwargs(args) == {
        "endpoint_url": TEST_WASABI_ENDPOINT,
        "region_name": "us-east-1",
    }

    args = Mock(endpoint_url=None, region_name=None)

    assert get_s3_client_kwargs(args) == {}


def test_get_s3_cards_uses_s3_compatible_overrides() -> None:
    """Test listing existing images uses the configured S3-compatible endpoint."""
    args = Mock(
        bucket="biblioplex",
        endpoint_url=TEST_WASABI_ENDPOINT,
        region_name="us-east-1",
        skip_existing=True,
        set_code=None,
    )
    mock_bucket = Mock()
    mock_bucket.objects.filter.return_value = []
    mock_resource = Mock()
    mock_resource.Bucket.return_value = mock_bucket

    with patch("scripts.copy_images_to_s3.boto3.resource", return_value=mock_resource) as mock_resource_factory:
        result = get_s3_cards(args)

    assert result == set()
    mock_resource_factory.assert_called_once_with(
        "s3",
        endpoint_url=TEST_WASABI_ENDPOINT,
        region_name="us-east-1",
    )
    mock_resource.Bucket.assert_called_once_with("biblioplex")


def test_init_worker_uses_s3_compatible_overrides() -> None:
    """Test worker initialization passes custom S3-compatible kwargs to boto3."""
    with patch("scripts.copy_images_to_s3.boto3.client") as mock_client_factory:
        CardProcessorPool.init_worker(
            {
                "endpoint_url": TEST_WASABI_ENDPOINT,
                "region_name": "us-east-1",
            }
        )

    mock_client_factory.assert_called_once_with(
        "s3",
        endpoint_url=TEST_WASABI_ENDPOINT,
        region_name="us-east-1",
    )
    assert CardProcessorPool.s3_client is mock_client_factory.return_value
