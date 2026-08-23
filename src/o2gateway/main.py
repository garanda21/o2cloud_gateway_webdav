from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from o2gateway.admin.router import create_admin_router
from o2gateway.cloud.base import CloudFileStore
from o2gateway.cloud.simulated import SimulatedCloudFileStore
from o2gateway.o2.api import O2CloudApiClient
from o2gateway.o2.login import O2LoginCoordinator, O2PlaywrightLoginService
from o2gateway.o2.movistar import MovistarCloudApiClient
from o2gateway.o2.session import O2SessionStore
from o2gateway.o2.store import O2CloudFileStore
from o2gateway.operations.build_info import BuildInfo, get_build_info
from o2gateway.operations.logging import configure_logging
from o2gateway.operations.telegram import TelegramNotifier
from o2gateway.persistence.db import Database
from o2gateway.persistence.metadata_cache import MetadataCache
from o2gateway.security.auth import LocalAuth
from o2gateway.settings import Settings, ensure_directories, get_settings
from o2gateway.webdav.locks import WebDavLockService
from o2gateway.webdav.router import create_webdav_router

logger = logging.getLogger(__name__)


class AppServices:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.sqlite_path)
        self.metadata_cache = MetadataCache(self.db, settings.cache_metadata_ttl_seconds, settings.cache_negative_ttl_seconds)
        self.auth = LocalAuth(settings)
        self.build_info: BuildInfo = get_build_info(settings.app_version, settings.app_commit)
        self.o2_session_store = O2SessionStore(settings)
        self.o2_api = self._build_cloud_api()
        self.telegram_notifier = TelegramNotifier(settings)
        self.locks = WebDavLockService(self.db)
        self._simulated_store: Optional[SimulatedCloudFileStore] = None
        self._o2_store: Optional[O2CloudFileStore] = None
        self.o2_login = O2PlaywrightLoginService(settings, self.o2_session_store, self.o2_api)
        self.o2_api.set_silent_reauthenticator(self.o2_login.silent_reauthenticate)
        self.o2_login_coordinator = O2LoginCoordinator(settings, self.o2_session_store, self.o2_login)

    def _build_cloud_api(self) -> O2CloudApiClient:
        if self.settings.cloud_provider.lower() == "movistar":
            return MovistarCloudApiClient(self.settings, self.o2_session_store)
        return O2CloudApiClient(self.settings, self.o2_session_store)

    async def initialize(self) -> None:
        await self.db.initialize()
        await self.locks.cleanup()

    async def close(self) -> None:
        await self.o2_api.close()
        await self.telegram_notifier.close()

    async def keep_session_alive(self) -> None:
        interval = self.settings.o2_session_keepalive_seconds
        if interval <= 0 or self.settings.cloud_provider.lower() not in {"o2", "movistar"}:
            return
        while True:
            await asyncio.sleep(max(60, interval))
            await self.o2_login.keep_session_alive()
            await self.notify_if_session_expired()

    async def monitor_session_expiry(self) -> None:
        interval = self.settings.telegram_alert_check_seconds
        if (
            interval <= 0
            or not self.telegram_notifier.configured
            or self.settings.cloud_provider.lower() not in {"o2", "movistar"}
        ):
            return
        while True:
            await asyncio.sleep(max(5, interval))
            await self.notify_if_session_expired()

    async def notify_if_session_expired(self) -> None:
        expired_at = self.o2_api.session_expired_at()
        if expired_at:
            await self.telegram_notifier.notify_session_expired(expired_at)

    def store(self) -> CloudFileStore:
        if self.settings.cloud_provider.lower() in {"o2", "movistar"}:
            if self._o2_store is None:
                self._o2_store = O2CloudFileStore(self.o2_api, self.metadata_cache, self.settings)
            return self._o2_store
        if self._simulated_store is None:
            self._simulated_store = SimulatedCloudFileStore(self.settings.simulated_root)
        return self._simulated_store


def create_app() -> FastAPI:
    settings = get_settings()
    ensure_directories(settings)
    configure_logging(settings.log_level, settings.log_file)
    services = AppServices(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await services.initialize()
        app.state.services = services
        keepalive_task = asyncio.create_task(services.keep_session_alive())
        notification_task = asyncio.create_task(services.monitor_session_expiry())
        logger.info("o2cloud gateway started")
        try:
            yield
        finally:
            for task in (keepalive_task, notification_task):
                task.cancel()
            for task in (keepalive_task, notification_task):
                with suppress(asyncio.CancelledError):
                    await task
            await services.close()

    app = FastAPI(title="O2Cloud WebDAV Gateway", version=services.build_info.version, lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(settings.normalized_admin_base(), status_code=303)

    if settings.admin_enabled:
        app.include_router(
            create_admin_router(
                settings,
                services.auth,
                services.o2_session_store,
                services.store,
                services.metadata_cache,
                services.locks,
                services.o2_login,
                services.o2_login_coordinator,
                services.telegram_notifier,
                services.build_info,
            )
        )
    if settings.webdav_enabled:
        app.include_router(create_webdav_router(settings, services.auth, services.store, services.locks))
    return app


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file)
    uvicorn.run("o2gateway.main:create_app", factory=True, host=settings.app_host, port=settings.app_port, log_config=None)


if __name__ == "__main__":
    run()
