from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import AsyncIterator, Mapping, Optional, Protocol, Tuple


ByteRange = Optional[Tuple[int, Optional[int]]]


@dataclass(frozen=True)
class CloudItemMetadata:
    id: str
    name: str
    type: str
    path: str
    size: Optional[int] = None
    created_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    content_type: Optional[str] = None
    etag: Optional[str] = None
    raw: Mapping[str, object] = field(default_factory=dict)

    @property
    def is_folder(self) -> bool:
        return self.type == "folder"


@dataclass(frozen=True)
class CloudQuota:
    used_bytes: int
    total_bytes: int
    free_bytes: int


class CloudFileStore(Protocol):
    async def list(self, path: str) -> list[CloudItemMetadata]:
        ...

    async def get_metadata(self, path: str) -> Optional[CloudItemMetadata]:
        ...

    async def open_read(self, path: str, byte_range: ByteRange = None) -> AsyncIterator[bytes]:
        ...

    async def create_folder(self, path: str) -> CloudItemMetadata:
        ...

    async def upload(self, path: str, local_tmp_path: str, *, overwrite: bool = True) -> CloudItemMetadata:
        ...

    async def move(self, source: str, destination: str, *, overwrite: bool = False) -> CloudItemMetadata:
        ...

    async def delete(self, path: str, *, soft_delete: bool = True) -> None:
        ...

    async def quota(self) -> CloudQuota:
        ...


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_cloud_path(path: str) -> str:
    if not path:
        return "/"
    value = "/" + path.replace("\\", "/").strip("/")
    parts = []
    for part in PurePosixPath(value).parts:
        if part in ("", "/"):
            continue
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def parent_path(path: str) -> str:
    normalized = normalize_cloud_path(path)
    if normalized == "/":
        return "/"
    parent = str(PurePosixPath(normalized).parent)
    return "/" if parent == "." else parent


def basename(path: str) -> str:
    normalized = normalize_cloud_path(path)
    return "" if normalized == "/" else PurePosixPath(normalized).name

