from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, Optional
from uuid import uuid4

from o2gateway.cloud.base import ByteRange, CloudItemMetadata, CloudQuota, basename, normalize_cloud_path, parent_path
from o2gateway.o2.api import O2CloudApiClient, O2Item, to_cloud_metadata
from o2gateway.operations.errors import CloudAlreadyExists, CloudNotFound
from o2gateway.persistence.metadata_cache import MetadataCache
from o2gateway.settings import Settings


logger = logging.getLogger(__name__)
LOCAL_READ_CHUNK_SIZE = 1024 * 1024


@dataclass
class RecentUpload:
    item: O2Item
    metadata: CloudItemMetadata
    local_path: str
    parent_folder_id: str
    remote_parent_folder_id: str
    remote_name: str
    expected_size: int
    expires_at: float


class O2CloudFileStore:
    def __init__(self, api: O2CloudApiClient, cache: MetadataCache, settings: Settings) -> None:
        self.api = api
        self.cache = cache
        self.settings = settings
        self._path_to_item: dict[str, O2Item] = {}
        self._recent_uploads: dict[str, RecentUpload] = {}
        self._deleted_paths: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._recent_upload_dir().mkdir(parents=True, exist_ok=True)

    async def list(self, path: str) -> list[CloudItemMetadata]:
        normalized = normalize_cloud_path(path)
        folder = await self._item_for_path(normalized)
        if folder is None or not folder.is_folder:
            raise CloudNotFound(path)
        items = await self.api.list_folder(folder.id)
        recent_children = self._recent_children(normalized)
        output = []
        for item in items:
            child_path = _join(normalized, item.name)
            if self._is_deleted(child_path):
                self._path_to_item.pop(child_path, None)
                continue
            recent = recent_children.get(child_path)
            if recent is not None and item.size != recent.expected_size:
                continue
            if recent is not None:
                self._discard_recent(child_path)
                recent_children.pop(child_path, None)
            self._path_to_item[child_path] = item
            metadata = to_cloud_metadata(item, child_path)
            await self.cache.put(metadata)
            output.append(metadata)
        for child_path, recent in recent_children.items():
            self._path_to_item[child_path] = recent.item
            await self.cache.put(recent.metadata)
            output.append(recent.metadata)
        return output

    async def get_metadata(self, path: str) -> Optional[CloudItemMetadata]:
        normalized = normalize_cloud_path(path)
        if self._is_deleted(normalized):
            return None
        recent = self._recent(normalized)
        if recent is not None:
            return recent.metadata
        cached = await self.cache.get(normalized)
        if cached is not None:
            return cached
        item = await self._item_for_path(normalized)
        if item is None:
            await self.cache.put_negative(normalized)
            return None
        metadata = to_cloud_metadata(item, normalized)
        await self.cache.put(metadata)
        return metadata

    async def open_read(self, path: str, byte_range: ByteRange = None) -> AsyncIterator[bytes]:
        if self._is_deleted(path):
            raise CloudNotFound(path)
        recent = self._recent(path)
        if recent is not None:
            async for chunk in _read_local_file(recent.local_path, byte_range):
                yield chunk
            return
        item = await self._item_for_path(path)
        if item is None or item.is_folder:
            raise CloudNotFound(path)
        async for chunk in self.api.download(item, byte_range):
            yield chunk

    async def create_folder(self, path: str) -> CloudItemMetadata:
        normalized = normalize_cloud_path(path)
        self._deleted_paths.pop(normalized, None)
        parent = await self._item_for_path(parent_path(normalized))
        if parent is None or not parent.is_folder:
            raise CloudNotFound(parent_path(normalized))
        created = await self.api.create_folder(parent.id, basename(normalized))
        self._path_to_item[normalized] = created
        await self.cache.invalidate(parent_path(normalized), normalized)
        metadata = to_cloud_metadata(created, normalized)
        await self.cache.put(metadata)
        return metadata

    async def upload(self, path: str, local_tmp_path: str, *, overwrite: bool = True) -> CloudItemMetadata:
        normalized = normalize_cloud_path(path)
        parent = await self._item_for_path(parent_path(normalized))
        if parent is None or not parent.is_folder:
            raise CloudNotFound(parent_path(normalized))
        existing = await self._item_for_path(normalized)
        if existing is not None and not overwrite:
            raise CloudAlreadyExists(normalized)
        cacheable = self._is_recent_cacheable(local_tmp_path)
        confirm_retries = self.settings.upload_confirm_retries if self._uses_short_upload_confirmation(local_tmp_path) else 48
        uploaded = await self.api.upload_file(
            parent.id,
            basename(normalized),
            local_tmp_path,
            confirm_retries=confirm_retries,
            confirm_delay_seconds=self.settings.upload_confirm_retry_delay_seconds,
        )
        if existing is not None and existing.id != uploaded.id:
            try:
                await self.api.move_to_trash(existing)
            except Exception:
                pass
        await self.cache.invalidate(parent_path(normalized), normalized)
        if cacheable:
            recent = self._store_recent_upload(normalized, uploaded, local_tmp_path, parent.id)
            self._path_to_item[normalized] = recent.item
            metadata = recent.metadata
        else:
            self._path_to_item[normalized] = uploaded
            metadata = to_cloud_metadata(uploaded, normalized)
        await self.cache.put(metadata)
        return metadata

    async def move(self, source: str, destination: str, *, overwrite: bool = False) -> CloudItemMetadata:
        src = normalize_cloud_path(source)
        dst = normalize_cloud_path(destination)
        recent = self._recent(src)
        if recent is not None:
            return await self._move_recent(recent, src, dst, overwrite)
        item = await self._item_for_path(src)
        if item is None:
            raise CloudNotFound(src)
        parent = await self._item_for_path(parent_path(dst))
        if parent is None or not parent.is_folder:
            raise CloudNotFound(parent_path(dst))
        existing = await self._item_for_path(dst)
        if existing is not None:
            if not overwrite:
                raise CloudAlreadyExists(dst)
            await self.api.move_to_trash(existing)
        await self.api.rename_or_move(item, basename(dst), parent.id)
        moved = O2Item(
            id=item.id,
            name=basename(dst),
            parent_id=parent.id,
            is_folder=item.is_folder,
            size=item.size,
            modified_at=item.modified_at,
            direct_url=item.direct_url,
            media_kind=item.media_kind,
            fingerprint=item.fingerprint,
            node=item.node,
            download_token=item.download_token,
        )
        self._path_to_item.pop(src, None)
        self._path_to_item[dst] = moved
        self._mark_deleted(src)
        self._deleted_paths.pop(dst, None)
        await self.cache.invalidate(src, dst, parent_path(src), parent_path(dst))
        metadata = to_cloud_metadata(moved, dst)
        await self.cache.put(metadata)
        return metadata

    async def delete(self, path: str, *, soft_delete: bool = True) -> None:
        normalized = normalize_cloud_path(path)
        recent = self._recent(normalized)
        if recent is not None:
            self._discard_recent(normalized)
            self._mark_deleted(normalized)
            self._path_to_item.pop(normalized, None)
            await self.cache.invalidate(normalized, parent_path(normalized))
            self._schedule_recent_delete(recent)
            return
        item = await self._item_for_path(normalized)
        if item is None:
            raise CloudNotFound(normalized)
        await self.api.move_to_trash(item)
        self._mark_deleted(normalized)
        self._path_to_item.pop(normalized, None)
        await self.cache.invalidate(normalized, parent_path(normalized))

    async def quota(self) -> CloudQuota:
        return await self.api.storage_info()

    async def _item_for_path(self, path: str) -> Optional[O2Item]:
        normalized = normalize_cloud_path(path)
        if self._is_deleted(normalized):
            return None
        recent = self._recent(normalized)
        if recent is not None:
            return recent.item
        if normalized in self._path_to_item:
            return self._path_to_item[normalized]
        if normalized == "/":
            root = await self.api.root_folder()
            self._path_to_item["/"] = root
            return root
        parent = await self._item_for_path(parent_path(normalized))
        if parent is None or not parent.is_folder:
            return None
        for child in await self.api.list_folder(parent.id):
            child_path = _join(parent_path(normalized), child.name)
            if self._is_deleted(child_path):
                self._path_to_item.pop(child_path, None)
                continue
            self._path_to_item[child_path] = child
            if child.name.lower() == basename(normalized).lower():
                return child
        return None

    def _recent_upload_dir(self) -> Path:
        return Path(self.settings.cache_dir) / "recent-uploads"

    def _is_recent_cacheable(self, local_tmp_path: str) -> bool:
        max_bytes = self.settings.upload_recent_cache_max_file_mb * 1024 * 1024
        size = os.path.getsize(local_tmp_path)
        return max_bytes > 0 and 0 < size <= max_bytes

    def _uses_short_upload_confirmation(self, local_tmp_path: str) -> bool:
        max_bytes = self.settings.upload_recent_cache_max_file_mb * 1024 * 1024
        return max_bytes > 0 and os.path.getsize(local_tmp_path) <= max_bytes

    def _mark_deleted(self, path: str) -> None:
        normalized = normalize_cloud_path(path)
        self._deleted_paths[normalized] = time.time() + self.settings.delete_tombstone_ttl_seconds
        self._recent_uploads.pop(normalized, None)
        self._path_to_item.pop(normalized, None)

    def _is_deleted(self, path: str) -> bool:
        normalized = normalize_cloud_path(path)
        expires_at = self._deleted_paths.get(normalized)
        if expires_at is None:
            return False
        if time.time() >= expires_at:
            self._deleted_paths.pop(normalized, None)
            return False
        return True

    def _recent(self, path: str) -> Optional[RecentUpload]:
        normalized = normalize_cloud_path(path)
        recent = self._recent_uploads.get(normalized)
        if recent is None:
            return None
        if time.time() >= recent.expires_at or not os.path.exists(recent.local_path):
            self._discard_recent(normalized)
            return None
        return recent

    def _recent_children(self, folder_path: str) -> dict[str, RecentUpload]:
        normalized = normalize_cloud_path(folder_path)
        return {
            path: recent
            for path, recent in list(self._recent_uploads.items())
            if parent_path(path) == normalized and self._recent(path) is not None
        }

    def _store_recent_upload(self, path: str, uploaded: O2Item, local_tmp_path: str, parent_folder_id: str) -> RecentUpload:
        normalized = normalize_cloud_path(path)
        target = self._recent_upload_dir() / ("upload-%s.tmp" % uuid4().hex)
        shutil.copyfile(local_tmp_path, target)
        size = os.path.getsize(target)
        item = O2Item(
            id=uploaded.id,
            name=basename(normalized),
            parent_id=parent_folder_id,
            is_folder=False,
            size=size,
            modified_at=datetime.now(timezone.utc),
            direct_url=uploaded.direct_url,
            media_kind=uploaded.media_kind,
            fingerprint=uploaded.fingerprint,
            node=uploaded.node,
            download_token=uploaded.download_token,
        )
        metadata = to_cloud_metadata(item, normalized)
        recent = RecentUpload(
            item=item,
            metadata=metadata,
            local_path=str(target),
            parent_folder_id=parent_folder_id,
            remote_parent_folder_id=parent_folder_id,
            remote_name=basename(normalized),
            expected_size=size,
            expires_at=time.time() + self.settings.upload_recent_cache_ttl_seconds,
        )
        self._discard_recent(normalized)
        self._recent_uploads[normalized] = recent
        logger.info(
            "recent upload cached",
            extra={
                "path": normalized,
                "remoteId": uploaded.id,
                "bytesIn": size,
                "ttlSeconds": self.settings.upload_recent_cache_ttl_seconds,
            },
        )
        return recent

    async def _move_recent(self, recent: RecentUpload, src: str, dst: str, overwrite: bool) -> CloudItemMetadata:
        if src == dst:
            return recent.metadata
        parent = await self._item_for_path(parent_path(dst))
        if parent is None or not parent.is_folder:
            raise CloudNotFound(parent_path(dst))
        existing = await self.get_metadata(dst)
        if existing is not None and not overwrite:
            raise CloudAlreadyExists(dst)
        self._deleted_paths.pop(dst, None)
        if existing is not None:
            destination_recent = self._recent(dst)
            if destination_recent is not None:
                self._discard_recent(dst)
                self._mark_deleted(dst)
                self._schedule_recent_delete(destination_recent)
            else:
                existing_item = await self._item_for_path(dst)
                try:
                    if existing_item is not None:
                        await self.api.move_to_trash(existing_item)
                except Exception:
                    pass
        item = O2Item(
            id=recent.item.id,
            name=basename(dst),
            parent_id=parent.id,
            is_folder=False,
            size=recent.expected_size,
            modified_at=datetime.now(timezone.utc),
            direct_url=recent.item.direct_url,
            media_kind=recent.item.media_kind,
            fingerprint=recent.item.fingerprint,
            node=recent.item.node,
            download_token=recent.item.download_token,
        )
        metadata = to_cloud_metadata(item, dst)
        self._recent_uploads.pop(src, None)
        self._recent_uploads[dst] = RecentUpload(
            item=item,
            metadata=metadata,
            local_path=recent.local_path,
            parent_folder_id=parent.id,
            remote_parent_folder_id=recent.remote_parent_folder_id,
            remote_name=recent.remote_name,
            expected_size=recent.expected_size,
            expires_at=recent.expires_at,
        )
        self._path_to_item.pop(src, None)
        self._path_to_item[dst] = item
        self._mark_deleted(src)
        self._deleted_paths.pop(dst, None)
        await self.cache.invalidate(src, dst, parent_path(src), parent_path(dst))
        await self.cache.put(metadata)
        self._schedule_recent_move(recent, dst, parent.id)
        return metadata

    def _discard_recent(self, path: str) -> None:
        recent = self._recent_uploads.pop(normalize_cloud_path(path), None)
        if recent is None:
            return
        try:
            os.unlink(recent.local_path)
        except OSError:
            pass

    def _schedule_recent_delete(self, recent: RecentUpload) -> None:
        self._track_background_task(asyncio.create_task(self._delete_recent_remote(recent), name="o2-delete-recent-upload"))

    def _schedule_recent_move(self, recent: RecentUpload, dst: str, parent_folder_id: str) -> None:
        self._track_background_task(asyncio.create_task(self._move_recent_remote(recent, dst, parent_folder_id), name="o2-move-recent-upload"))

    def _track_background_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_background_task)

    def _log_background_task(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("recent upload background task failed", exc_info=(type(error), error, error.__traceback__))

    async def _delete_recent_remote(self, recent: RecentUpload) -> None:
        expected_id = None if recent.item.id.startswith("pending:") else recent.item.id
        found = await self.api.find_child_with_retries(
            recent.remote_parent_folder_id,
            recent.remote_name,
            False,
            expected_size=recent.expected_size,
            expected_id=expected_id,
        )
        if found is None:
            logger.warning("recent upload remote delete skipped; item was not confirmed", extra={"fileName": recent.remote_name, "remoteId": recent.item.id})
            return
        await self.api.move_to_trash(found)
        logger.info("recent upload deleted remotely", extra={"fileName": recent.remote_name, "remoteId": found.id})

    async def _move_recent_remote(self, recent: RecentUpload, dst: str, parent_folder_id: str) -> None:
        expected_id = None if recent.item.id.startswith("pending:") else recent.item.id
        found = await self.api.find_child_with_retries(
            recent.remote_parent_folder_id,
            recent.remote_name,
            False,
            expected_size=recent.expected_size,
            expected_id=expected_id,
        )
        if found is None:
            logger.warning("recent upload remote move skipped; item was not confirmed", extra={"fileName": recent.remote_name, "remoteId": recent.item.id})
            return
        await self.api.rename_or_move(found, basename(dst), parent_folder_id)
        logger.info("recent upload moved remotely", extra={"sourceName": recent.remote_name, "destinationPath": dst, "remoteId": found.id})


def _join(parent: str, name: str) -> str:
    if parent == "/":
        return "/" + name
    return str(PurePosixPath(parent) / name)


async def _read_local_file(local_path: str, byte_range: ByteRange = None) -> AsyncIterator[bytes]:
    size = os.path.getsize(local_path)
    start = 0
    end = size - 1
    if byte_range is not None:
        start = max(0, byte_range[0])
        if byte_range[1] is not None:
            end = min(end, byte_range[1])
    remaining = max(0, end - start + 1)
    with open(local_path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(LOCAL_READ_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
