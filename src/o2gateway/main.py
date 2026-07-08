from __future__ import annotations

import logging
from contextlib import asynccontextmanager
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
from o2gateway.operations.logging import configure_logging
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
        self.o2_session_store = O2SessionStore(settings)
        self.o2_api = self._build_cloud_api()
        self.locks = WebDavLockService(self.db)
        self._simulated_store: Optional[SimulatedCloudFileStore] = None
        self._o2_store: Optional[O2CloudFileStore] = None
        self.o2_login = O2PlaywrightLoginService(settings, self.o2_session_store, self.o2_api)
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

    def store(self) -> CloudFileStore:
        if self.settings.cloud_provider.lower() in {"o2", "movistar"}:
            if self._o2_store is None:
                self._o2_store = O2CloudFileStore(self.o2_api, self.metadata_cache)
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
        logger.info("o2cloud gateway started")
        try:
            yield
        finally:
            await services.close()

    app = FastAPI(title="O2Cloud WebDAV Gateway", version="0.1.0", lifespan=lifespan)

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
            )
        )
    if settings.webdav_enabled:
        app.include_router(create_webdav_router(settings, services.auth, services.store, services.locks))
    return app


def run() -> None:
    settings = get_settings()
    uvicorn.run("o2gateway.main:create_app", factory=True, host=settings.app_host, port=settings.app_port)


if __name__ == "__main__":
    run()
