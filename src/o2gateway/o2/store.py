from __future__ import annotations

from pathlib import PurePosixPath
from typing import AsyncIterator, Optional

from o2gateway.cloud.base import ByteRange, CloudItemMetadata, CloudQuota, basename, normalize_cloud_path, parent_path
from o2gateway.o2.api import O2CloudApiClient, O2Item, to_cloud_metadata
from o2gateway.operations.errors import CloudAlreadyExists, CloudNotFound
from o2gateway.persistence.metadata_cache import MetadataCache


class O2CloudFileStore:
    def __init__(self, api: O2CloudApiClient, cache: MetadataCache) -> None:
        self.api = api
        self.cache = cache
        self._path_to_item: dict[str, O2Item] = {}

    async def list(self, path: str) -> list[CloudItemMetadata]:
        normalized = normalize_cloud_path(path)
        folder = await self._item_for_path(normalized)
        if folder is None or not folder.is_folder:
            raise CloudNotFound(path)
        items = await self.api.list_folder(folder.id)
        output = []
        for item in items:
            child_path = _join(normalized, item.name)
            self._path_to_item[child_path] = item
            metadata = to_cloud_metadata(item, child_path)
            await self.cache.put(metadata)
            output.append(metadata)
        return output

    async def get_metadata(self, path: str) -> Optional[CloudItemMetadata]:
        normalized = normalize_cloud_path(path)
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
        item = await self._item_for_path(path)
        if item is None or item.is_folder:
            raise CloudNotFound(path)
        async for chunk in self.api.download(item, byte_range):
            yield chunk

    async def create_folder(self, path: str) -> CloudItemMetadata:
        normalized = normalize_cloud_path(path)
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
        uploaded = await self.api.upload_file(parent.id, basename(normalized), local_tmp_path)
        if existing is not None and existing.id != uploaded.id:
            try:
                await self.api.move_to_trash(existing)
            except Exception:
                pass
        self._path_to_item[normalized] = uploaded
        await self.cache.invalidate(parent_path(normalized), normalized)
        metadata = to_cloud_metadata(uploaded, normalized)
        await self.cache.put(metadata)
        return metadata

    async def move(self, source: str, destination: str, *, overwrite: bool = False) -> CloudItemMetadata:
        src = normalize_cloud_path(source)
        dst = normalize_cloud_path(destination)
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
        await self.cache.invalidate(src, dst, parent_path(src), parent_path(dst))
        metadata = to_cloud_metadata(moved, dst)
        await self.cache.put(metadata)
        return metadata

    async def delete(self, path: str, *, soft_delete: bool = True) -> None:
        normalized = normalize_cloud_path(path)
        item = await self._item_for_path(normalized)
        if item is None:
            raise CloudNotFound(normalized)
        await self.api.move_to_trash(item)
        self._path_to_item.pop(normalized, None)
        await self.cache.invalidate(normalized, parent_path(normalized))

    async def quota(self) -> CloudQuota:
        return await self.api.storage_info()

    async def _item_for_path(self, path: str) -> Optional[O2Item]:
        normalized = normalize_cloud_path(path)
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
            self._path_to_item[child_path] = child
            if child.name.lower() == basename(normalized).lower():
                return child
        return None


def _join(parent: str, name: str) -> str:
    if parent == "/":
        return "/" + name
    return str(PurePosixPath(parent) / name)

