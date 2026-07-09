from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from o2gateway.cloud.base import CloudFileStore, CloudItemMetadata, normalize_cloud_path, parent_path
from o2gateway.operations.errors import (
    CloudAlreadyExists,
    CloudError,
    CloudForbidden,
    CloudNotFound,
    CloudUnsupported,
)
from o2gateway.operations.ids import new_operation_id
from o2gateway.security.auth import LocalAuth
from o2gateway.settings import Settings
from o2gateway.webdav import xml
from o2gateway.webdav.locks import WebDavLockService
from o2gateway.webdav.parsing import (
    cloud_path_from_request,
    destination_to_cloud_path,
    href_for_cloud_path,
    lock_token,
    overwrite_enabled,
    parse_depth,
    parse_range,
    timeout_seconds,
)

logger = logging.getLogger(__name__)


def create_webdav_router(
    settings: Settings,
    auth: LocalAuth,
    store_factory: Callable[[], CloudFileStore],
    locks: WebDavLockService,
) -> APIRouter:
    router = APIRouter()
    path_base = settings.normalized_webdav_base()

    async def handler(request: Request, rest: str = "") -> Response:
        auth_response = auth.require_webdav(request)
        if auth_response is not None:
            return auth_response
        operation_id = new_operation_id()
        store = store_factory()
        method = request.method.upper()
        cloud_path = cloud_path_from_request(path_base, request.url.path)
        started = time.perf_counter()
        try:
            if method == "OPTIONS":
                return _options()
            if method == "PROPFIND":
                return await _propfind(settings, store, cloud_path, request)
            if method == "HEAD":
                return await _head(store, cloud_path)
            if method == "GET":
                return await _get(settings, store, cloud_path, request)
            if method == "PUT":
                _assert_writable(settings)
                await locks.assert_can_write(cloud_path, request.headers.get("if"))
                return await _put(settings, store, cloud_path, request, operation_id)
            if method == "MKCOL":
                _assert_writable(settings)
                await locks.assert_can_write(cloud_path, request.headers.get("if"))
                return await _mkcol(store, cloud_path)
            if method == "DELETE":
                _assert_writable(settings)
                if _is_ignored_appledouble(settings, cloud_path):
                    return Response(status_code=204)
                await locks.assert_can_write(cloud_path, request.headers.get("if"))
                return await _delete(store, cloud_path)
            if method == "MOVE":
                _assert_writable(settings)
                await locks.assert_can_write(cloud_path, request.headers.get("if"))
                destination = destination_to_cloud_path(request, path_base)
                return await _move(store, cloud_path, destination, request)
            if method == "COPY":
                raise CloudUnsupported("COPY is not implemented in V1")
            if method == "LOCK":
                if _is_ignored_appledouble(settings, cloud_path):
                    return Response(status_code=204)
                return await _lock(locks, cloud_path, request)
            if method == "UNLOCK":
                return await _unlock(locks, request)
            if method == "PROPPATCH":
                return Response(status_code=405)
            return Response(status_code=405)
        except CloudNotFound as ex:
            logger.debug("webdav not found", extra={"operationId": operation_id, "path": str(ex)})
            return Response(xml.error_response(str(ex)), status_code=ex.status_code, media_type="application/xml")
        except CloudError as ex:
            logger.warning("webdav cloud error", extra={"operation_id": operation_id}, exc_info=True)
            return Response(xml.error_response(str(ex)), status_code=ex.status_code, media_type="application/xml")
        except ValueError as ex:
            return Response(xml.error_response(str(ex)), status_code=400, media_type="application/xml")
        except Exception as ex:
            logger.exception("webdav unhandled error", extra={"operation_id": operation_id})
            return Response(xml.error_response("gateway error"), status_code=502, media_type="application/xml")
        finally:
            logger.info(
                "webdav request completed",
                extra={
                    "operation_id": operation_id,
                    "method": method,
                    "path": cloud_path,
                    "durationMs": round((time.perf_counter() - started) * 1000, 2),
                },
            )

    route_path = path_base
    router.add_api_route(route_path, handler, methods=["OPTIONS", "PROPFIND", "HEAD", "GET", "PUT", "DELETE", "MKCOL", "MOVE", "COPY", "LOCK", "UNLOCK", "PROPPATCH"])
    router.add_api_route(route_path + "/{rest:path}", handler, methods=["OPTIONS", "PROPFIND", "HEAD", "GET", "PUT", "DELETE", "MKCOL", "MOVE", "COPY", "LOCK", "UNLOCK", "PROPPATCH"])
    return router


