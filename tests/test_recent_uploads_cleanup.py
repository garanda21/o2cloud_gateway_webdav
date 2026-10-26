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


# --- Disk-failure cases (review of #25) ---------------------------------------

import errno
import logging
import os
import shutil

import o2gateway.o2.store as store_module

LIMIT = 1024 * 1024  # CACHE_MAX_SIZE_MB=1


def _disk_usage(tmp_path):
    return sum(p.stat().st_size for p in _spool(tmp_path).glob("upload-*.tmp"))


def _tracked_copies(store):
    paths = {e.local_path for e in store._overlay.values() if e.local_path}
    return {os.path.basename(p) for p in paths | set(store._stranded_copies)}


async def test_a_copy_larger_than_the_whole_budget_is_not_made(tmp_path):
    _, store = await build(tmp_path, cache_max_size_mb=1)
    store.settings.upload_recent_cache_max_file_mb = 10  # only the budget applies
    big = b"B" * 1_500_000

    await _upload(store, tmp_path, "big", big)

    assert _copies(tmp_path) == []
    assert await _read(store, "/big") == big  # served from the provider


async def test_room_is_made_before_copying(tmp_path, monkeypatch):
    _, store = await build(tmp_path, cache_max_size_mb=1)
    usage_when_copying = []
    real_copyfile = shutil.copyfile

    def copyfile(src, dst):
        usage_when_copying.append(_disk_usage(tmp_path) + os.path.getsize(src))
        return real_copyfile(src, dst)

    monkeypatch.setattr(store_module.shutil, "copyfile", copyfile)
    await _upload(store, tmp_path, "first", b"1" * 600_000)
    await _upload(store, tmp_path, "second", b"2" * 600_000)

    # The old copy is evicted before the new one is written, never after.
    assert usage_when_copying and all(usage <= LIMIT for usage in usage_when_copying)


async def test_a_failed_copy_is_a_cache_miss_not_an_upload_failure(tmp_path, monkeypatch):
    api, store = await build(tmp_path)
    content = b"C" * 600_000

    def copyfile_until_disk_full(src, dst):
        with open(dst, "wb") as handle:
            handle.write(content[:1000])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store_module.shutil, "copyfile", copyfile_until_disk_full)
    metadata = await _upload(store, tmp_path, "pack", content)

    assert metadata.name == "pack"  # the upload itself succeeded
    assert [call["name"] for call in api.upload_calls] == ["pack"]
    assert _copies(tmp_path) == []  # no partial orphan left behind
    monkeypatch.setattr(store_module.shutil, "copyfile", shutil.copyfile)
    assert await _read(store, "/pack") == content  # served from the provider


async def test_a_copy_that_cannot_be_deleted_is_not_forgotten(tmp_path, monkeypatch):
    _, store = await build(tmp_path, cache_max_size_mb=1)
    real_unlink = os.unlink
    stuck = set()

    def unlink(path, *args, **kwargs):
        if str(path) in stuck:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(store_module.os, "unlink", unlink)
    await _upload(store, tmp_path, "first", b"1" * 600_000)
    stuck.update(e.local_path for e in store._overlay.values() if e.local_path)

    await _upload(store, tmp_path, "second", b"2" * 600_000)
    assert _disk_usage(tmp_path) <= LIMIT  # no room made, so no second copy
    assert set(_copies(tmp_path)) == _tracked_copies(store)  # nothing untracked

    stuck.clear()  # deletion works again
    await _upload(store, tmp_path, "third", b"3" * 600_000)

    assert _disk_usage(tmp_path) <= LIMIT
    assert set(_copies(tmp_path)) == _tracked_copies(store)
    assert len(_copies(tmp_path)) == 1


async def test_startup_reports_copies_it_cannot_remove(tmp_path, monkeypatch, caplog):
    _spool(tmp_path).mkdir(parents=True)
    orphan = _spool(tmp_path) / "upload-orphan.tmp"
    orphan.write_bytes(b"x" * 1000)

    def unlink(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(store_module.os, "unlink", unlink)
    with caplog.at_level(logging.WARNING, logger="o2gateway.o2.store"):
        await build(tmp_path)

    assert orphan.exists()
    assert any("could not be removed" in record.getMessage() for record in caplog.records)


async def test_an_orphan_that_survives_startup_counts_against_the_budget_and_is_retried(tmp_path, monkeypatch):
    _spool(tmp_path).mkdir(parents=True)
    orphan = _spool(tmp_path) / "upload-orphan.tmp"
    orphan.write_bytes(b"x" * 700_000)
    real_unlink = os.unlink
    stuck = {str(orphan)}

    def unlink(path, *args, **kwargs):
        if str(path) in stuck:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(store_module.os, "unlink", unlink)
    _, store = await build(tmp_path, cache_max_size_mb=1)

    await _upload(store, tmp_path, "first", b"1" * 600_000)
    assert _disk_usage(tmp_path) <= LIMIT  # the orphan's 700 KB left no room for a copy
    assert set(_copies(tmp_path)) == _tracked_copies(store)

    stuck.clear()
    await _upload(store, tmp_path, "second", b"2" * 600_000)

    assert not orphan.exists()  # retried on the next sweep
    assert _disk_usage(tmp_path) <= LIMIT
    assert set(_copies(tmp_path)) == _tracked_copies(store)
