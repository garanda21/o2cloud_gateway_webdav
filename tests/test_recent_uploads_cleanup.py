"""Local copies of recent uploads must not accumulate in cache/recent-uploads (#24).

Backup tools write each file once and rarely list or read it back, so expiry
cannot depend on the same path or its folder being accessed again.
"""
from __future__ import annotations

import time

from test_o2_overlay_store import build


def _spool(tmp_path):
    return tmp_path / "cache" / "recent-uploads"


def _copies(tmp_path):
    return sorted(p.name for p in _spool(tmp_path).glob("upload-*.tmp"))


async def _upload(store, tmp_path, name, content):
    source = tmp_path / ("src-" + name)
    source.write_bytes(content)
    return await store.upload("/" + name, str(source))


async def _read(store, path):
    return b"".join([chunk async for chunk in store.open_read(path)])


async def test_expired_copies_are_removed_on_the_next_upload_without_reading_them(tmp_path, monkeypatch):
    _, store = await build(tmp_path)
    await _upload(store, tmp_path, "pack-a", b"a" * 1000)
    assert len(_copies(tmp_path)) == 1

    later = time.time() + 901  # past UPLOAD_RECENT_CACHE_TTL_SECONDS (900 in build)
    monkeypatch.setattr("o2gateway.o2.store.time.time", lambda: later)
    await _upload(store, tmp_path, "pack-b", b"b" * 1000)

    assert len(_copies(tmp_path)) == 1  # only pack-b's copy is left


async def test_copies_left_by_a_previous_process_are_removed_on_start(tmp_path):
    _spool(tmp_path).mkdir(parents=True)
    (_spool(tmp_path) / "upload-orphan.tmp").write_bytes(b"x" * 1000)
    (_spool(tmp_path) / "keep-me.txt").write_bytes(b"not ours")

    await build(tmp_path)

    assert _copies(tmp_path) == []
    assert (_spool(tmp_path) / "keep-me.txt").exists()


async def test_cache_max_size_evicts_the_oldest_copies_and_reads_fall_back_to_the_provider(tmp_path):
    _, store = await build(tmp_path, cache_max_size_mb=1)
    first = b"1" * 600_000
    await _upload(store, tmp_path, "first", first)
    await _upload(store, tmp_path, "second", b"2" * 600_000)

    total = sum(p.stat().st_size for p in _spool(tmp_path).glob("upload-*.tmp"))
    assert total <= 1024 * 1024
    assert len(_copies(tmp_path)) == 1
    # "first" lost its local copy but is still served, now from the provider.
    assert await _read(store, "/first") == first


async def test_cache_max_size_zero_keeps_the_previous_unlimited_behaviour(tmp_path):
    _, store = await build(tmp_path, cache_max_size_mb=0)
    await _upload(store, tmp_path, "first", b"1" * 600_000)
    await _upload(store, tmp_path, "second", b"2" * 600_000)

    assert len(_copies(tmp_path)) == 2