def _options() -> Response:
    return Response(
        status_code=204,
        headers={
            "DAV": "1, 2",
            "Allow": "OPTIONS, PROPFIND, HEAD, GET, PUT, DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK, PROPPATCH",
            "MS-Author-Via": "DAV",
        },
    )


async def _propfind(settings: Settings, store: CloudFileStore, cloud_path: str, request: Request) -> Response:
    if _is_ignored_appledouble(settings, cloud_path):
        raise CloudNotFound(cloud_path)
    depth = parse_depth(request.headers.get("depth"))
    if depth == "infinity" and not settings.webdav_depth_infinity:
        depth = "1"
    item = await store.get_metadata(cloud_path)
    if item is None:
        raise CloudNotFound(cloud_path)
    pairs = [(href_for_cloud_path(settings.normalized_webdav_base(), item.path, item.is_folder), item)]
    if item.is_folder and depth != "0":
        children = _filter_ignored_appledouble(settings, await store.list(cloud_path))
        pairs.extend((href_for_cloud_path(settings.normalized_webdav_base(), child.path, child.is_folder), child) for child in children)
    return Response(xml.multistatus(pairs), status_code=207, media_type="application/xml")


async def _head(store: CloudFileStore, cloud_path: str) -> Response:
    item = await store.get_metadata(cloud_path)
    if item is None or item.is_folder:
        raise CloudNotFound(cloud_path)
    headers = _metadata_headers(item)
    return Response(status_code=200, headers=headers)


async def _get(settings: Settings, store: CloudFileStore, cloud_path: str, request: Request) -> Response:
    if _is_ignored_appledouble(settings, cloud_path):
        raise CloudNotFound(cloud_path)
    item = await store.get_metadata(cloud_path)
    if item is not None and item.is_folder:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return await _browser_listing(settings, store, cloud_path, request)
    if item is None or item.is_folder:
        raise CloudNotFound(cloud_path)
    byte_range = parse_range(request.headers.get("range"))
    status = 206 if byte_range is not None else 200
    headers = _metadata_headers(item, include_content_length=False)
    if byte_range is not None and item.size is not None:
        start, end = _range_bounds(byte_range, item.size)
        headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, item.size)
    iterator = store.open_read(cloud_path, byte_range)
    try:
        first_chunk = await anext(iterator)
    except StopAsyncIteration:
        return Response(b"", status_code=status, media_type=item.content_type or "application/octet-stream", headers=headers)
    return StreamingResponse(_prepend_chunk(first_chunk, iterator), status_code=status, media_type=item.content_type or "application/octet-stream", headers=headers)


