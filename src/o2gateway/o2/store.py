from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, Callable, Coroutine, Optional
from uuid import uuid4

from o2gateway.cloud.base import ByteRange, CloudItemMetadata, CloudQuota, basename, normalize_cloud_path, parent_path
from o2gateway.o2.api import O2CloudApiClient, O2Item, to_cloud_metadata
from o2gateway.operations.errors import CloudAlreadyExists, CloudMediaNotValidated, CloudNotFound
from o2gateway.persistence.metadata_cache import MetadataCache
from o2gateway.settings import Settings


logger = logging.getLogger(__name__)
LOCAL_READ_CHUNK_SIZE = 1024 * 1024

# Estados del overlay local. El proveedor (Funambol) valida los media de forma
# asíncrona (~4-10s, MED-1017 mientras tanto) y su listado de carpeta puede
# tardar >60s en reflejar cambios, así que el overlay es la fuente de verdad
# para el cliente WebDAV hasta que el remoto se pone al día.
PENDING_CREATE = "pending_create"  # placeholder local (PUT de 0 bytes de Finder); nunca subido
UPLOADED = "uploaded"  # subido con éxito; visible desde overlay hasta que el listado remoto lo muestre
PENDING_DELETE = "pending_delete"  # borrado aceptado; oculto hasta que el remoto deje de listarlo


@dataclass
class OverlayEntry:
    state: str
    item: O2Item
    metadata: CloudItemMetadata
    local_path: Optional[str]
    remote_id: Optional[str]
    parent_folder_id: Optional[str]
    expires_at: float
    remote_deleted: bool = False


