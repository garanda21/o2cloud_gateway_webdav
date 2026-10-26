"""The shipped docker-compose.yml must forward settings from .env (#24, #26).

Compose only passes to the container what is listed under `environment:`; .env
is merely the source for `${NAME:-default}` interpolation (or the file given
with `docker compose --env-file`). These tests resolve the real compose file the
same way and check what the app's Settings() ends up with.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from o2gateway.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
INTERPOLATION = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)(?::-(?P<default>[^}]*))?\}")

# .env.example variables that are deliberately NOT forwarded from .env.
NOT_FORWARDED = {
    # Credentials come from the mounted secret files, never from .env.
    "WEBDAV_PASSWORD", "ADMIN_PASSWORD",
    "WEBDAV_PASSWORD_FILE", "ADMIN_PASSWORD_FILE", "ADMIN_SESSION_SECRET_FILE", "TELEGRAM_BOT_TOKEN_FILE",
    "APP_ENCRYPTION_KEY_FILE",
    # Paths that must match the volumes mounted by this compose file.
    "CONFIG_DIR", "CACHE_DIR", "DATA_DIR", "SQLITE_PATH", "SIMULATED_ROOT",
    "UPLOAD_SPOOL_DIR", "LOG_FILE", "O2_SESSION_FILE",
    # Ports and bind address must match the published ports.
    "APP_HOST", "APP_PORT",
    # Provider endpoints and the provider itself (.env.example says `simulated`).
    "CLOUD_PROVIDER", "O2_API_BASE_URL", "O2_UPLOAD_BASE_URL", "O2_LOGIN_URL",
}


def _environment() -> dict[str, str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["o2cloud-webdav"]
    assert "env_file" not in service, "a mandatory env_file breaks deployments without .env"
    return {key: str(value) for key, value in service["environment"].items()}


def _env_example() -> dict[str, str]:
    text = (ROOT / ".env.example").read_text()
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.M))


def _resolve(dotenv: dict[str, str]) -> dict[str, str]:
    """What the container receives, given the interpolation source (.env)."""

    def substitute(match: re.Match) -> str:
        value = dotenv.get(match["name"], "")
        return value if value else (match["default"] or "")

    return {key: INTERPOLATION.sub(substitute, value) for key, value in _environment().items()}


def _settings(container_env: dict[str, str], monkeypatch) -> Settings:
    for key in Settings.model_fields:
        monkeypatch.delenv(key.upper(), raising=False)
    for key, value in container_env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_runs_without_a_dotenv_file_with_the_same_values_as_before(monkeypatch):
    settings = _settings(_resolve({}), monkeypatch)

    assert settings.cloud_provider == "o2"
    assert settings.app_base_url == "http://localhost:8088"
    assert settings.upload_max_file_mb == 10240
    assert settings.cache_max_size_mb == 4096
    assert settings.upload_recent_cache_max_file_mb == 256
    assert settings.o2_proactive_reauth_seconds == 0  # O2 default kept: the variable is not set


def test_every_env_example_variable_is_forwarded_or_deliberately_excluded():
    forwarded = {match["name"] for value in _environment().values() for match in INTERPOLATION.finditer(value)}
    unhandled = set(_env_example()) - forwarded - NOT_FORWARDED

    assert unhandled == set(), "forward these in docker-compose.yml or list them in NOT_FORWARDED"


def test_copying_env_example_changes_nothing(monkeypatch):
    without = _settings(_resolve({}), monkeypatch).model_dump()
    with_example = _settings(_resolve(_env_example()), monkeypatch).model_dump()

    assert with_example == without


def test_custom_cache_settings_reach_the_app(monkeypatch):
    dotenv = {"CACHE_MAX_SIZE_MB": "512", "UPLOAD_RECENT_CACHE_MAX_FILE_MB": "0", "WEBDAV_READ_ONLY": "true"}

    settings = _settings(_resolve(dotenv), monkeypatch)

    assert settings.cache_max_size_mb == 512
    assert settings.upload_recent_cache_max_file_mb == 0
    assert settings.webdav_read_only is True


def test_wiring_and_credentials_are_not_taken_from_dotenv(monkeypatch):
    dotenv = {
        "CLOUD_PROVIDER": "movistar",
        "APP_PORT": "9999",
        "SQLITE_PATH": "/elsewhere.db",
        "WEBDAV_PASSWORD": "from-dotenv",
        "ADMIN_PASSWORD": "from-dotenv",
    }

    container_env = _resolve(dotenv)
    settings = _settings(container_env, monkeypatch)

    assert settings.cloud_provider == "o2"
    assert settings.app_port == 8080
    assert settings.sqlite_path == "/data/o2cloud-gateway.db"
    assert "WEBDAV_PASSWORD" not in container_env and "ADMIN_PASSWORD" not in container_env
    assert settings.webdav_password is None and settings.admin_password is None  # secret files apply


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker compose not installed")
def test_real_compose_resolution(tmp_path):
    """Optional: check the same rules with the real `docker compose config`."""
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("docker compose plugin not available")
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(ROOT / "docker-compose.yml", project / "docker-compose.yml")

    def config(*args: str) -> dict[str, str]:
        out = subprocess.run(
            ["docker", "compose", *args, "config", "--format", "json"],
            cwd=project, capture_output=True, text=True, check=True,
        ).stdout
        return json.loads(out)["services"]["o2cloud-webdav"]["environment"]

    assert config()["CACHE_MAX_SIZE_MB"] == "4096"  # no .env at all
    (project / ".env").write_text("CACHE_MAX_SIZE_MB=512\n")
    assert config()["CACHE_MAX_SIZE_MB"] == "512"
    (project / "production.env").write_text("CACHE_MAX_SIZE_MB=123\nCLOUD_PROVIDER=movistar\n")
    selected = config("--env-file", "production.env")
    assert selected["CACHE_MAX_SIZE_MB"] == "123"  # the selected file wins over .env
    assert selected["CLOUD_PROVIDER"] == "o2"  # wiring stays fixed
