"""The tracked Postgres config is what actually runs: hba_file points at the mount, and the costly knobs are bounded."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "configs/postgres/conf/postgresql.conf.template"
PG_HBA = REPO_ROOT / "configs/postgres/conf/pg_hba.conf"
COMPOSE = REPO_ROOT / "docker-compose.yml"


def _setting(name: str) -> str:
    """The last uncommented assignment wins, as it does for postgres itself."""
    values = re.findall(rf"^{re.escape(name)}\s*=\s*([^\s#]+)", TEMPLATE.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert values, f"{name} is not set in the template"
    return values[-1]


def test_hba_file_points_at_the_compose_mount() -> None:
    assert _setting("hba_file") == "'/pg_hba.conf'"
    assert "./configs/postgres/conf/pg_hba.conf:/pg_hba.conf" in COMPOSE.read_text(encoding="utf-8")


def test_pg_hba_uses_scram_not_md5() -> None:
    rules = [line for line in PG_HBA.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
    methods = {line.split()[-1] for line in rules}
    assert "md5" not in methods
    # Remote (container-network) connections for both address families, as initdb's own file has.
    assert any(rule.split()[3:5] == ["0.0.0.0/0", "scram-sha-256"] for rule in rules)
    assert any(rule.split()[3:5] == ["::/0", "scram-sha-256"] for rule in rules)


def test_auto_explain_is_sampled_without_per_node_timing() -> None:
    assert float(_setting("auto_explain.sample_rate")) <= 0.1
    assert _setting("auto_explain.log_timing") == "off"
    assert _setting("auto_explain.log_analyze") == "on"


def test_statistics_target_and_wal_size_are_bounded() -> None:
    assert int(_setting("default_statistics_target")) == 500
    assert _setting("max_wal_size") == "2GB"
