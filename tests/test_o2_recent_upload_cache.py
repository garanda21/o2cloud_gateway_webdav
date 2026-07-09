import pytest

from o2gateway.o2.api import O2Item
from o2gateway.o2.store import O2CloudFileStore
from o2gateway.persistence.db import Database
from o2gateway.persistence.metadata_cache import MetadataCache
from o2gateway.settings import Settings


class FakeO2Api:
    def __init__(self):
        self.confirm_retries = None
        self.items = {"root": []}
        self.keep_deleted_in_list = False

    async def root_folder(self):
        return O2Item("root", "", None, True)

    async def list_folder(self, folder_id):
        return list(self.items.get(folder_id, []))

    async def upload_file(self, parent_folder_id, name, local_path, *, confirm_retries=48, confirm_delay_seconds=1.25):
        self.confirm_retries = confirm_retries
        item = O2Item("remote-a", name, parent_folder_id, False, size=0)
        self.items[parent_folder_id] = [item]
        return item

    async def download(self, item, byte_range=None):
        yield b"remote"

    async def move_to_trash(self, item):
        if self.keep_deleted_in_list:
            return
        self.items[item.parent_id] = [candidate for candidate in self.items.get(item.parent_id, []) if candidate.id != item.id]


@pytest.mark.asyncio
async def test_recent_upload_cache_overlays_unvalidated_remote_size(tmp_path):
    db = Database(str(tmp_path / "gateway.db"))
    await db.initialize()
    cache = MetadataCache(db, ttl_seconds=20, negative_ttl_seconds=5)
    settings = Settings(
        cache_dir=str(tmp_path / "cache"),
        upload_confirm_retries=1,
        upload_recent_cache_max_file_mb=1,
        upload_recent_cache_ttl_seconds=120,
    )
    api = FakeO2Api()
    store = O2CloudFileStore(api, cache, settings)  # type: ignore[arg-type]

    source = tmp_path / "a.txt"
    source.write_bytes(b"hello movistar")

    uploaded = await store.upload("/a.txt", str(source))

    assert api.confirm_retries == 1
    assert uploaded.size == len(b"hello movistar")
    assert (await store.get_metadata("/a.txt")).size == len(b"hello movistar")
    assert [item.size for item in await store.list("/")] == [len(b"hello movistar")]

    chunks = []
    async for chunk in store.open_read("/a.txt", (0, 4)):
        chunks.append(chunk)
    assert b"".join(chunks) == b"hello"


@pytest.mark.asyncio
async def test_delete_tombstone_hides_provider_stale_listing(tmp_path):
    db = Database(str(tmp_path / "gateway.db"))
    await db.initialize()
    cache = MetadataCache(db, ttl_seconds=20, negative_ttl_seconds=5)
    settings = Settings(
        cache_dir=str(tmp_path / "cache"),
        upload_confirm_retries=1,
        upload_recent_cache_max_file_mb=1,
        delete_tombstone_ttl_seconds=60,
    )
    api = FakeO2Api()
    api.keep_deleted_in_list = True
    store = O2CloudFileStore(api, cache, settings)  # type: ignore[arg-type]

    source = tmp_path / "empty.txt"
    source.write_bytes(b"")

    await store.upload("/empty.txt", str(source))
    assert await store.get_metadata("/empty.txt") is not None

    await store.delete("/empty.txt")

    assert await store.get_metadata("/empty.txt") is None
    assert [item.name for item in await store.list("/")] == []
