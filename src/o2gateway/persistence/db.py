from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                create table if not exists locks (
                    token text primary key,
                    path text not null,
                    owner text,
                    expires_at real not null,
                    created_at real not null
                );
                create index if not exists idx_locks_path on locks(path);

                create table if not exists metadata_cache (
                    path text primary key,
                    payload text not null,
                    expires_at real not null,
                    last_seen_at real not null
                );

                create table if not exists audit (
                    id integer primary key autoincrement,
                    created_at real not null,
                    operation_id text,
                    component text,
                    event text,
                    payload text
                );
                """
            )
            await db.commit()

    async def execute(self, query: str, args: tuple[Any, ...] = ()) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(query, args)
            await db.commit()

    async def fetchone(self, query: str, args: tuple[Any, ...] = ()) -> Optional[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, args)
            return await cursor.fetchone()

    async def fetchall(self, query: str, args: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, args)
            rows = await cursor.fetchall()
            return list(rows)

    async def audit(self, operation_id: str, component: str, event: str, payload: dict[str, Any]) -> None:
        await self.execute(
            "insert into audit(created_at, operation_id, component, event, payload) values (?, ?, ?, ?, ?)",
            (time.time(), operation_id, component, event, json.dumps(payload, ensure_ascii=False)),
        )

