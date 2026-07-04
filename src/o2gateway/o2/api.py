from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx

from o2gateway.cloud.base import ByteRange, CloudItemMetadata, CloudQuota
from o2gateway.o2.session import O2Cookie, O2Session, O2SessionStore
from o2gateway.operations.errors import CloudError, CloudNotFound, CloudQuotaExceeded, CloudSessionExpired, CloudSessionMissing, CloudTimeout
from o2gateway.settings import Settings


PAGE_SIZE = 200
MEDIA_LIST_FIELDS = [
    "name",
    "modificationdate",
    "creationdate",
    "size",
    "thumbnails",
    "thumbnaildimensions",
    "viewurl",
    "videometadata",
    "audiometadata",
    "favorite",
    "shared",
    "etag",
    "origin",
    "folderid",
    "uploaded",
]


@dataclass(frozen=True)
class O2Item:
    id: str
    name: str
    parent_id: Optional[str]
    is_folder: bool
    size: int = 0
    modified_at: Optional[datetime] = None
    direct_url: Optional[str] = None
    media_kind: Optional[str] = None
    fingerprint: Optional[str] = None
    node: Optional[str] = None
    download_token: Optional[str] = None


class O2CloudApiClient:
    def __init__(self, settings: Settings, session_store: O2SessionStore) -> None:
        self.settings = settings
        self.session_store = session_store
        timeout = httpx.Timeout(settings.o2_http_timeout_seconds, read=settings.download_timeout_seconds)
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._folder_candidates: dict[str, list[str]] = {}
        self._download_urls: dict[str, str] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def validate_session(self, session: Optional[O2Session] = None) -> bool:
        session = session or self._require_session()
        probes = [
            ("GET", "profile", {"action": "get"}, None),
            ("GET", "user/status", {"action": "get"}, None),
            ("POST", "media/folder/root", {"action": "get"}, None),
        ]
        for method, resource, query, body in probes:
            try:
                response = await self._request(method, resource, query, body=body, session=session, parse_json=False)
                if response.status_code < 300:
                    text = response.text[:5000].lower()
                    if "<html" in text and "login" in text:
                        continue
                    if '"error"' in text and any(term in text for term in ["invalid", "unauthorized", "forbidden"]):
                        continue
                    return True
            except Exception:
                continue
        return False

    async def root_folder(self) -> O2Item:
        payload = await self._json("POST", "media/folder/root", {"action": "get"})
        data = _object(payload.get("data"))
        folders = _array(data.get("folders"))
        folder = folders[0] if folders else _first_object(payload, "rootfolder", "rootFolder", "folder") or payload
        candidates = _folder_id_candidates(folder)
        if not candidates:
            raise CloudError("O2 did not return root folder id")
        self._register_folder_candidates(candidates[0], candidates)
        return O2Item(
            id=candidates[0],
            name=_first_string(folder, "name") or "",
            parent_id=None,
            is_folder=True,
            modified_at=_first_date(folder, "modificationdate", "creationdate", "date"),
        )

    async def storage_info(self) -> CloudQuota:
        payload = await self._json("GET", "media", {"action": "get-storage-space", "softdeleted": "true"})
        data = _first_object(payload, "data", "storage") or payload
        used = _first_long_recursive(data, "used", "usedspace", "storageused") or 0
        free = _first_long_recursive(data, "free", "freespace", "storagefree")
        total = _first_long_recursive(data, "total", "totalspace", "quota", "capacity", "storagetotal")
        if total is None and free is not None:
            total = used + free
        total = total or 10 * 1024 * 1024 * 1024 * 1024
        free = free if free is not None else max(0, total - used)
        return CloudQuota(used_bytes=max(0, used), total_bytes=max(0, total), free_bytes=max(0, min(free, total)))

    async def list_folder(self, folder_id: str) -> list[O2Item]:
        last_error: Optional[Exception] = None
        for candidate in self._known_folder_candidates(folder_id):
            try:
                items = await self._load_folder(candidate)
                if items:
                    return sorted(items, key=lambda item: (not item.is_folder, item.name.lower()))
            except Exception as ex:
                last_error = ex
        if last_error:
            raise last_error
        return []

    async def create_folder(self, parent_folder_id: str, name: str) -> O2Item:
        payloads = [
            {"data": {"name": name, "parentid": _o2_id(parent_folder_id)}},
            {"data": {"magic": False, "offline": False, "name": name, "parentid": _o2_id(parent_folder_id)}},
        ]
        last_error: Optional[Exception] = None
        for payload in payloads:
            for form in [False, True]:
                try:
                    result = await self._json("POST", "media/folder", {"action": "save"}, payload, form=form)
                    parsed = _try_parse_item(result, parent_folder_id, True, name)
                    if parsed:
                        return parsed
                except Exception as ex:
                    last_error = ex
                    continue
        found = await self.find_child(parent_folder_id, name, True)
        if found:
            return found
        if isinstance(last_error, CloudQuotaExceeded):
            raise last_error
        raise CloudError("O2 created folder but did not return an id")

    async def upload_file(self, parent_folder_id: str, name: str, local_path: str) -> O2Item:
        size = Path(local_path).stat().st_size
        metadata: dict[str, Any] = {
            "name": name,
            "size": size,
            "modificationdate": "",
            "folderid": _o2_id(parent_folder_id),
        }
        content_type = mimetypes.guess_type(name)[0]
        if content_type:
            metadata["contenttype"] = content_type
        query = {"action": "save"}
        if size > 200 * 1024 * 1024:
            query["acceptasynchronous"] = "true"
        url = self._upload_url(query)
        session = self._require_session()
        headers = self._session_headers(session)
        headers.update({"Accept": "*/*", "X-Requested-With": "XMLHttpRequest", "Connection": "keep-alive"})
        with open(local_path, "rb") as handle:
            files = {
                "data": (None, json.dumps({"data": metadata}, separators=(",", ":")), "application/json"),
                "file": (name, handle, content_type or "application/octet-stream"),
            }
            response = await self.client.post(url, headers=headers, files=files)
        self._capture_cookies(response, session)
        if response.status_code in (401, 403):
            raise CloudSessionExpired("O2 rejected upload session")
        if response.status_code >= 500 and size > 200 * 1024 * 1024:
            confirmed = await self.find_child_with_retries(parent_folder_id, name, False, expected_size=size)
            if confirmed:
                return confirmed
        if response.status_code >= 400:
            raise CloudError("O2 upload failed HTTP %s: %s" % (response.status_code, response.text[:200]))
        parsed = None
        try:
            parsed = _try_parse_item(response.json(), parent_folder_id, False, name)
        except Exception:
            pass
        confirmed = await self.find_child_with_retries(parent_folder_id, name, False, expected_size=size, expected_id=parsed.id if parsed else None)
        return confirmed or parsed or O2Item("pending:upload:%s" % name, name, parent_folder_id, False, size=size)

    async def rename_or_move(self, item: O2Item, new_name: str, parent_folder_id: str) -> None:
        if item.is_folder:
            payload = {"data": {"magic": False, "offline": False, "id": _o2_id(item.id), "name": new_name, "parentid": _o2_id(parent_folder_id)}}
            await self._first_success(
                lambda: self._json("POST", "media/folder", {"action": "save"}, payload, form=True),
                lambda: self._json("POST", "media/folder", {"action": "save"}, payload, form=False),
            )
            return
        media_kind = _normalize_media_kind(item.media_kind or _media_kind_for(item.name, None))
        payload = {"data": {"id": _o2_id(item.id), "name": new_name, "folderid": _o2_id(parent_folder_id)}}
        operations = [lambda: self._json("POST", "upload/%s" % media_kind, {"action": "save-metadata"}, payload, form=True)]
        if media_kind != "file":
            operations.append(lambda: self._json("POST", "upload/file", {"action": "save-metadata"}, payload, form=True))
        if item.name.lower() == new_name.lower():
            move_payload = {"data": {"ids": [_o2_id(item.id)], "parentid": _o2_id(parent_folder_id)}}
            operations.append(lambda: self._json("POST", "media/file", {"action": "move"}, move_payload))
        await self._first_success(*operations)

    async def move_to_trash(self, item: O2Item) -> None:
        if item.is_folder:
            payload = {"data": {"ids": [_o2_id(item.id)]}}
            await self._first_success(
                lambda: self._json("POST", "media/folder", {"action": "softdelete"}, payload),
                lambda: self._json("POST", "media/folder", {"action": "softdelete"}, payload, form=True),
            )
            return
        media_kind = _normalize_media_kind(item.media_kind or _media_kind_for(item.name, None))
        operations = []
        for body_name in _delete_payload_names(media_kind):
            payload = {"data": {body_name: [_o2_id(item.id)]}}
            operations.append(lambda payload=payload: self._json("POST", "media/%s" % media_kind, {"action": "delete", "softdelete": "true"}, payload))
        operations.append(lambda: self._json("POST", "media/file", {"action": "delete", "softdelete": "true"}, {"data": {"files": [_o2_id(item.id)]}}))
        await self._first_success(*operations)

    async def download(self, item: O2Item, byte_range: ByteRange = None) -> AsyncIterator[bytes]:
        effective_range = byte_range
        if effective_range is None and item.size > 0:
            effective_range = (0, item.size - 1)
        urls = []
        if item.id in self._download_urls:
            urls.append(self._download_urls[item.id])
        if item.direct_url:
            urls.append(item.direct_url)
        try:
            resolved = await self.resolve_download_url(item)
            urls.append(resolved)
        except Exception:
            pass
        if item.node and item.download_token:
            urls.append(self._native_video_url(item.name, item.node, item.download_token))
        last_error: Optional[Exception] = None
        for raw_url in _unique(urls):
            try:
                async for chunk in self._download_url(raw_url, item.name, effective_range):
                    yield chunk
                self._download_urls[item.id] = raw_url
                return
            except Exception as ex:
                last_error = ex
                self._download_urls.pop(item.id, None)
        raise CloudError("O2 could not download requested file") from last_error

    async def resolve_download_url(self, item: O2Item) -> str:
        payload = await self._json(
            "POST",
            "media",
            {"action": "get", "origin": "omh,dropbox"},
            {"data": {"ids": [item.id], "fields": ["name", "url", "origin", "folderid", "size", "etag"]}},
        )
        data = _object(payload.get("data"))
        media_server = _first_string(data, "mediaserverurl") or self.settings.o2_api_base_url.split("/sapi")[0]
        media = _first_array(data, "media", "files", "videos", "audios", "pictures", "images", "items") or _first_array(payload, "media", "files", "videos", "audios", "pictures", "images", "items")
        if not media:
            raise CloudNotFound(item.name)
        detail = media[0]
        for key in ["downloadurl", "url", "viewurl", "playbackurl"]:
            value = _first_media_string(detail, key)
            if value:
                return _absolute_url(value, media_server)
        raise CloudNotFound(item.name)

    async def find_child(self, parent_folder_id: str, name: str, is_folder: bool) -> Optional[O2Item]:
        for item in await self.list_folder(parent_folder_id):
            if item.is_folder == is_folder and item.name.lower() == name.lower():
                return item
        return None

    async def find_child_with_retries(self, parent_folder_id: str, name: str, is_folder: bool, expected_size: Optional[int] = None, expected_id: Optional[str] = None) -> Optional[O2Item]:
        for _ in range(48):
            found = await self.find_child(parent_folder_id, name, is_folder)
            if found and (expected_size is None or found.size == expected_size) and (expected_id is None or found.id == expected_id):
                return found
            await asyncio.sleep(1.25)
        return None

    async def _load_folder(self, folder_id: str) -> list[O2Item]:
        items: list[O2Item] = []
        items.extend(await self._load_folders(folder_id))
        items.extend(await self._load_files(folder_id))
        return items

    async def _load_folders(self, folder_id: str) -> list[O2Item]:
        payload = await self._json("GET", "media/folder", {"action": "list", "parentid": folder_id})
        data = _object(payload.get("data"))
        folders = _array(data.get("folders")) or _array(payload.get("folders"))
        output = []
        current_ids = set(self._known_folder_candidates(folder_id))
        seen = set()
        for folder in folders:
            candidates = _folder_id_candidates(folder)
            item_id = next((candidate for candidate in candidates if candidate not in current_ids), None)
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            self._register_folder_candidates(item_id, candidates)
            output.append(O2Item(item_id, _first_string(folder, "name") or "Carpeta", folder_id, True, modified_at=_first_date(folder, "modificationdate", "creationdate", "date")))
        return output

    async def _load_files(self, folder_id: str) -> list[O2Item]:
        output: list[O2Item] = []
        seen = set()
        signatures = set()
        for offset in range(0, 100000, PAGE_SIZE):
            payload = await self._media_list_page(folder_id, offset)
            data = _object(payload.get("data"))
            media_server = _first_string(data, "mediaserverurl") or self.settings.o2_api_base_url.split("/sapi")[0]
            files = _first_array(data, "media", "files", "videos", "audios", "pictures", "images", "items") or _first_array(payload, "media", "files", "videos", "audios", "pictures", "images", "items")
            signature = "|".join(",".join(_media_id_candidates(item)) or str(index) for index, item in enumerate(files))
            if signature in signatures:
                break
            signatures.add(signature)
            added = 0
            for file_item in files:
                if _looks_like_folder(file_item):
                    continue
                ids = _media_id_candidates(file_item)
                name = _first_media_string(file_item, "name", "filename", "title")
                if not name:
                    continue
                size = _first_media_long(file_item, "size", "filesize", "fileSize", "contentlength", "contentLength") or 0
                key = ids[0] if ids else "%s:%s" % (name, size)
                if key in seen:
                    continue
                seen.add(key)
                raw_type = _first_media_string(file_item, "type", "mediatype", "mimetype", "contenttype")
                direct_url = _absolute_url(_first_media_string(file_item, "url", "downloadurl"), media_server)
                playback_url = _absolute_url(_first_media_string(file_item, "playbackurl", "viewurl"), media_server)
                output.append(
                    O2Item(
                        ids[0] if ids else key,
                        name,
                        folder_id,
                        False,
                        size=max(0, size),
                        modified_at=_first_media_date(file_item, "modificationdate", "creationdate", "uploaded", "date"),
                        direct_url=direct_url,
                        media_kind=_media_kind_for(name, raw_type),
                        fingerprint=_first_media_string(file_item, "fingerprint", "hash", "etag", "sha1", "checksum"),
                        node=_first_media_string(file_item, "node", "servernode", "serverNode", "storageNode", "storagenode") or _first_query_value(playback_url, "node"),
                        download_token=_first_media_string(file_item, "k", "token", "downloadtoken", "downloadToken", "playbacktoken", "playbackToken") or _first_query_value(playback_url, "k", "token"),
                    )
                )
                added += 1
            if not files or added == 0 or (not data.get("more") and len(files) < PAGE_SIZE):
                break
        return output

    async def _media_list_page(self, folder_id: str, offset: int) -> dict[str, Any]:
        query = {"action": "get", "folderid": folder_id, "limit": str(PAGE_SIZE)}
        if offset:
            query["offset"] = str(offset)
        try:
            return await self._json("POST", "media", query, {"data": {"fields": MEDIA_LIST_FIELDS}})
        except Exception:
            return await self._json("POST", "media", query)

    async def _download_url(self, raw_url: str, file_name: str, byte_range: ByteRange) -> AsyncIterator[bytes]:
        session = self._require_session()
        headers = self._session_headers(session)
        headers["Accept"] = "*/*"
        if byte_range is not None:
            start, end = byte_range
            headers["Range"] = "bytes=%s-%s" % (start, "" if end is None else end)
        async with self.client.stream("GET", _normalize_download_url(raw_url, file_name), headers=headers) as response:
            if response.status_code == 416:
                return
            if response.status_code in (401, 403):
                raise CloudSessionExpired("download session expired")
            response.raise_for_status()
            async for chunk in response.aiter_bytes(1024 * 1024):
                yield chunk

    async def _json(self, method: str, resource: str, query: dict[str, str], body: Any = None, form: bool = False) -> dict[str, Any]:
        response = await self._request(method, resource, query, body=body, form=form, parse_json=False)
        if response.status_code in (401, 403):
            raise CloudSessionExpired("O2 session expired")
        response.raise_for_status()
        try:
            payload = response.json()
        except json.JSONDecodeError as ex:
            raise CloudError("O2 returned non-JSON response: %s" % response.text[:200]) from ex
        _throw_if_o2_error(payload)
        return payload

    async def _request(self, method: str, resource: str, query: dict[str, str], body: Any = None, form: bool = False, session: Optional[O2Session] = None, parse_json: bool = True) -> httpx.Response:
        session = session or self._require_session()
        url = self._api_url(resource, query, session)
        headers = self._session_headers(session)
        headers["Accept"] = "application/json"
        if body is None:
            response = await self.client.request(method, url, headers=headers)
        elif form:
            response = await self.client.request(method, url, headers=headers, data={"data": json.dumps(body, separators=(",", ":"))})
        else:
            response = await self.client.request(method, url, headers=headers, json=body)
        self._capture_cookies(response, session)
        return response

    def _api_url(self, resource: str, query: dict[str, str], session: O2Session) -> str:
        params = {key: value for key, value in query.items() if value is not None}
        if session.validation_key:
            params["validationkey"] = session.validation_key
        base = self.settings.o2_api_base_url.rstrip("/") + "/"
        return urljoin(base, resource.lstrip("/")) + "?" + urlencode(params)

    def _upload_url(self, query: dict[str, str]) -> str:
        session = self._require_session()
        params = {key: value for key, value in query.items() if value is not None}
        if session.validation_key:
            params["validationkey"] = session.validation_key
        return self.settings.o2_upload_base_url.rstrip("/") + "/upload?" + urlencode(params)

    def _session_headers(self, session: O2Session) -> dict[str, str]:
        headers = {
            "User-Agent": session.user_agent or "O2CloudGateway/0.1",
            "Origin": self.settings.o2_api_base_url.split("/sapi")[0],
            "Referer": self.settings.o2_api_base_url.split("/sapi")[0] + "/",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "X-deviceid": "O2CloudGateway",
        }
        if session.cookie_header:
            headers["Cookie"] = session.cookie_header
        return headers

    def _capture_cookies(self, response: httpx.Response, session: O2Session) -> None:
        existing = {cookie.name: cookie for cookie in session.cookies}
        for cookie in response.cookies.jar:
            if cookie.name and cookie.value:
                existing[cookie.name] = O2Cookie(cookie.name, cookie.value, cookie.domain or "cloud.o2online.es", cookie.path or "/")
        session.cookies = list(existing.values())
        self.session_store.save(session)

    def _require_session(self) -> O2Session:
        session = self.session_store.read()
        if session is None or not session.is_authenticated:
            raise CloudSessionMissing("O2 session is not configured")
        return session

    def _known_folder_candidates(self, folder_id: str) -> list[str]:
        return self._folder_candidates.get(folder_id, [folder_id])

    def _register_folder_candidates(self, primary: str, candidates: list[str]) -> None:
        ordered = _unique([primary] + candidates)
        for candidate in ordered:
            self._folder_candidates[candidate] = ordered

    async def _first_success(self, *operations):
        last_error = None
        for operation in operations:
            try:
                return await operation()
            except Exception as ex:
                last_error = ex
        raise last_error or CloudError("O2 operation failed")

    def _native_video_url(self, file_name: str, node: str, token: str) -> str:
        query = urlencode({"name": file_name, "node": node, "k": token})
        return self.settings.o2_api_base_url.rstrip("/") + "/download/video?" + query


