"""The host static-file bind mount is a dev-only override, never part of the base compose file."""

from __future__ import annotations

from scripts.tests.test_compose_service_env import REPO_ROOT, _render_compose_config

DEV_COMPOSE_FILE = REPO_ROOT / "docker-compose.dev.yml"
STATIC_TARGET = "/app/api/static"


def _apiservice_bind_targets(config: dict) -> set[str]:
    volumes = config["services"]["apiservice"].get("volumes") or []
    return {v["target"] for v in volumes if v.get("type") == "bind"}


def test_base_compose_does_not_mount_host_static_files() -> None:
    """Blue and green must serve the static files built into their own image."""
    config = _render_compose_config()
    assert STATIC_TARGET not in _apiservice_bind_targets(config)


def test_dev_override_mounts_host_static_files() -> None:
    """The dev stack layers the override so JS edits show up without a rebuild."""
    config = _render_compose_config(extra_files=[DEV_COMPOSE_FILE])
    assert STATIC_TARGET in _apiservice_bind_targets(config)


def test_dev_override_touches_only_the_static_mount() -> None:
    """Keep the override to one concern so it cannot drift from the base file unnoticed."""
    text = DEV_COMPOSE_FILE.read_text(encoding="utf-8")
    body = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert body == [
        "services:",
        "  apiservice:",
        "    volumes:",
        "      - ./api/static:/app/api/static",
    ]


def test_makefile_only_layers_dev_override_for_dev() -> None:
    """Every per-environment compose invocation goes through compose_files, which gates on dev."""
    makefile = (REPO_ROOT / "makefile").read_text(encoding="utf-8")
    assert "compose_files = --file $(BASE_COMPOSE)$(if $(filter dev,$(1)), --file $(DEV_COMPOSE))" in makefile
    for line in makefile.splitlines():
        if "--env-file envs/$*" in line or "--env-file envs/$(env)" in line:
            assert "$(call compose_files," in line, line
    for line in makefile.splitlines():
        if "--env-file envs/blue" in line or "--env-file envs/green" in line:
            assert "DEV_COMPOSE" not in line, line


def test_services_restart_and_rotate_logs() -> None:
    """Both long-running services restart on their own and cap their json-file logs."""
    config = _render_compose_config()
    for name in ("postgres", "apiservice"):
        service = config["services"][name]
        assert service.get("restart") == "unless-stopped", name
        logging = service.get("logging") or {}
        assert logging.get("driver") == "json-file", name
        assert logging.get("options") == {"max-size": "50m", "max-file": "5"}, name


def test_apiservice_healthcheck_uses_ready_and_graceful_stop() -> None:
    """The healthcheck holds `up --wait` until /ready; SIGTERM gets more than the 10s default."""
    config = _render_compose_config()
    service = config["services"]["apiservice"]
    assert "localhost:8080/ready" in service["healthcheck"]["test"]
    assert service.get("stop_grace_period") == "15s"
