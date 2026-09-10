"""Validate per-service environment scoping in docker-compose.yml."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

# Sentinel values used only during `docker compose config` rendering. Never log these in failure
# messages — assert on key names and cross-service presence instead.
_SENTINEL_ENV = {
    "ADMIN_PASSWORD": "SENTINEL_ADMIN_PASSWORD",
    "HONEYBADGER_API_KEY": "SENTINEL_HONEYBADGER_API_KEY",
    "XPGDATABASE": "SENTINEL_XPGDATABASE",
    "XPGPASSWORD": "SENTINEL_XPGPASSWORD",
    "XPGUSER": "SENTINEL_XPGUSER",
    "ENABLE_ENGINE": "true",
    "ENABLE_CACHE": "false",
    "ENVIRONMENT": "dev",
    "APP_ENV": "dev",
    "API_PORT": "28080",
    "POSTGRES_MEM_LIMIT": "3g",
    "HOSTNAME": "sentinel-host",
}

POSTGRES_ENV_KEYS = frozenset(
    {
        "PGDATA",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
    }
)

APISERVICE_ENV_KEYS = frozenset(
    {
        "ADMIN_PASSWORD",
        "CDN_URL",
        "CORS_ALLOWED_ORIGINS",
        "ENABLE_CACHE",
        "ENABLE_ENGINE",
        "ENVIRONMENT",
        "HONEYBADGER_API_KEY",
        "HOSTNAME",
        "PGDATABASE",
        "PGHOST",
        "PGPASSWORD",
        "PGUSER",
        "PREFER_SCORE_BACKFILL_TIMEOUT_MS",
        "PYTHONUNBUFFERED",
        "SHARED_CACHE_PATH",
    }
)

CLIENT_ENV_KEYS = frozenset(
    {
        "API_URL",
        "BATCH_SIZE",
        "CORPUS",
        "PYTHONUNBUFFERED",
        "QUERY_DELAY",
        "QUERY_MODE",
    }
)

# Keys that must never appear in a service's runtime environment block.
POSTGRES_FORBIDDEN_KEYS = frozenset(
    {
        "ADMIN_PASSWORD",
        "HONEYBADGER_API_KEY",
        "PGDATABASE",
        "PGHOST",
        "PGPASSWORD",
        "PGUSER",
        "XPGDATABASE",
        "XPGPASSWORD",
        "XPGUSER",
    }
)

CLIENT_FORBIDDEN_KEYS = frozenset(
    {
        "ADMIN_PASSWORD",
        "HONEYBADGER_API_KEY",
        "PGDATABASE",
        "PGHOST",
        "PGPASSWORD",
        "PGUSER",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "XPGDATABASE",
        "XPGPASSWORD",
        "XPGUSER",
    }
)

SECRET_VALUE_MARKERS = frozenset(
    {
        _SENTINEL_ENV["ADMIN_PASSWORD"],
        _SENTINEL_ENV["HONEYBADGER_API_KEY"],
        _SENTINEL_ENV["XPGDATABASE"],
        _SENTINEL_ENV["XPGPASSWORD"],
        _SENTINEL_ENV["XPGUSER"],
    }
)


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in sorted(values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _normalize_environment(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(key): "" if value is None else str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        env: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                msg = f"Unexpected environment entry: {item!r}"
                raise TypeError(msg)
            key, value = item.split("=", 1)
            env[key] = value
        return env
    msg = f"Unexpected environment type: {type(raw)!r}"
    raise TypeError(msg)


def _render_compose_config(extra_files: list[Path] | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="compose-env-test-") as tmpdir:
        tmp = Path(tmpdir)
        dot_env = tmp / ".env"
        dot_env_generated = tmp / ".env.generated"
        envs_dev = tmp / "envs-dev"

        _write_env_file(dot_env, _SENTINEL_ENV)
        _write_env_file(dot_env_generated, {"POSTGRES_MEM_LIMIT": _SENTINEL_ENV["POSTGRES_MEM_LIMIT"]})
        _write_env_file(
            envs_dev,
            {
                "APP_ENV": _SENTINEL_ENV["APP_ENV"],
                "API_PORT": _SENTINEL_ENV["API_PORT"],
                "ENABLE_CACHE": _SENTINEL_ENV["ENABLE_CACHE"],
                "ENABLE_ENGINE": _SENTINEL_ENV["ENABLE_ENGINE"],
            },
        )

        cmd = [
            "docker",
            "compose",
            "--project-name",
            "sylvan_compose_env_test",
            "--env-file",
            str(dot_env),
            "--env-file",
            str(dot_env_generated),
            "--env-file",
            str(envs_dev),
            "--file",
            str(COMPOSE_FILE),
        ]
        for extra in extra_files or []:
            cmd += ["--file", str(extra)]
        cmd += ["config", "--format", "json"]
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={**os.environ, "HOME": os.environ.get("HOME", str(tmp))},
        )
        if proc.returncode != 0:
            msg = proc.stderr.strip() or proc.stdout.strip() or "docker compose config failed"
            pytest.fail(msg)

        return json.loads(proc.stdout)


def _service_env(config: dict[str, Any], service: str) -> dict[str, str]:
    services = config.get("services")
    if not isinstance(services, dict):
        msg = "Compose config missing services mapping"
        raise AssertionError(msg)
    service_cfg = services.get(service)
    if not isinstance(service_cfg, dict):
        msg = f"Compose config missing service {service!r}"
        raise AssertionError(msg)
    return _normalize_environment(service_cfg.get("environment"))


def _assert_no_secret_values(env: dict[str, str], *, service: str) -> None:
    leaked = [key for key, value in env.items() if value in SECRET_VALUE_MARKERS]
    if leaked:
        msg = f"{service} received sentinel secret values via keys: {', '.join(sorted(leaked))}"
        raise AssertionError(msg)


def test_compose_services_have_no_env_file() -> None:
    """Runtime env_file injection must not broaden secret exposure."""
    config = _render_compose_config()
    services = config["services"]
    for name, service_cfg in services.items():
        assert "env_file" not in service_cfg, f"{name} still declares env_file"


def test_compose_postgres_env_is_explicit_and_scoped() -> None:
    config = _render_compose_config()
    env = _service_env(config, "postgres")

    assert set(env) == POSTGRES_ENV_KEYS
    assert not POSTGRES_FORBIDDEN_KEYS.intersection(env)
    assert env["POSTGRES_DB"] == _SENTINEL_ENV["XPGDATABASE"]
    assert env["POSTGRES_USER"] == _SENTINEL_ENV["XPGUSER"]
    assert env["POSTGRES_PASSWORD"] == _SENTINEL_ENV["XPGPASSWORD"]


def test_compose_apiservice_env_is_explicit_and_scoped() -> None:
    config = _render_compose_config()
    env = _service_env(config, "apiservice")

    assert set(env) == APISERVICE_ENV_KEYS
    assert env["ADMIN_PASSWORD"] == _SENTINEL_ENV["ADMIN_PASSWORD"]
    assert env["HONEYBADGER_API_KEY"] == _SENTINEL_ENV["HONEYBADGER_API_KEY"]
    assert env["PGDATABASE"] == _SENTINEL_ENV["XPGDATABASE"]
    assert env["PGUSER"] == _SENTINEL_ENV["XPGUSER"]
    assert env["PGPASSWORD"] == _SENTINEL_ENV["XPGPASSWORD"]
    assert env["PGHOST"] == "postgres"


def test_compose_client_env_has_no_secrets() -> None:
    config = _render_compose_config()
    env = _service_env(config, "client")

    assert set(env) == CLIENT_ENV_KEYS
    assert not CLIENT_FORBIDDEN_KEYS.intersection(env)
    _assert_no_secret_values(env, service="client")