async def _put(settings: Settings, store: CloudFileStore, cloud_path: str, request: Request, operation_id: str) -> Response:
    if not settings.webdav_allow_dotfiles and Path(cloud_path).name.startswith("."):
        raise CloudForbidden("dotfiles are disabled")
    if _is_ignored_appledouble(settings, cloud_path):
        async for _ in request.stream():
            pass
        return Response(status_code=204)
    max_bytes = settings.upload_max_file_mb * 1024 * 1024
    Path(settings.upload_spool_dir).mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="upload-", suffix=".tmp", dir=settings.upload_spool_dir)
    written = 0
    try:
        read_started = time.perf_counter()
        with os.fdopen(fd, "wb") as handle:
            async for chunk in request.stream():
                written += len(chunk)
                if written > max_bytes:
                    raise CloudForbidden("upload exceeds configured limit")
                handle.write(chunk)
        logger.info(
            "webdav upload body spooled",
            extra={
                "operation_id": operation_id,
                "path": cloud_path,
                "bytesIn": written,
                "durationMs": round((time.perf_counter() - read_started) * 1000, 2),
            },
        )
        existed = await store.get_metadata(cloud_path)
        upload_started = time.perf_counter()
        item = await store.upload(cloud_path, tmp_path, overwrite=True)
        logger.info(
            "webdav upload store completed",
            extra={
                "operation_id": operation_id,
                "path": cloud_path,
                "bytesIn": written,
                "statusCode": 204 if existed else 201,
                "durationMs": round((time.perf_counter() - upload_started) * 1000, 2),
            },
        )
        return Response(status_code=204 if existed else 201, headers={"ETag": item.etag or ""})
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _mkcol(store: CloudFileStore, cloud_path: str) -> Response:
    if await store.get_metadata(cloud_path) is not None:
        raise CloudAlreadyExists(cloud_path)
    await store.create_folder(cloud_path)
    return Response(status_code=201)


async def _delete(store: CloudFileStore, cloud_path: str) -> Response:
    await store.delete(cloud_path, soft_delete=True)
    return Response(status_code=204)


async def _move(store: CloudFileStore, source: str, destination: str, request: Request) -> Response:
    overwrite = overwrite_enabled(request.headers.get("overwrite"))
    existed = await store.get_metadata(destination)
    await store.move(source, destination, overwrite=overwrite)
    return Response(status_code=204 if existed else 201)


async def _lock(locks: WebDavLockService, cloud_path: str, request: Request) -> Response:
    body = await request.body()
    owner = body.decode("utf-8", errors="ignore")[:500] if body else ""
    lock = await locks.create(cloud_path, owner=owner, timeout_seconds=timeout_seconds(request.headers.get("timeout")))
    return Response(
        xml.lockdiscovery(lock),
        status_code=200,
        media_type="application/xml",
        headers={"Lock-Token": "<%s>" % lock.token},
    )


async def _unlock(locks: WebDavLockService, request: Request) -> Response:
    token = lock_token(request.headers.get("lock-token"))
    if not token:
        return Response(status_code=400)
    released = await locks.release(token)
    return Response(status_code=204 if released else 409)


def _metadata_headers(item: CloudItemMetadata, *, include_content_length: bool = True) -> dict[str, str]:
    headers = {
        "Accept-Ranges": "bytes",
        "Last-Modified": xml.http_date(item.modified_at),
    }
    if include_content_length and item.size is not None:
        headers["Content-Length"] = str(item.size)
    if item.etag:
        headers["ETag"] = item.etag
    if item.content_type:
        headers["Content-Type"] = item.content_type
    return headers


def _range_bounds(byte_range, size: int) -> tuple[int, int]:
    start, end = byte_range
    if start < 0:
        start = max(0, size + start)
    if end is None or end >= size:
        end = size - 1
    return start, end


def _assert_writable(settings: Settings) -> None:
    if settings.webdav_read_only:
        raise CloudForbidden("gateway is in read-only mode")


async def _prepend_chunk(first_chunk: bytes, iterator):
    yield first_chunk
    async for chunk in iterator:
        yield chunk


