import asyncio
import sqlite3
import time

import pytest

from o2gateway.persistence.db import Database

INSERT = "insert or replace into metadata_cache(path, payload, expires_at, last_seen_at) values (?, ?, ?, ?)"


async def _database(tmp_path, **kwargs):
    db = Database(str(tmp_path / "gateway.db"), **kwargs)
    await db.initialize()
    return db


async def _hold_write_lock(path: str, seconds: float, locked: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def hold() -> None:
        conn = sqlite3.connect(path, timeout=0)
        conn.execute("begin immediate")
        conn.execute("insert into audit(created_at, event) values (0, 'holder')")
        loop.call_soon_threadsafe(locked.set)
        time.sleep(seconds)
        conn.commit()
        conn.close()

    await loop.run_in_executor(None, hold)


async def test_initialize_enables_wal(tmp_path):
    db = await _database(tmp_path)

    row = await db.fetchone("pragma journal_mode")

    assert row[0] == "wal"


async def test_a_write_waits_for_a_concurrent_writer_instead_of_failing(tmp_path):
    db = await _database(tmp_path, busy_timeout=5)
    locked = asyncio.Event()
    holder = asyncio.create_task(_hold_write_lock(db.path, 1.0, locked))
    await locked.wait()

    await db.execute(INSERT, ("/a", "{}", 0, 0))

    await holder


async def test_a_short_busy_timeout_reproduces_the_original_failure(tmp_path):
    db = await _database(tmp_path, busy_timeout=0.1)
    locked = asyncio.Event()
    holder = asyncio.create_task(_hold_write_lock(db.path, 1.0, locked))
    await locked.wait()

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await db.execute(INSERT, ("/a", "{}", 0, 0))

    await holder


async def test_reads_are_not_blocked_by_an_open_write_transaction(tmp_path):
    db = await _database(tmp_path, busy_timeout=0.1)
    await db.execute(INSERT, ("/a", "{}", 0, 0))
    locked = asyncio.Event()
    holder = asyncio.create_task(_hold_write_lock(db.path, 1.0, locked))
    await locked.wait()

    row = await db.fetchone("select path from metadata_cache where path = ?", ("/a",))

    assert row["path"] == "/a"
    await holder


async def test_many_concurrent_writes_all_succeed(tmp_path):
    db = await _database(tmp_path)

    await asyncio.gather(*[
        db.execute(INSERT, ("/f%03d" % index, "{}", 0, 0))
        for index in range(100)
    ])

    rows = await db.fetchall("select count(*) as n from metadata_cache")
    assert rows[0]["n"] == 100
