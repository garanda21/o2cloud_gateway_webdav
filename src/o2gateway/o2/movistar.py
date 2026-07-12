from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

from o2gateway.o2.api import (
    PAGE_SIZE,
    O2CloudApiClient,
    O2Item,
    _absolute_url,
    _first_array,
    _first_media_date,
    _first_media_long,
    _first_media_string,
    _first_query_value,
    _media_id_candidates,
    _media_kind_for,
    _normalize_media_kind,
    _object,
    _o2_id,
)
from o2gateway.o2.session import O2Session


class MovistarCloudApiClient(O2CloudApiClient):
    async def _load_files(self, folder_id: str) -> list[O2Item]:
        output: list[O2Item] = []
        seen: set[str] = set()
        for offset in range(0, 100000, PAGE_SIZE):
            payload = await self._json("POST", "media", {"action": "get", "folderid": folder_id, "limit": str(PAGE_SIZE), "offset": str(offset)})
            data = _object(payload.get("data"))
            media = _first_array(data, "media", "files", "items") or _first_array(payload, "media", "files", "items")
            ids = [item_id for item in media for item_id in _media_id_candidates(item)]
            ids = [item_id for item_id in ids if item_id and item_id not in seen]
            if not ids:
                break
            details = await self._file_details(ids)
            for item in details:
                if item.id in seen:
                    continue
                seen.add(item.id)
                if item.parent_id and _o2_id(item.parent_id) != _o2_id(folder_id):
                    continue
                output.append(item)
            if not data.get("more") or len(media) < PAGE_SIZE:
                break
        return output

    async def rename_or_move(self, item, new_name: str, parent_folder_id: str) -> None:
        if item.is_folder:
            await super().rename_or_move(item, new_name, parent_folder_id)
            return
        media_kind = _normalize_media_kind(item.media_kind or _media_kind_for(item.name, None))
        payload = {"data": {"id": _o2_id(item.id), "name": new_name, "folderid": _o2_id(parent_folder_id)}}
        operations = [lambda: self._json("POST", "upload/%s" % media_kind, {"action": "save-metadata"}, payload, form=False)]
        if media_kind != "file":
            operations.append(lambda: self._json("POST", "upload/file", {"action": "save-metadata"}, payload, form=False))
        if item.name.lower() == new_name.lower():
            move_payload = {"data": {"ids": [_o2_id(item.id)], "parentid": _o2_id(parent_folder_id)}}
            operations.append(lambda: self._json("POST", "media/file", {"action": "move"}, move_payload))
        await self._first_success(*operations)

    async def resolve_download_url(self, item: O2Item) -> str:
        details = await self._file_details([item.id])
        if not details:
            return await super().resolve_download_url(item)
        detail = details[0]
        if detail.direct_url:
            return detail.direct_url
        return await super().resolve_download_url(item)

    async def _file_details(self, ids: list[str]) -> list[O2Item]:
        payload = await self._json("POST", "media/file", {"action": "get"}, {"data": {"ids": [_o2_id(item_id) for item_id in ids]}})
        data = _object(payload.get("data"))
        media_server = _first_media_string(data, "mediaserverurl") or self.settings.o2_api_base_url.split("/sapi")[0]
        files = _first_array(data, "files", "media", "items") or _first_array(payload, "files", "media", "items")
        output: list[O2Item] = []
        for file_item in files:
            item_id = (_media_id_candidates(file_item) or [""])[0]
            name = _first_media_string(file_item, "name", "filename", "title")
            if not item_id or not name:
                continue
            raw_type = _first_media_string(file_item, "type", "mediatype", "mimetype", "contenttype")
            direct_url = _absolute_url(_first_media_string(file_item, "url", "downloadurl"), media_server)
            playback_url = _absolute_url(_first_media_string(file_item, "playbackurl", "viewurl"), media_server)
            output.append(
                O2Item(
                    item_id,
                    name,
                    _first_media_string(file_item, "folder", "folderid", "folderId", "parentid"),
                    False,
                    size=max(0, _first_media_long(file_item, "size", "filesize", "fileSize", "contentlength", "contentLength") or 0),
                    modified_at=_first_media_date(file_item, "modificationdate", "creationdate", "uploaded", "date"),
                    direct_url=direct_url,
                    media_kind=_normalize_media_kind(_media_kind_for(name, raw_type)),
                    fingerprint=_first_media_string(file_item, "fingerprint", "hash", "etag", "sha1", "checksum"),
                    node=_first_media_string(file_item, "node", "servernode", "serverNode", "storageNode", "storagenode") or _first_query_value(playback_url, "node"),
                    download_token=_first_media_string(file_item, "k", "token", "downloadtoken", "downloadToken", "playbacktoken", "playbackToken") or _first_query_value(playback_url, "k", "token"),
                )
            )
        return output

    def _upload_url(self, query: dict[str, str], session: Optional[O2Session] = None) -> str:
        session = session or self._require_session()
        params = {key: value for key, value in query.items() if value is not None}
        if session.validation_key:
            params["validationkey"] = session.validation_key
        base = self.settings.o2_upload_base_url.rstrip("/")
        if not base.endswith("/upload"):
            base += "/upload"
        return base + "?" + urlencode(params)
