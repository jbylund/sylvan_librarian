"""Guard the ordering of the rolling deploy and the read-only prettier target."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "makefile"


def _recipe(target: str) -> list[str]:
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", text, flags=re.MULTILINE)
    assert match is not None, f"no recipe for {target!r}"
    return match.group(1).splitlines()


def test_rolling_deploy_smoke_checks_blue_before_touching_green() -> None:
    """A blue that is 'healthy' but cannot answer /ready or /search must not take green down too."""
    lines = _recipe("rolling-deploy")
    blue_up = next(i for i, line in enumerate(lines) if "sylvan_blue" in line and " up " in line)
    blue_smoke = next(i for i, line in enumerate(lines) if "smoke_check,blue" in line)
    green_up = next(i for i, line in enumerate(lines) if "sylvan_green" in line and " up " in line)
    green_smoke = next(i for i, line in enumerate(lines) if "smoke_check,green" in line)
    assert blue_up < blue_smoke < green_up < green_smoke


def test_rolling_deploy_pulls_and_smoke_check_hits_ready_and_search() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^rolling-deploy: [^\n]*\bpull_images\b", text, flags=re.MULTILINE)
    assert "pull postgres" in "\n".join(_recipe("pull_images"))
    assert "true ||" not in "\n".join(_recipe("pull_images"))
    smoke = text[text.index("define smoke_check") : text.index("endef", text.index("define smoke_check"))]
    assert "for path in ready 'search?q=t:elf'" in smoke
    assert "curl --fail" in smoke
    assert "envs/$(1)" in smoke


def test_prettier_check_is_read_only() -> None:
    assert "npx prettier --check $(html_files) $(js_files)" in "\n".join(_recipe("prettier_check"))
    assert "--write" not in "\n".join(_recipe("prettier_check"))
