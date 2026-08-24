from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from o2gateway.cloud.base import CloudFileStore, CloudQuota
from o2gateway.o2.login import O2LoginCoordinator, O2PlaywrightLoginService
from o2gateway.o2.session import O2SessionStore, deserialize_session
from o2gateway.operations.build_info import BuildInfo
from o2gateway.operations.errors import CloudSessionExpired, CloudSessionMissing
from o2gateway.operations.logging import truncate_log_file
from o2gateway.operations.telegram import TelegramNotifier
from o2gateway.persistence.metadata_cache import MetadataCache
from o2gateway.security.auth import LocalAuth
from o2gateway.settings import Settings
from o2gateway.webdav.locks import WebDavLockService


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
logger = logging.getLogger(__name__)


class AdminQuotaCache:
    """Collapse concurrent quota reads and reuse them for a short TTL."""

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = max(0.0, ttl_seconds)
        self._value: Optional[CloudQuota] = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def _fresh_value(self) -> Optional[CloudQuota]:
        if self._value is not None and monotonic() < self._expires_at:
            return self._value
        return None

    async def get(self, loader: Callable[[], Awaitable[CloudQuota]], *, force: bool = False) -> CloudQuota:
        cached = self._fresh_value()
        if not force and cached is not None:
            return cached
        async with self._lock:
            cached = self._fresh_value()
            if not force and cached is not None:
                return cached
            value = await loader()
            self._value = value
            self._expires_at = monotonic() + self.ttl_seconds
            return value