async def _browser_listing(settings: Settings, store: CloudFileStore, cloud_path: str, request: Request) -> Response:
    from html import escape

    children = _filter_ignored_appledouble(settings, await store.list(cloud_path))
    display_path = cloud_path if cloud_path else "/"

    # Build breadcrumbs
    parts = [p for p in display_path.strip("/").split("/") if p]
    breadcrumbs = '<a href="{base}/">raíz</a>'.format(base=str(request.url).split("/dav")[0] + "/dav")
    accumulated = ""
    for part in parts:
        accumulated += "/" + part
        breadcrumbs += f' / <a href="{request.url.scheme}://{request.url.netloc}/dav{accumulated}/">{escape(part)}</a>'

    rows = []
    # Parent link (except at root)
    if parts:
        parent = "/" + "/".join(parts[:-1])
        parent_href = f"{request.url.scheme}://{request.url.netloc}/dav{parent}/" if parent != "/" else f"{request.url.scheme}://{request.url.netloc}/dav/"
        rows.append(f'<tr><td><a href="{parent_href}">&#x21A9; ..</a></td><td></td><td></td></tr>')

    folders = sorted([c for c in children if c.is_folder], key=lambda c: c.name.lower())
    files = sorted([c for c in children if not c.is_folder], key=lambda c: c.name.lower())

    for item in folders:
        href = f"{request.url.scheme}://{request.url.netloc}/dav{item.path}/"
        rows.append(
            f'<tr>'
            f'<td><a href="{href}">&#x1F4C1; {escape(item.name)}</a></td>'
            f'<td style="color:#627d98">—</td>'
            f'<td style="color:#627d98">{escape(xml.http_date(item.modified_at)) if item.modified_at else "—"}</td>'
            f'</tr>'
        )
    for item in files:
        href = f"{request.url.scheme}://{request.url.netloc}/dav{item.path}"
        size_str = _fmt_bytes(item.size) if item.size is not None else "—"
        rows.append(
            f'<tr>'
            f'<td><a href="{href}">&#x1F4C4; {escape(item.name)}</a></td>'
            f'<td style="text-align:right;color:#627d98">{size_str}</td>'
            f'<td style="color:#627d98">{escape(xml.http_date(item.modified_at)) if item.modified_at else "—"}</td>'
            f'</tr>'
        )

    rows_html = "\n".join(rows)
    html = f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>O2Cloud · {escape(display_path)}</title>
  <style>
    body{{font-family:system-ui,sans-serif;margin:0;background:#f4f6f8;color:#1f2933}}
    header{{background:#102a43;color:#fff;padding:14px 24px;font-weight:700}}
    main{{max-width:960px;margin:24px auto;padding:0 16px}}
    nav{{margin-bottom:12px;color:#627d98;font-size:.9em}} nav a{{color:#1769aa;text-decoration:none}} nav a:hover{{text-decoration:underline}}
    table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d9e2ec;border-radius:8px;overflow:hidden}}
    th{{text-align:left;padding:10px 14px;background:#f4f6f8;border-bottom:1px solid #d9e2ec;font-weight:600;color:#52606d;font-size:.85em;text-transform:uppercase;letter-spacing:.05em}}
    td{{padding:9px 14px;border-bottom:1px solid #f0f4f8;font-size:.95em}} tr:last-child td{{border-bottom:none}}
    tr:hover td{{background:#f0f4f8}}
    a{{color:#1769aa;text-decoration:none}} a:hover{{text-decoration:underline}}
    .size{{width:90px}} .date{{width:200px}}
  </style>
</head>
<body>
  <header>O2Cloud WebDAV Gateway</header>
  <main>
    <nav>{breadcrumbs}</nav>
    <table>
      <thead><tr><th>Nombre</th><th class="size">Tamaño</th><th class="date">Modificado</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </main>
</body>
</html>"""
    return Response(html, status_code=200, media_type="text/html; charset=utf-8")


def _fmt_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(size)
    for unit in units[:-1]:
        if v < 1024:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.2f} {unit}"
        v /= 1024
    return f"{v:.2f} TB"


def _is_ignored_appledouble(settings: Settings | None, cloud_path: str) -> bool:
    if settings is None or not settings.webdav_ignore_appledouble:
        return False
    return Path(cloud_path).name.startswith("._")


def _filter_ignored_appledouble(settings: Settings | None, items: list[CloudItemMetadata]) -> list[CloudItemMetadata]:
    if settings is None or not settings.webdav_ignore_appledouble:
        return items
    return [item for item in items if not Path(item.path).name.startswith("._")]
