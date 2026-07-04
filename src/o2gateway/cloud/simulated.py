from __future__ import annotations

import asyncio
import mimetypes
import os
import shutil
from pathlib import Path
from typing import AsyncIterator, Optional

from o2gateway.cloud.base import (
    ByteRange,
    CloudItemMetadata,
    CloudQuota,
    basename,
    normalize_cloud_path,
    parent_path,
)
from o2gateway.operations.errors import CloudAlreadyExists, CloudForbidden, CloudNotFound


class SimulatedCloudFileStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._seed()

    async def list(self, path: str) -> list[CloudItemMetadata]:
        local = self._to_local(path)
        if not local.exists() or not local.is_dir():
            raise CloudNotFound(path)
        items = [self._metadata(child) for child in sorted(local.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))]
        return items

    async def get_metadata(self, path: str) -> Optional[CloudItemMetadata]:
        local = self._to_local(path)
        if not local.exists():
            return None
        return self._metadata(local)

    async def open_read(self, path: str, byte_range: ByteRange = None) -> AsyncIterator[bytes]:
        local = self._to_local(path)
        if not local.exists() or local.is_dir():
            raise CloudNotFound(path)
        start, end = _normalize_range(byte_range, local.stat().st_size)
        with local.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    async def create_folder(self, path: str) -> CloudItemMetadata:
        async with self._lock:
            local = self._to_local(path)
            if local.exists():
                raise CloudAlreadyExists(path)
            parent = local.parent
            if not parent.exists() or not parent.is_dir():
                raise CloudNotFound(str(parent))
            local.mkdir()
            return self._metadata(local)

    async def upload(self, path: str, local_tmp_path: str, *, overwrite: bool = True) -> CloudItemMetadata:
        async with self._lock:
            dest = self._to_local(path)
            if dest.exists() and dest.is_dir():
                raise CloudAlreadyExists(path)
            if dest.exists() and not overwrite:
                raise CloudAlreadyExists(path)
            if not dest.parent.exists():
                raise CloudNotFound(parent_path(path))
            shutil.copyfile(local_tmp_path, dest)
            return self._metadata(dest)

    async def move(self, source: str, destination: str, *, overwrite: bool = False) -> CloudItemMetadata:
        async with self._lock:
            src = self._to_local(source)
            dst = self._to_local(destination)
            if not src.exists():
                raise CloudNotFound(source)
            if dst.exists():
                if not overwrite:
                    raise CloudAlreadyExists(destination)
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            if not dst.parent.exists():
                raise CloudNotFound(parent_path(destination))
            if src.is_dir() and _is_relative_to(dst, src):
                raise CloudForbidden("cannot move a folder into itself")
            shutil.move(str(src), str(dst))
            return self._metadata(dst)

    async def delete(self, path: str, *, soft_delete: bool = True) -> None:
        async with self._lock:
            local = self._to_local(path)
            if not local.exists():
                raise CloudNotFound(path)
            if local == self.root:
                raise CloudForbidden("cannot delete root")
            if local.is_dir():
                shutil.rmtree(local)
            else:
                local.unlink()

    async def quota(self) -> CloudQuota:
        used = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                used += path.stat().st_size
        total = 10 * 1024 * 1024 * 1024 * 1024
        return CloudQuota(used_bytes=used, total_bytes=total, free_bytes=max(0, total - used))

    def _to_local(self, path: str) -> Path:
        normalized = normalize_cloud_path(path)
        if normalized == "/":
            return self.root
        local = (self.root / normalized.strip("/")).resolve()
        root = self.root.resolve()
        if not _is_relative_to(local, root):
            raise CloudForbidden("path escapes simulated root")
        return local

    def _metadata(self, path: Path) -> CloudItemMetadata:
        stat = path.stat()
        cloud_path = "/" if path == self.root else "/" + path.relative_to(self.root).as_posix()
        is_dir = path.is_dir()
        content_type = None if is_dir else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = None if is_dir else stat.st_size
        modified = _from_timestamp(stat.st_mtime)
        return CloudItemMetadata(
            id=path.relative_to(self.root).as_posix() if path != self.root else "root",
            name=basename(cloud_path),
            type="folder" if is_dir else "file",
            path=cloud_path,
            size=size,
            created_at=_from_timestamp(stat.st_ctime),
            modified_at=modified,
            content_type=content_type,
            etag='W/"%s-%s-%s"' % (path.relative_to(self.root).as_posix() if path != self.root else "root", size or 0, int(stat.st_mtime)),
            raw={"provider": "simulated"},
        )

    def _seed(self) -> None:
        sample_dir = self.root / "Fotos"
        sample_dir.mkdir(parents=True, exist_ok=True)
        sample_file = self.root / "README.txt"
        if not sample_file.exists():
            sample_file.write_text("O2Cloud WebDAV Gateway simulated backend\n", encoding="utf-8")


def _from_timestamp(value: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, timezone.utc)


def _normalize_range(byte_range: ByteRange, size: int) -> tuple[int, int]:
    if size <= 0:
        return 0, -1
    if byte_range is None:
        return 0, size - 1
    start, end = byte_range
    if start < 0:
        start = max(0, size + start)
    if end is None or end >= size:
        end = size - 1
    if start >= size:
        return size, size - 1
    if end < start:
        return start, start - 1
    return start, end


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

