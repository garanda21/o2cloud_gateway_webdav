import asyncio
import json

from fastapi import Request
from fastapi.responses import JSONResponse

import o2gateway.admin.router as admin_router
from o2gateway.admin.router import AdminQuotaCache, create_admin_router
from o2gateway.cloud.base import CloudQuota
from o2gateway.operations.build_info import get_build_info
from o2gateway.settings import Settings


async def test_admin_quota_cache_reuses_value_and_collapses_concurrent_reads():
    calls = 0

    async def load_quota():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return CloudQuota(used_bytes=calls, total_bytes=10, free_bytes=9)

    cache = AdminQuotaCache(60)
    first, second = await asyncio.gather(cache.get(load_quota), cache.get(load_quota))

    assert calls == 1
    assert first == second
    refreshed = await cache.get(load_quota, force=True)
    assert calls == 2
    assert refreshed.used_bytes == 2


async def test_log_clear_returns_structured_error_when_truncation_fails(tmp_path, monkeypatch):
    class AuthStub:
        def validate_admin_cookie(self, _cookie):
            return True

        def validate_csrf(self, _request):
            return True

    settings = Settings(log_file=str(tmp_path / "gateway.log"))
    router = create_admin_router(
        settings,
        AuthStub(),
        None,
        lambda: None,
        None,
        None,
        None,
        None,
        None,
        get_build_info("1.0.0", "abcdef123456"),
    )
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/admin/logs/clear")
    request = Request({"type": "http", "method": "POST", "path": "/api/admin/logs/clear", "headers": []})

    def fail_truncation(_path):
        raise PermissionError()

    monkeypatch.setattr(admin_router, "truncate_log_file", fail_truncation)

    response = await endpoint(request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    assert json.loads(response.body) == {
        "ok": False,
        "cleared": False,
        "error": "No se pudo limpiar el archivo de logs.",
    }
