"""Tests for the compare_minification script."""

from pathlib import Path
from unittest.mock import patch

from scripts.compare_minification import minify_css


def test_minify_css_uses_postcss_cli_with_cssnano(tmp_path: Path) -> None:
    """Test CSS minification invokes postcss-cli with cssnano."""
    input_path = tmp_path / "input.css"
    output_path = tmp_path / "output.css"

    with patch("scripts.compare_minification.subprocess.run") as mock_run:
        minify_css(input_path, output_path)

    mock_run.assert_called_once_with(
        ["npx", "postcss", str(input_path), "--use", "cssnano", "--output", str(output_path)],
        check=True,
        capture_output=True,
    )
