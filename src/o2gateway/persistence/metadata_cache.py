from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Optional

from o2gateway.cloud.base import CloudItemMetadata, normalize_cloud_path, parent_path
from o2gateway.persistence.db import Database


class MetadataCache:
    def __init__(self, db: Database, ttl_seconds: int, negative_ttl_seconds: int) -> None:
        self.db = db
        self.ttl_seconds = ttl_seconds
        self.negative_ttl_seconds = negative_ttl_seconds

    async def get(self, path: str) -> Optional[CloudItemMetadata]:
        row = await self.db.fetchone(
            "select payload, expires_at from metadata_cache where path = ?",
            (normalize_cloud_path(path),),
        )
        if row is None or row["expires_at"] < time.time():
            return None
        payload = json.loads(row["payload"])
        if payload.get("negative"):
            return None
        return _decode_metadata(payload)

    async def put(self, item: CloudItemMetadata) -> None:
        now = time.time()
        await self.db.execute(
            "insert or replace into metadata_cache(path, payload, expires_at, last_seen_at) values (?, ?, ?, ?)",
            (item.path, json.dumps(_encode_metadata(item), ensure_ascii=False), now + self.ttl_seconds, now),
        )

    async def put_negative(self, path: str) -> None:
        now = time.time()
        await self.db.execute(
            "insert or replace into metadata_cache(path, payload, expires_at, last_seen_at) values (?, ?, ?, ?)",
            (normalize_cloud_path(path), json.dumps({"negative": True}), now + self.negative_ttl_seconds, now),
        )

    async def invalidate(self, *paths: str) -> None:
        normalized = {normalize_cloud_path(path) for path in paths}
        normalized.update(parent_path(path) for path in list(normalized))
        for path in normalized:
            await self.db.execute("delete from metadata_cache where path = ?", (path,))

    async def clear(self) -> None:
        await self.db.execute("delete from metadata_cache")

    async def count(self) -> int:
        row = await self.db.fetchone("select count(*) as count from metadata_cache")
        return int(row["count"]) if row is not None else 0


def _encode_metadata(item: CloudItemMetadata) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "type": item.type,
        "path": item.path,
        "size": item.size,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "modified_at": item.modified_at.isoformat() if item.modified_at else None,
        "content_type": item.content_type,
        "etag": item.etag,
        "raw": dict(item.raw),
    }


def _decode_metadata(payload: dict) -> CloudItemMetadata:
    return CloudItemMetadata(
        id=payload["id"],
        name=payload["name"],
        type=payload["type"],
        path=payload["path"],
        size=payload.get("size"),
        created_at=_parse_dt(payload.get("created_at")),
        modified_at=_parse_dt(payload.get("modified_at")),
        content_type=payload.get("content_type"),
        etag=payload.get("etag"),
        raw=payload.get("raw") or {},
    )


def _parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value)