def create_admin_router(
    settings: Settings,
    auth: LocalAuth,
    session_store: O2SessionStore,
    store_factory,
    metadata_cache: MetadataCache,
    locks: WebDavLockService,
    login_service: Optional[O2PlaywrightLoginService],
    login_coordinator: O2LoginCoordinator,
    telegram_notifier: TelegramNotifier,
    build_info: BuildInfo,
) -> APIRouter:
    router = APIRouter()
    base = settings.normalized_admin_base()
    quota_cache = AdminQuotaCache(settings.cache_quota_ttl_seconds)

    def is_admin(request: Request) -> bool:
        return auth.validate_admin_cookie(request.cookies.get("admin_session"))

    def csrf(request: Request) -> str:
        cookie = request.cookies.get("admin_session") or ""
        return auth.csrf_token(cookie) if cookie else ""

    def require_json_admin(request: Request) -> Optional[Response]:
        if is_admin(request):
            return None
        return JSONResponse({"error": "admin authentication required"}, status_code=401)

    @router.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(base, status_code=303)

    @router.get(base, response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not is_admin(request):
            return RedirectResponse(base + "/login", status_code=303)
        session = session_store.read()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "settings": settings,
                "csrf": csrf(request),
                "session": session,
                "renewal_available": bool(
                    session and (session.can_refresh or (login_service is not None and login_service.has_persistent_profile()))
                ),
                "webdav_url": settings.app_base_url.rstrip("/") + settings.normalized_webdav_base(),
                "novnc_url": settings.novnc_url(),
                "login_status": login_coordinator.status(),
                "provider_label": settings.provider_label(),
                "telegram_status": telegram_notifier.status(),
                "build_info": build_info,
            },
        )

    @router.get(base + "/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"settings": settings, "error": None, "build_info": build_info},
        )

    @router.post(base + "/login")
    async def login(request: Request, username: str = Form(...), password: str = Form(...)):
        if not auth.check_admin_password(username, password):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"settings": settings, "error": "Credenciales incorrectas", "build_info": build_info},
                status_code=401,
            )
        cookie = auth.create_admin_cookie(username)
        response = RedirectResponse(base, status_code=303)
        response.set_cookie("admin_session", cookie, httponly=True, samesite="lax", secure=settings.app_base_url.startswith("https://"))
        return response

    @router.post(base + "/logout")
    async def logout():
        response = RedirectResponse(base + "/login", status_code=303)
        response.delete_cookie("admin_session")
        return response

    @router.get("/api/admin/status")
    async def status(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        session = session_store.read()
        quota = None
        test_error = None
        o2_session = "configured" if session and session.is_authenticated else "missing"
        try:
            quota_value = await quota_cache.get(store_factory().quota)
            quota = {
                "usedBytes": quota_value.used_bytes,
                "totalBytes": quota_value.total_bytes,
                "freeBytes": quota_value.free_bytes,
            }
        except CloudSessionExpired as ex:
            o2_session = "expired"
            test_error = "Sesión %s expirada, vuelve a iniciar sesión. (%s)" % (settings.provider_label(), str(ex))
        except CloudSessionMissing as ex:
            o2_session = "missing"
            test_error = str(ex)
        except Exception as ex:
            test_error = str(ex)
        return {
            "service": "ok",
            "version": build_info.version,
            "commit": build_info.commit,
            "cloudProvider": settings.cloud_provider,
            "cloudProviderLabel": settings.provider_label(),
            "webdavUrl": settings.app_base_url.rstrip("/") + settings.normalized_webdav_base(),
            "o2Session": o2_session,
            "renewalAvailable": bool(
                session and (session.can_refresh or (login_service is not None and login_service.has_persistent_profile()))
            ),
            "quota": quota,
            "metadataCacheEntries": await metadata_cache.count(),
            "activeLocks": len(await locks.list_active()),
            "lastError": test_error,
            "telegram": telegram_notifier.status(),
        }

    @router.get("/api/admin/o2/session")
    async def o2_session(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        session = session_store.read()
        return {
            "configured": bool(session and session.is_authenticated),
            "createdAt": session.created_at if session else None,
            "cookieCount": len(session.cookies) if session else 0,
            "userAgent": session.user_agent if session else None,
            "renewable": bool(session and (session.can_refresh or (login_service is not None and login_service.has_persistent_profile()))),
            "encrypted": session_store.box.enabled,
        }

    @router.get("/api/admin/o2/login/status")
    async def o2_login_status(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        return login_coordinator.status()

    @router.post("/api/admin/o2/logout")
    async def o2_logout(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        if not auth.validate_csrf(request):
            return JSONResponse({"error": "invalid csrf"}, status_code=403)
        session_store.delete()
        if login_service is not None:
            login_service.clear_session_cache()
        login_coordinator.reset()
        return {"ok": True}

    @router.post("/api/admin/o2/import")
    async def o2_import(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        if not auth.validate_csrf(request):
            return JSONResponse({"error": "invalid csrf"}, status_code=403)
        payload = await request.json()
        session = deserialize_session(payload)
        session_store.save(session)
        login_coordinator.reset()
        return {"ok": True, "configured": session.is_authenticated}

    @router.post("/api/admin/o2/login")
    async def o2_login(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        if not auth.validate_csrf(request):
            return JSONResponse({"error": "invalid csrf"}, status_code=403)
        if login_service is None:
            return JSONResponse({"error": "Playwright login is not available"}, status_code=501)
        try:
            status = await login_coordinator.start()
            return {"ok": True, "login": status}
        except Exception as ex:
            return JSONResponse({"ok": False, "error": str(ex)}, status_code=500)

    @router.post("/api/admin/cache/clear")
    async def cache_clear(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        if not auth.validate_csrf(request):
            return JSONResponse({"error": "invalid csrf"}, status_code=403)
        await metadata_cache.clear()
        return {"ok": True}

    @router.post("/api/admin/notifications/telegram/test")
    async def telegram_test(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        if not auth.validate_csrf(request):
            return JSONResponse({"error": "invalid csrf"}, status_code=403)
        if not telegram_notifier.configured:
            return JSONResponse(
                {"ok": False, "error": "Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID para activar los avisos."},
                status_code=409,
            )
        if not await telegram_notifier.send_test():
            return JSONResponse(
                {"ok": False, "error": telegram_notifier.last_error or "No se pudo enviar la notificación."},
                status_code=502,
            )
        return {"ok": True, "sentAt": telegram_notifier.last_notification_at}

    @router.get("/api/admin/locks")
    async def active_locks(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        return {"locks": [lock.__dict__ for lock in await locks.list_active()]}

    @router.get("/api/admin/logs")
    async def logs(request: Request, lines: int = 200):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        path = Path(settings.log_file)
        if not path.exists():
            return PlainTextResponse("")
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return PlainTextResponse("\n".join(content[-min(max(lines, 1), 2000) :]))

    @router.post("/api/admin/logs/clear")
    async def logs_clear(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        if not auth.validate_csrf(request):
            return JSONResponse({"error": "invalid csrf"}, status_code=403)
        try:
            return {"ok": True, "cleared": truncate_log_file(settings.log_file)}
        except OSError:
            logger.exception("failed to truncate the configured log file")
            return JSONResponse(
                {"ok": False, "cleared": False, "error": "No se pudo limpiar el archivo de logs."},
                status_code=500,
            )

    @router.post("/api/admin/test")
    async def test_connection(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        if not auth.validate_csrf(request):
            return JSONResponse({"error": "invalid csrf"}, status_code=403)
        store: CloudFileStore = store_factory()
        items = await store.list("/")
        quota = await quota_cache.get(store.quota, force=True)
        return {"ok": True, "rootItems": len(items), "quota": quota.__dict__}

    return router
