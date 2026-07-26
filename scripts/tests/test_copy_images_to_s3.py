"""Tests for the copy_images_to_s3 script."""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from scripts.copy_images_to_s3 import (
    SCRYFALL_USER_AGENT,
    download_image,
    fetch_cards_from_db,
    get_session,
)

# Verdicts the forked child writes back through a pipe. The child cannot assert -- a failure
# there would not propagate to the test run -- so it reports and the parent asserts.
CHILD_BUILT_OWN_SESSION = b"fresh"
CHILD_REUSED_PARENT_SESSION = b"reused"
VERDICT_MAX_BYTES = 16  # comfortably longer than either verdict, so a truncated read cannot pass


def test_download_image_success() -> None:
    """Test successful image download."""
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / "test.png"

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.iter_content = Mock(return_value=[b"chunk1", b"chunk2"])

        # spec'd so the mock only answers calls a real Session would accept
        mock_session = Mock(spec=requests.Session)
        mock_session.get = Mock(return_value=mock_response)

        with patch("scripts.copy_images_to_s3.get_session", return_value=mock_session):
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

        mock_session = Mock(spec=requests.Session)
        mock_session.get = Mock(side_effect=requests.RequestException("Network error"))

        with patch("scripts.copy_images_to_s3.get_session", return_value=mock_session):
            result = download_image("https://example.com/image.png", output_path)

        assert result is False
        assert not output_path.exists()


def test_get_session_identifies_itself_to_scryfall() -> None:
    """The session carries the descriptive User-Agent Scryfall requires."""
    session = get_session(os.getpid())

    assert session.headers["User-Agent"] == SCRYFALL_USER_AGENT


def test_get_session_is_reused_within_a_process() -> None:
    """Repeated calls with the same pid hand back one session, so connections are pooled."""
    assert get_session(os.getpid()) is get_session(os.getpid())


def test_get_session_holds_only_the_current_pid() -> None:
    """maxsize=1: a new pid evicts the old entry rather than accumulating sessions."""
    get_session.cache_clear()

    first = get_session(1)
    second = get_session(2)

    assert second is not first
    assert get_session.cache_info().currsize == 1


# pytest itself is multi-threaded, so fork() warns. Safe here because the child only builds a
# Session and writes to a pipe -- it never touches a lock another thread could have held.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_get_session_is_rebuilt_after_fork() -> None:
    """A forked child builds its own session instead of sharing the parent's open sockets."""
    get_session.cache_clear()
    parent_session = get_session(os.getpid())

    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        # Child: report a verdict and exit without unwinding back into pytest.
        os.close(read_fd)
        try:
            reused = get_session(os.getpid()) is parent_session
            os.write(write_fd, CHILD_REUSED_PARENT_SESSION if reused else CHILD_BUILT_OWN_SESSION)
        finally:
            os._exit(0)

    os.close(write_fd)
    verdict = os.read(read_fd, VERDICT_MAX_BYTES)
    os.close(read_fd)
    _, wait_status = os.waitpid(child_pid, 0)

    assert wait_status == 0
    assert verdict == CHILD_BUILT_OWN_SESSION


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
