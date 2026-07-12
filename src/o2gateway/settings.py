from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

O2_API_BASE_URL = "https://cloud.o2online.es/sapi/"
O2_UPLOAD_BASE_URL = "https://upload.cloud.o2online.es/sapi/"
O2_LOGIN_URL = "https://cloud.o2online.es/"

MOVISTAR_API_BASE_URL = "https://micloud.movistar.es/sapi/"
MOVISTAR_UPLOAD_BASE_URL = "https://upload.micloud.movistar.es/sapi/"
MOVISTAR_LOGIN_URL = "https://micloud.movistar.es/"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "0.0.0.0"
    app_port: int = 8080
    app_base_url: str = "http://localhost:8080"
    app_encryption_key_file: Optional[str] = None

    cloud_provider: str = "simulated"
    simulated_root: str = "/data/simulated"

    o2_api_base_url: str = O2_API_BASE_URL
    o2_upload_base_url: str = O2_UPLOAD_BASE_URL
    o2_login_url: str = O2_LOGIN_URL
    o2_session_file: str = "/config/secrets/o2-session.json"
    o2_playwright_headless: bool = False
    o2_session_keepalive_seconds: int = 300
    o2_login_novnc_url: Optional[str] = None
    novnc_port: int = 6080
    novnc_path: str = "/vnc.html?autoconnect=true&resize=scale&reconnect=true"

    webdav_enabled: bool = True
    webdav_path_base: str = "/dav"
    webdav_username: str = "o2dav"
    webdav_password: Optional[str] = None
    webdav_password_file: Optional[str] = "/run/secrets/webdav_password"
    webdav_read_only: bool = False
    webdav_allow_dotfiles: bool = True
    webdav_depth_infinity: bool = False
    webdav_ignore_appledouble: bool = True

    admin_enabled: bool = True
    admin_path_base: str = "/admin"
    admin_username: str = "admin"
    admin_password: Optional[str] = None
    admin_password_file: Optional[str] = "/run/secrets/admin_password"
    admin_session_secret_file: Optional[str] = "/run/secrets/app_encryption_key"

    config_dir: str = "/config"
    cache_dir: str = "/cache"
    data_dir: str = "/data"
    sqlite_path: str = "/data/o2cloud-gateway.db"

    cache_metadata_ttl_seconds: int = 20
    cache_negative_ttl_seconds: int = 5
    cache_max_size_mb: int = 4096

    upload_max_file_mb: int = 10240
    upload_spool_dir: str = "/cache/uploads"
    # TTL de seguridad de las entradas del overlay local (placeholders de Finder y
    # subidas recientes); normalmente se retiran antes, cuando el listado remoto
    # las refleja. El listado de Movistar puede tardar >60s en ponerse al día.
    upload_recent_cache_ttl_seconds: int = 900
    upload_recent_cache_max_file_mb: int = 256
    # Presupuesto de reintentos para operaciones que chocan con la ventana de
    # validación del proveedor (MED-1017, ~4-10s tras subir).
    upload_overwrite_retry_seconds: float = 20.0
    # Vida máxima del tombstone tras un DELETE y presupuesto del borrado remoto
    # diferido; también cubre la latencia del listado remoto tras borrar.
    delete_tombstone_ttl_seconds: int = 300

    download_timeout_seconds: int = 3600
    o2_http_timeout_seconds: int = 120

    log_level: str = Field(default="INFO")
    log_file: str = "/data/logs/gateway.log"

    @model_validator(mode="before")
    @classmethod
    def apply_provider_defaults(cls, values):
        if not isinstance(values, dict):
            return values
        normalized = {str(key).lower(): key for key in values}
        provider_key = normalized.get("cloud_provider")
        provider = str(values.get(provider_key, "")).lower() if provider_key else ""
        if provider != "movistar":
            return values
        defaults = {
            "o2_api_base_url": (O2_API_BASE_URL, MOVISTAR_API_BASE_URL),
            "o2_upload_base_url": (O2_UPLOAD_BASE_URL, MOVISTAR_UPLOAD_BASE_URL),
            "o2_login_url": (O2_LOGIN_URL, MOVISTAR_LOGIN_URL),
        }
        for field_name, (o2_default, provider_default) in defaults.items():
            current_key = normalized.get(field_name)
            if current_key is None or values.get(current_key) == o2_default:
                values[field_name] = provider_default
        return values

    def normalized_webdav_base(self) -> str:
        return normalize_base(self.webdav_path_base)

    def normalized_admin_base(self) -> str:
        return normalize_base(self.admin_path_base)

    def provider_label(self) -> str:
        provider = self.cloud_provider.lower()
        if provider == "movistar":
            return "Movistar Cloud"
        if provider == "o2":
            return "O2 Cloud"
        if provider == "simulated":
            return "Simulated"
        return self.cloud_provider

    def novnc_url(self) -> str:
        if self.o2_login_novnc_url:
            return self.o2_login_novnc_url
        parsed = urlparse(self.app_base_url)
        port = self.novnc_port or parsed.port or 6080
        netloc = parsed.hostname or "localhost"
        if ":" in netloc and not netloc.startswith("["):
            netloc = f"[{netloc}]"
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth += f":{parsed.password}"
            netloc = f"{auth}@{netloc}"
        netloc = f"{netloc}:{port}"
        return urlunparse((parsed.scheme or "http", netloc, "", "", "", "")).rstrip("/") + self.novnc_path


def normalize_base(value: str) -> str:
    value = "/" + value.strip("/")
    return "/" if value == "/" else value


def read_secret(value: Optional[str], file_path: Optional[str]) -> Optional[str]:
    if value:
        return value.rstrip("\n")
    if not file_path:
        return None
    path = Path(file_path)
    if path.exists():
        return path.read_text(encoding="utf-8").rstrip("\n")
    return None


def ensure_directories(settings: Settings) -> None:
    for raw in [
        settings.config_dir,
        settings.cache_dir,
        settings.data_dir,
        settings.upload_spool_dir,
        str(Path(settings.cache_dir) / "recent-uploads"),
        str(Path(settings.log_file).parent),
        str(Path(settings.o2_session_file).parent),
    ]:
        Path(raw).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if os.environ.get("O2GATEWAY_TEST_MODE") != "1":
        ensure_directories(settings)
    return settings