def to_cloud_metadata(item: O2Item, path: str) -> CloudItemMetadata:
    content_type = None if item.is_folder else mimetypes.guess_type(item.name)[0] or "application/octet-stream"
    etag = item.fingerprint or 'W/"%s-%s-%s"' % (item.id, item.size, int(item.modified_at.timestamp()) if item.modified_at else 0)
    return CloudItemMetadata(
        id=item.id,
        name=item.name,
        type="folder" if item.is_folder else "file",
        path=path,
        size=None if item.is_folder else item.size,
        modified_at=item.modified_at,
        content_type=content_type,
        etag=etag,
        raw={"mediaKind": item.media_kind, "directUrl": item.direct_url, "node": item.node, "downloadToken": item.download_token},
    )


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _array(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_object(payload: dict[str, Any], *names: str) -> Optional[dict[str, Any]]:
    for name in names:
        value = payload.get(name)
        if isinstance(value, dict):
            return value
    return None


def _first_array(payload: dict[str, Any], *names: str) -> list[Any]:
    for name in names:
        value = payload.get(name)
        if isinstance(value, list) and value:
            return value
    return []


def _first_string(payload: dict[str, Any], *names: str) -> Optional[str]:
    for name in names:
        value = payload.get(name)
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            return str(value)
    return None


def _media_views(element: dict[str, Any], depth: int = 0):
    yield element
    if depth >= 2:
        return
    for name in ["media", "file", "item", "metadata", "data", "properties", "content", "source"]:
        value = element.get(name)
        if isinstance(value, dict):
            yield from _media_views(value, depth + 1)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from _media_views(item, depth + 1)


def _first_media_string(element: dict[str, Any], *names: str) -> Optional[str]:
    for view in _media_views(element):
        value = _first_string(view, *names)
        if value:
            return value
    return None


def _first_media_long(element: dict[str, Any], *names: str) -> Optional[int]:
    for view in _media_views(element):
        value = _first_long(view, *names)
        if value is not None:
            return value
    return None


def _first_media_date(element: dict[str, Any], *names: str) -> Optional[datetime]:
    for view in _media_views(element):
        value = _first_date(view, *names)
        if value is not None:
            return value
    return None


def _folder_id_candidates(element: dict[str, Any]) -> list[str]:
    return _unique([_first_string(element, "id"), _first_string(element, "folderid"), _first_string(element, "folderId"), _first_string(element, "uuid")])


def _media_id_candidates(element: dict[str, Any]) -> list[str]:
    output = []
    for view in _media_views(element):
        output.extend([_first_string(view, "id"), _first_string(view, "mediaid"), _first_string(view, "mediaId"), _first_string(view, "fdoid"), _first_string(view, "uuid")])
    return _unique(output)


def _try_parse_item(payload: dict[str, Any], parent_folder_id: str, expected_is_folder: Optional[bool], fallback_name: str) -> Optional[O2Item]:
    for candidate in _object_candidates(payload):
        item_id = _first_string(candidate, "id", "folderid", "folderId", "mediaid", "fdoid", "uuid")
        if not item_id:
            continue
        name = _first_string(candidate, "name", "filename") or fallback_name
        raw_type = _first_string(candidate, "type", "mediatype", "mimetype", "contenttype")
        is_folder = expected_is_folder if expected_is_folder is not None else bool(raw_type and "folder" in raw_type.lower())
        return O2Item(
            item_id,
            name,
            _first_string(candidate, "parentid", "folderid", "folder", "folderId") or parent_folder_id,
            is_folder,
            size=0 if is_folder else max(0, _first_long(candidate, "size", "filesize", "fileSize", "contentlength", "contentLength") or 0),
            modified_at=_first_date(candidate, "modificationdate", "creationdate", "uploaded", "date"),
            direct_url=_absolute_url(_first_string(candidate, "url", "viewurl", "downloadurl"), "https://cloud.o2online.es"),
            media_kind=None if is_folder else _media_kind_for(name, raw_type),
            fingerprint=_first_string(candidate, "fingerprint", "hash", "etag", "sha1", "checksum"),
            node=_first_string(candidate, "node"),
            download_token=_first_string(candidate, "k", "token"),
        )
    return None


def _object_candidates(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _object_candidates(child)
    elif isinstance(value, list):
        for item in value:
            yield from _object_candidates(item)


def _looks_like_folder(element: dict[str, Any]) -> bool:
    raw = (_first_media_string(element, "type", "mediatype", "mediaType", "mimetype", "contenttype", "kind") or "").lower()
    return "folder" in raw or "directory" in raw


def _first_long(payload: dict[str, Any], *names: str) -> Optional[int]:
    for name in names:
        value = payload.get(name)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            parsed = _parse_byte_count(value)
            if parsed is not None:
                return parsed
    return None


def _first_long_recursive(payload: Any, *names: str) -> Optional[int]:
    if isinstance(payload, dict):
        direct = _first_long(payload, *names)
        if direct is not None:
            return direct
        for value in payload.values():
            found = _first_long_recursive(value, *names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _first_long_recursive(item, *names)
            if found is not None:
                return found
    return None


def _parse_byte_count(raw: str) -> Optional[int]:
    value = raw.strip().replace("\u00a0", " ")
    try:
        return int(value)
    except ValueError:
        pass
    match = re.match(r"(?P<number>\d+(?:[\.,]\d+)?)\s*(?P<unit>b|bytes|kb|kib|mb|mib|gb|gib|tb|tib)?", value, re.I)
    if not match:
        return None
    number = float(match.group("number").replace(",", "."))
    unit = (match.group("unit") or "b").lower()
    factor = {"kb": 1024, "kib": 1024, "mb": 1024**2, "mib": 1024**2, "gb": 1024**3, "gib": 1024**3, "tb": 1024**4, "tib": 1024**4}.get(unit, 1)
    return int(round(number * factor))


def _first_date(payload: dict[str, Any], *names: str) -> Optional[datetime]:
    for name in names:
        value = payload.get(name)
        if isinstance(value, int) and value > 0:
            return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, timezone.utc)
        if isinstance(value, str) and value:
            try:
                if value.isdigit():
                    number = int(value)
                    return datetime.fromtimestamp(number / 1000 if number > 10_000_000_000 else number, timezone.utc)
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                continue
    return None


def _media_kind_for(name: str, raw_type: Optional[str]) -> str:
    raw = (raw_type or "").lower()
    if "video" in raw:
        return "video"
    if "audio" in raw:
        return "audio"
    if "picture" in raw or "image" in raw:
        return "picture"
    ext = Path(name).suffix.lower().strip(".")
    if ext in {"mkv", "mp4", "avi", "mov", "m4v", "webm", "ts", "mpeg", "mpg", "m3u8"}:
        return "video"
    if ext in {"mp3", "aac", "m4a", "flac", "wav", "ogg", "opus", "wma"}:
        return "audio"
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "tif", "tiff"}:
        return "picture"
    return "file"


def _normalize_media_kind(value: str) -> str:
    value = value.lower().strip()
    return {"image": "picture", "track": "audio"}.get(value, value if value in {"file", "picture", "video", "audio"} else "file")


def _delete_payload_names(media_kind: str) -> list[str]:
    media_kind = _normalize_media_kind(media_kind)
    if media_kind == "picture":
        return ["pictures"]
    if media_kind == "video":
        return ["videos"]
    if media_kind == "audio":
        return ["audios", "tracks"]
    return ["files"]


def _o2_id(value: str) -> Any:
    try:
        return int(value)
    except ValueError:
        return value


def _throw_if_o2_error(payload: dict[str, Any]) -> None:
    error = payload.get("error")
    if error and error not in (0, "0", "OK", "SUCCESS", "COM-0000", False):
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
            if code == "FOL-1035" or "maximum folder count" in message.lower():
                raise CloudQuotaExceeded(message or code)
        raise CloudError("O2 returned error: %s" % str(error)[:240])
    success = payload.get("success")
    if isinstance(success, str) and success.lower() == "false":
        raise CloudError("O2 rejected operation")
    status = str(payload.get("status") or "").lower()
    if "error" in status or "fail" in status:
        raise CloudError("O2 rejected operation")


def _absolute_url(raw: Optional[str], media_server: str) -> Optional[str]:
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return media_server.rstrip("/") + "/" + raw.lstrip("/")


def _first_query_value(raw_url: Optional[str], *names: str) -> Optional[str]:
    if not raw_url:
        return None
    query = parse_qs(urlparse(raw_url).query)
    for name in names:
        values = query.get(name)
        if values:
            return values[0]
    return None


def _normalize_download_url(raw_url: str, file_name: str) -> str:
    parsed = urlparse(raw_url)
    if not parsed.scheme:
        return raw_url
    query = parse_qs(parsed.query)
    query["filename"] = [file_name]
    parsed = parsed._replace(query=urlencode(query, doseq=True))
    return urlunparse(parsed)


def _unique(values):
    output = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output
