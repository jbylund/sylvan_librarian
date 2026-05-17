#!/usr/bin/env python3
"""Compare file sizes: uncompressed, compressed, minified, and minified+compressed.

This script compares the size of CSS and JavaScript files under different conditions:
1. Original (uncompressed)
2. Compressed with zstd (level 4, matching compression middleware)
3. Minified
4. Minified + compressed with zstd

Usage:
    python scripts/compare_minification.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import zstandard as zstd


def get_file_size(filepath: Path) -> int:
    """Get file size in bytes."""
    return filepath.stat().st_size


def compress_with_zstd(data: bytes, level: int = 4) -> bytes:
    """Compress data with zstd at the specified level."""
    cctx = zstd.ZstdCompressor(level=level)
    return cctx.compress(data)


def minify_css(input_path: Path, output_path: Path) -> None:
    """Minify CSS file using postcss-cli with cssnano."""
    try:
        subprocess.run(
            ["npx", "postcss", str(input_path), "--use", "cssnano", "--output", str(output_path)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Error minifying CSS: {e.stderr.decode()}", file=sys.stderr)
        raise


def minify_js(input_path: Path, output_path: Path) -> None:
    """Minify JavaScript file using terser."""
    try:
        subprocess.run(
            [
                "npx",
                "terser",
                str(input_path),
                "--compress",
                "--mangle",
                "--output",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Error minifying JS: {e.stderr.decode()}", file=sys.stderr)
        raise


KIBI = 2**10


def format_size(size_bytes: int) -> str:
    """Format size in human-readable format."""
    for unit in ["B", "KB", "MB"]:
        if size_bytes < KIBI:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= KIBI
    return f"{size_bytes:.1f} GB"


def format_percentage(original: int, compressed: int) -> str:
    """Format compression ratio as percentage."""
    if original == 0:
        return "0.0%"
    ratio = (1 - compressed / original) * 100
    return f"{ratio:.1f}%"


def compare_file(filepath: Path, file_type: str) -> None:
    """Compare a single file under different conditions."""
    print(f"\n{'=' * 70}")
    print(f"File: {filepath}")
    print(f"Type: {file_type.upper()}")
    print(f"{'=' * 70}")

    # Read original file
    original_data = filepath.read_bytes()
    original_size = len(original_data)

    # 1. Original (uncompressed)
    print("\n1. Original (uncompressed):")
    print(f"   Size: {format_size(original_size)} ({original_size:,} bytes)")

    # 2. Compressed with zstd
    compressed_data = compress_with_zstd(original_data, level=4)
    compressed_size = len(compressed_data)
    print("\n2. Compressed (zstd level 4):")
    print(f"   Size: {format_size(compressed_size)} ({compressed_size:,} bytes)")
    print(f"   Reduction: {format_percentage(original_size, compressed_size)}")

    # 3. Minified
    temp_dir = Path("/tmp")  # noqa: S108
    minified_path = temp_dir / f"{filepath.stem}.min{filepath.suffix}"
    try:
        if file_type == "css":
            minify_css(filepath, minified_path)
        else:  # js
            minify_js(filepath, minified_path)

        minified_data = minified_path.read_bytes()
        minified_size = len(minified_data)
        print("\n3. Minified:")
        print(f"   Size: {format_size(minified_size)} ({minified_size:,} bytes)")
        print(f"   Reduction: {format_percentage(original_size, minified_size)}")

        # 4. Minified + compressed
        minified_compressed_data = compress_with_zstd(minified_data, level=4)
        minified_compressed_size = len(minified_compressed_data)
        print("\n4. Minified + Compressed (zstd level 4):")
        print(f"   Size: {format_size(minified_compressed_size)} ({minified_compressed_size:,} bytes)")
        print(f"   Reduction vs original: {format_percentage(original_size, minified_compressed_size)}")
        print(f"   Reduction vs compressed: {format_percentage(compressed_size, minified_compressed_size)}")
        print(f"   Reduction vs minified: {format_percentage(minified_size, minified_compressed_size)}")

    finally:
        # Cleanup temporary file
        try:
            minified_path.unlink()
        except OSError as e:
            print(f"Warning: Failed to cleanup temporary file {minified_path}: {e}", file=sys.stderr)


def main() -> None:
    """Main entry point."""
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    static_dir = project_root / "api" / "static"

    css_file = static_dir / "styles.css"
    js_file = static_dir / "app.js"

    if not css_file.exists():
        print(f"Error: {css_file} not found", file=sys.stderr)
        sys.exit(1)

    if not js_file.exists():
        print(f"Error: {js_file} not found", file=sys.stderr)
        sys.exit(1)

    print("Minification Comparison Tool")
    print("=" * 70)
    print("Comparing file sizes under different compression scenarios")
    print("Using zstd compression level 4 (matching compression middleware)")

    compare_file(css_file, "css")
    compare_file(js_file, "js")

    print(f"\n{'=' * 70}")
    print("Summary:")
    print("  - Original: Uncompressed file size")
    print("  - Compressed: Original file compressed with zstd (level 4)")
    print("  - Minified: File minified with postcss-cli + cssnano (CSS) or terser (JS)")
    print("  - Minified + Compressed: Minified file then compressed with zstd")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
