from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Optional

from o2gateway.cloud.base import normalize_cloud_path
from o2gateway.operations.errors import CloudForbidden
from o2gateway.persistence.db import Database


@dataclass(frozen=True)
class WebDavLock:
    token: str
    path: str
    owner: str
    expires_at: float


class WebDavLockService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def cleanup(self) -> None:
        await self.db.execute("delete from locks where expires_at < ?", (time.time(),))

    async def create(self, path: str, owner: str, timeout_seconds: int = 3600) -> WebDavLock:
        await self.cleanup()
        normalized = normalize_cloud_path(path)
        existing = await self.active_for_path(normalized)
        if existing is not None:
            raise CloudForbidden("resource is locked")
        token = "opaquelocktoken:%s" % secrets.token_urlsafe(24)
        expires_at = time.time() + max(60, min(timeout_seconds, 86400))
        await self.db.execute(
            "insert into locks(token, path, owner, expires_at, created_at) values (?, ?, ?, ?, ?)",
            (token, normalized, owner, expires_at, time.time()),
        )
        return WebDavLock(token=token, path=normalized, owner=owner, expires_at=expires_at)

    async def release(self, token: str) -> bool:
        await self.cleanup()
        before = await self.db.fetchone("select token from locks where token = ?", (token,))
        if before is None:
            return False
        await self.db.execute("delete from locks where token = ?", (token,))
        return True

    async def active_for_path(self, path: str) -> Optional[WebDavLock]:
        await self.cleanup()
        row = await self.db.fetchone(
            "select token, path, owner, expires_at from locks where path = ? and expires_at >= ? order by expires_at desc limit 1",
            (normalize_cloud_path(path), time.time()),
        )
        if row is None:
            return None
        return WebDavLock(token=row["token"], path=row["path"], owner=row["owner"] or "", expires_at=row["expires_at"])

    async def assert_can_write(self, path: str, token_header: Optional[str]) -> None:
        lock = await self.active_for_path(path)
        if lock is None:
            return
        if token_header and lock.token in token_header:
            return
        raise CloudForbidden("resource is locked")

    async def list_active(self) -> list[WebDavLock]:
        await self.cleanup()
        rows = await self.db.fetchall(
            "select token, path, owner, expires_at from locks where expires_at >= ? order by path",
            (time.time(),),
        )
        return [WebDavLock(token=row["token"], path=row["path"], owner=row["owner"] or "", expires_at=row["expires_at"]) for row in rows]

