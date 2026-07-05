from __future__ import annotations
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from o2gateway.cloud.base import CloudFileStore
from o2gateway.o2.login import O2LoginCoordinator, O2PlaywrightLoginService
from o2gateway.o2.session import O2SessionStore, deserialize_session
from o2gateway.persistence.metadata_cache import MetadataCache
from o2gateway.security.auth import LocalAuth
from o2gateway.settings import Settings
from o2gateway.webdav.locks import WebDavLockService


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def create_admin_router(
    settings: Settings,
    auth: LocalAuth,
    session_store: O2SessionStore,
    store_factory,
    metadata_cache: MetadataCache,
    locks: WebDavLockService,
    login_service: Optional[O2PlaywrightLoginService],
    login_coordinator: O2LoginCoordinator,
) -> APIRouter:
    router = APIRouter()
    base = settings.normalized_admin_base()

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
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "settings": settings,
                "csrf": csrf(request),
                "session": session_store.read(),
                "webdav_url": settings.app_base_url.rstrip("/") + settings.normalized_webdav_base(),
                "novnc_url": settings.novnc_url(),
                "login_status": login_coordinator.status(),
            },
        )

    @router.get(base + "/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"settings": settings, "error": None})

    @router.post(base + "/login")
    async def login(request: Request, username: str = Form(...), password: str = Form(...)):
        if not auth.check_admin_password(username, password):
            return templates.TemplateResponse(request, "login.html", {"settings": settings, "error": "Credenciales incorrectas"}, status_code=401)
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
            quota_value = await store_factory().quota()
            quota = {
                "usedBytes": quota_value.used_bytes,
                "totalBytes": quota_value.total_bytes,
                "freeBytes": quota_value.free_bytes,
            }
        except CloudSessionExpired as ex:
            o2_session = "expired"
            test_error = "Sesión O2 expirada, vuelve a iniciar sesión. (%s)" % str(ex)
        except CloudSessionMissing as ex:
            o2_session = "missing"
            test_error = str(ex)
        except Exception as ex:
            test_error = str(ex)
        return {
            "service": "ok",
            "version": "0.1.0",
            "cloudProvider": settings.cloud_provider,
            "webdavUrl": settings.app_base_url.rstrip("/") + settings.normalized_webdav_base(),
            "o2Session": o2_session,
            "quota": quota,
            "metadataCacheEntries": await metadata_cache.count(),
            "activeLocks": len(await locks.list_active()),
            "lastError": test_error,
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

    @router.post("/api/admin/test")
    async def test_connection(request: Request):
        auth_response = require_json_admin(request)
        if auth_response:
            return auth_response
        if not auth.validate_csrf(request):
            return JSONResponse({"error": "invalid csrf"}, status_code=403)
        store: CloudFileStore = store_factory()
        items = await store.list("/")
        quota = await store.quota()
        return {"ok": True, "rootItems": len(items), "quota": quota.__dict__}

    return router