class O2CloudFileStore:
    def __init__(self, api: O2CloudApiClient, cache: MetadataCache, settings: Settings) -> None:
        self.api = api
        self.cache = cache
        self.settings = settings
        self._path_to_item: dict[str, O2Item] = {}
        self._overlay: dict[str, OverlayEntry] = {}
        self._hidden_remote_ids: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._spool_dir().mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ lectura

    async def list(self, path: str) -> list[CloudItemMetadata]:
        normalized = normalize_cloud_path(path)
        folder = await self._item_for_path(normalized)
        if folder is None or not folder.is_folder:
            raise CloudNotFound(path)
        items = await self.api.list_folder(folder.id)
        overlay_children = self._overlay_children(normalized)
        listed_ids = {item.id for item in items}
        output: list[CloudItemMetadata] = []
        covered_paths: set[str] = set()
        for item in items:
            child_path = _join(normalized, item.name)
            if self._is_hidden_remote_id(item.id):
                continue
            entry = overlay_children.get(child_path)
            if entry is not None:
                if entry.state == PENDING_DELETE:
                    continue
                if entry.state == UPLOADED and item.id == entry.remote_id and item.size == entry.metadata.size:
                    # El listado remoto ya refleja la subida: el overlay sobra.
                    self._drop_entry(child_path)
                    overlay_children.pop(child_path, None)
                else:
                    # El overlay gana (placeholder local o fila remota desfasada).
                    continue
            self._path_to_item[child_path] = item
            metadata = to_cloud_metadata(item, child_path)
            await self.cache.put(metadata)
            output.append(metadata)
            covered_paths.add(child_path)
        for child_path, entry in overlay_children.items():
            if child_path in covered_paths or entry.state == PENDING_DELETE:
                continue
            self._path_to_item[child_path] = entry.item
            await self.cache.put(entry.metadata)
            output.append(entry.metadata)
        self._sweep_settled_tombstones(normalized, listed_ids)
        return output

    async def get_metadata(self, path: str) -> Optional[CloudItemMetadata]:
        normalized = normalize_cloud_path(path)
        entry = self._entry(normalized)
        if entry is not None:
            return None if entry.state == PENDING_DELETE else entry.metadata
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
        normalized = normalize_cloud_path(path)
        entry = self._entry(normalized)
        if entry is not None:
            if entry.state == PENDING_DELETE:
                raise CloudNotFound(path)
            if entry.local_path is not None:
                async for chunk in _read_local_file(entry.local_path, byte_range):
                    yield chunk
                return
            if (entry.metadata.size or 0) == 0:
                return
            # Subida grande sin copia local: el remoto sirve descargas incluso
            # durante la ventana de validación.
            async for chunk in self.api.download(entry.item, byte_range):
                yield chunk
            return
        item = await self._item_for_path(normalized)
        if item is None or item.is_folder:
            raise CloudNotFound(path)
        async for chunk in self.api.download(item, byte_range):
            yield chunk

    async def quota(self) -> CloudQuota:
        return await self.api.storage_info()

    # ------------------------------------------------------------------ escritura

    async def create_folder(self, path: str) -> CloudItemMetadata:
        normalized = normalize_cloud_path(path)
        entry = self._entry(normalized)
        if entry is not None and entry.state == PENDING_DELETE:
            self._drop_entry(normalized)
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
        size = os.path.getsize(local_tmp_path)
        remote_target = await self._remote_target_id(normalized, overwrite)
        if size == 0:
            # Placeholder de Finder (crea con PUT vacío y manda el contenido en un
            # segundo PUT). Nunca se sube al proveedor: un media de 0 bytes queda
            # ~10s en ventana MED-1017 y bloquea el flujo posterior del cliente.
            return self._store_pending_create(normalized, parent.id, remote_target)
        uploaded = await self._upload_with_validation_retry(parent.id, basename(normalized), local_tmp_path, remote_target)
        entry = self._store_uploaded(normalized, uploaded, local_tmp_path, parent.id)
        await self.cache.invalidate(parent_path(normalized), normalized)
        await self.cache.put(entry.metadata)
        return entry.metadata

    async def delete(self, path: str, *, soft_delete: bool = True) -> None:
        normalized = normalize_cloud_path(path)
        entry = self._entry(normalized)
        if entry is not None:
            if entry.state == PENDING_DELETE:
                return
            remote_id = entry.remote_id
            item = entry.item
            self._drop_entry(normalized)
            await self.cache.invalidate(normalized, parent_path(normalized))
            if entry.state == PENDING_CREATE and remote_id is None:
                # El placeholder nunca llegó al proveedor: borrado puramente local.
                return
            if remote_id is None:
                # Subida sin id remoto (formas de respuesta antiguas de O2): localizar
                # por nombre+tamaño antes de borrar para no tocar un archivo ajeno.
                self._store_tombstone(normalized, item, None)
                self._schedule(self._find_and_delete_remote(entry), "o2-find-delete")
                return
            self._store_tombstone(normalized, item, remote_id)
            self._schedule(self._delete_remote(normalized, item), "o2-delete-remote")
            return
        item = await self._item_for_path(normalized)
        if item is None:
            raise CloudNotFound(normalized)
        if item.is_folder:
            await self.api.move_to_trash(item)
            self._path_to_item.pop(normalized, None)
            await self.cache.invalidate(normalized, parent_path(normalized))
            return
        self._store_tombstone(normalized, item, item.id)
        self._path_to_item.pop(normalized, None)
        await self.cache.invalidate(normalized, parent_path(normalized))
        self._schedule(self._delete_remote(normalized, item), "o2-delete-remote")

    async def move(self, source: str, destination: str, *, overwrite: bool = False) -> CloudItemMetadata:
        src = normalize_cloud_path(source)
        dst = normalize_cloud_path(destination)
        if src == dst:
            existing = await self.get_metadata(src)
            if existing is None:
                raise CloudNotFound(src)
            return existing
        parent = await self._item_for_path(parent_path(dst))
        if parent is None or not parent.is_folder:
            raise CloudNotFound(parent_path(dst))
        existing = await self.get_metadata(dst)
        if existing is not None:
            if not overwrite:
                raise CloudAlreadyExists(dst)
            await self.delete(dst)
        entry = self._entry(src)
        if entry is not None and entry.state != PENDING_DELETE:
            return await self._move_entry(entry, src, dst, parent)
        item = await self._item_for_path(src)
        if item is None:
            raise CloudNotFound(src)
        await self._retry_while_not_validated(
            lambda: self.api.rename_or_move(item, basename(dst), parent.id),
            budget_seconds=self.settings.upload_overwrite_retry_seconds,
        )
        moved = replace(item, name=basename(dst), parent_id=parent.id)
        self._path_to_item.pop(src, None)
        self._path_to_item[dst] = moved
        await self.cache.invalidate(src, dst, parent_path(src), parent_path(dst))
        metadata = to_cloud_metadata(moved, dst)
        await self.cache.put(metadata)
        return metadata

    # ------------------------------------------------------------------ overlay

    def _entry(self, path: str) -> Optional[OverlayEntry]:
        normalized = normalize_cloud_path(path)
        entry = self._overlay.get(normalized)
        if entry is None:
            return None
        if time.time() >= entry.expires_at:
            if entry.state == PENDING_CREATE:
                logger.warning(
                    "pending create expired without content",
                    extra={"path": normalized, "remoteId": entry.remote_id},
                )
            self._drop_entry(normalized)
            return None
        return entry

    def _overlay_children(self, folder_path: str) -> dict[str, OverlayEntry]:
        normalized = normalize_cloud_path(folder_path)
        return {
            path: entry
            for path in list(self._overlay)
            if parent_path(path) == normalized and (entry := self._entry(path)) is not None
        }

    def _drop_entry(self, path: str) -> None:
        normalized = normalize_cloud_path(path)
        entry = self._overlay.pop(normalized, None)
        self._path_to_item.pop(normalized, None)
        if entry is None or entry.local_path is None:
            return
        try:
            os.unlink(entry.local_path)
        except OSError:
            pass

    def _is_hidden_remote_id(self, remote_id: str) -> bool:
        expires_at = self._hidden_remote_ids.get(remote_id)
        if expires_at is None:
            return False
        if time.time() >= expires_at:
            self._hidden_remote_ids.pop(remote_id, None)
            return False
        return True

    def _sweep_settled_tombstones(self, folder_path: str, listed_ids: set[str]) -> None:
        for path in list(self._overlay):
            if parent_path(path) != folder_path:
                continue
            entry = self._overlay.get(path)
            if entry is None or entry.state != PENDING_DELETE or not entry.remote_deleted:
                continue
            if entry.remote_id is None or entry.remote_id not in listed_ids:
                self._drop_entry(path)
                if entry.remote_id is not None:
                    self._hidden_remote_ids.pop(entry.remote_id, None)

    def _store_pending_create(self, path: str, parent_folder_id: str, remote_target: Optional[str]) -> CloudItemMetadata:
        normalized = normalize_cloud_path(path)
        self._drop_entry(normalized)
        item = O2Item(
            id=remote_target or ("pending:create:%s" % uuid4().hex),
            name=basename(normalized),
            parent_id=parent_folder_id,
            is_folder=False,
            size=0,
            modified_at=datetime.now(timezone.utc),
        )
        metadata = to_cloud_metadata(item, normalized)
        self._overlay[normalized] = OverlayEntry(
            state=PENDING_CREATE,
            item=item,
            metadata=metadata,
            local_path=None,
            remote_id=remote_target,
            parent_folder_id=parent_folder_id,
            expires_at=time.time() + self.settings.upload_recent_cache_ttl_seconds,
        )
        self._path_to_item[normalized] = item
        logger.info("pending create stored", extra={"path": normalized, "remoteId": remote_target})
        return metadata

    def _store_uploaded(self, path: str, uploaded: O2Item, local_tmp_path: str, parent_folder_id: str) -> OverlayEntry:
        normalized = normalize_cloud_path(path)
        self._drop_entry(normalized)
        size = os.path.getsize(local_tmp_path)
        local_copy: Optional[str] = None
        if size <= self.settings.upload_recent_cache_max_file_mb * 1024 * 1024:
            target = self._spool_dir() / ("upload-%s.tmp" % uuid4().hex)
            shutil.copyfile(local_tmp_path, target)
            local_copy = str(target)
        remote_id = None if uploaded.id.startswith("pending:") else uploaded.id
        item = replace(uploaded, name=basename(normalized), parent_id=parent_folder_id, size=size)
        metadata = to_cloud_metadata(item, normalized)
        entry = OverlayEntry(
            state=UPLOADED,
            item=item,
            metadata=metadata,
            local_path=local_copy,
            remote_id=remote_id,
            parent_folder_id=parent_folder_id,
            expires_at=time.time() + self.settings.upload_recent_cache_ttl_seconds,
        )
        self._overlay[normalized] = entry
        self._path_to_item[normalized] = item
        logger.info(
            "upload stored in overlay",
            extra={"path": normalized, "remoteId": remote_id, "bytesIn": size, "localCopy": local_copy is not None},
        )
        return entry

    def _store_tombstone(self, path: str, item: O2Item, remote_id: Optional[str]) -> None:
        normalized = normalize_cloud_path(path)
        expires_at = time.time() + self.settings.delete_tombstone_ttl_seconds
        self._overlay[normalized] = OverlayEntry(
            state=PENDING_DELETE,
            item=item,
            metadata=to_cloud_metadata(item, normalized),
            local_path=None,
            remote_id=remote_id,
            parent_folder_id=item.parent_id,
            expires_at=expires_at,
        )
        self._path_to_item.pop(normalized, None)
        if remote_id is not None:
            self._hidden_remote_ids[remote_id] = expires_at

    # ------------------------------------------------------------------ remoto

    async def _remote_target_id(self, path: str, overwrite: bool) -> Optional[str]:
        entry = self._entry(path)
        if entry is not None and entry.state != PENDING_DELETE:
            if not overwrite:
                raise CloudAlreadyExists(path)
            return entry.remote_id
        existing = await self._item_for_path(path)
        if existing is None or existing.is_folder:
            return None
        if not overwrite:
            raise CloudAlreadyExists(path)
        return existing.id

    async def _upload_with_validation_retry(
        self, parent_folder_id: str, name: str, local_tmp_path: str, media_id: Optional[str]
    ) -> O2Item:
        # Sobrescribir por id falla con MED-1017 si el destino sigue en ventana de
        # validación (~4-10s); reintentar dentro del presupuesto lo cubre.
        return await self._retry_while_not_validated(
            lambda: self.api.upload_file(parent_folder_id, name, local_tmp_path, media_id=media_id),
            budget_seconds=self.settings.upload_overwrite_retry_seconds,
        )

    async def _retry_while_not_validated(self, operation: Callable[[], Coroutine], *, budget_seconds: float, delay_seconds: float = 1.5):
        deadline = time.time() + max(0.0, budget_seconds)
        while True:
            try:
                return await operation()
            except CloudMediaNotValidated:
                if time.time() + delay_seconds > deadline:
                    raise
                await asyncio.sleep(delay_seconds)

    async def _item_for_path(self, path: str) -> Optional[O2Item]:
        normalized = normalize_cloud_path(path)
        entry = self._entry(normalized)
        if entry is not None:
            return None if entry.state == PENDING_DELETE else entry.item
        if normalized in self._path_to_item:
            return self._path_to_item[normalized]
        if normalized == "/":
            root = await self.api.root_folder()
            self._path_to_item["/"] = root
            return root
        parent = await self._item_for_path(parent_path(normalized))
        if parent is None or not parent.is_folder:
            return None
        result: Optional[O2Item] = None
        for child in await self.api.list_folder(parent.id):
            if self._is_hidden_remote_id(child.id):
                continue
            child_path = _join(parent_path(normalized), child.name)
            child_entry = self._entry(child_path)
            if child_entry is not None:
                continue
            self._path_to_item[child_path] = child
            if child.name.lower() == basename(normalized).lower():
                result = child
        return result

    def _spool_dir(self) -> Path:
        return Path(self.settings.cache_dir) / "recent-uploads"

    async def _move_entry(self, entry: OverlayEntry, src: str, dst: str, parent: O2Item) -> CloudItemMetadata:
        item = replace(entry.item, name=basename(dst), parent_id=parent.id)
        metadata = to_cloud_metadata(item, dst)
        self._overlay.pop(src, None)
        self._overlay[dst] = OverlayEntry(
            state=entry.state,
            item=item,
            metadata=metadata,
            local_path=entry.local_path,
            remote_id=entry.remote_id,
            parent_folder_id=parent.id,
            expires_at=entry.expires_at,
        )
        self._path_to_item.pop(src, None)
        self._path_to_item[dst] = item
        await self.cache.invalidate(src, dst, parent_path(src), parent_path(dst))
        await self.cache.put(metadata)
        if entry.state == UPLOADED and entry.remote_id is not None:
            remote_item = replace(entry.item, id=entry.remote_id)
            self._schedule(
                self._retry_while_not_validated(
                    lambda: self.api.rename_or_move(remote_item, basename(dst), parent.id),
                    budget_seconds=self.settings.delete_tombstone_ttl_seconds,
                ),
                "o2-rename-remote",
            )
        # PENDING_CREATE nunca llegó al remoto: renombrar es puramente local.
        return metadata

    async def _delete_remote(self, path: str, item: O2Item) -> None:
        deadline = time.time() + max(1.0, float(self.settings.delete_tombstone_ttl_seconds))
        delay_seconds = 2.5
        while True:
            try:
                await self.api.move_to_trash(item)
                break
            except CloudMediaNotValidated as ex:
                if time.time() + delay_seconds > deadline:
                    logger.warning(
                        "deferred remote delete expired",
                        extra={"fileName": item.name, "remoteId": item.id, "reason": str(ex)},
                    )
                    return
                await asyncio.sleep(delay_seconds)
        normalized = normalize_cloud_path(path)
        entry = self._overlay.get(normalized)
        if entry is not None and entry.state == PENDING_DELETE:
            entry.remote_deleted = True
        logger.info("remote delete completed", extra={"fileName": item.name, "remoteId": item.id})

    async def _find_and_delete_remote(self, entry: OverlayEntry) -> None:
        if entry.parent_folder_id is None:
            return
        found = await self.api.find_child_with_retries(
            entry.parent_folder_id,
            entry.item.name,
            False,
            expected_size=entry.metadata.size,
            attempts=8,
            delay_seconds=2.5,
        )
        if found is None:
            logger.warning(
                "remote delete skipped; item without id was never confirmed",
                extra={"fileName": entry.item.name},
            )
            return
        await self._delete_remote(entry.metadata.path, found)

    def _schedule(self, coroutine: Coroutine, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_background_task)

    def _log_background_task(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("background task failed", exc_info=(type(error), error, error.__traceback__))


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
