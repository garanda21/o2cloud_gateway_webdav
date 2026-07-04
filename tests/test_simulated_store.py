import pytest

from o2gateway.cloud.simulated import SimulatedCloudFileStore


@pytest.mark.asyncio
async def test_simulated_store_put_list_read_move_delete(tmp_path):
    store = SimulatedCloudFileStore(str(tmp_path))
    source = tmp_path / "local.txt"
    source.write_text("hello world", encoding="utf-8")

    uploaded = await store.upload("/Docs/a.txt", str(source), overwrite=True) if (tmp_path / "Docs").exists() else None
    if uploaded is None:
        await store.create_folder("/Docs")
        uploaded = await store.upload("/Docs/a.txt", str(source), overwrite=True)

    assert uploaded.path == "/Docs/a.txt"
    assert [item.name for item in await store.list("/Docs")] == ["a.txt"]

    chunks = []
    async for chunk in store.open_read("/Docs/a.txt", (0, 4)):
        chunks.append(chunk)
    assert b"".join(chunks) == b"hello"

    moved = await store.move("/Docs/a.txt", "/Docs/b.txt")
    assert moved.path == "/Docs/b.txt"
    await store.delete("/Docs/b.txt")
    assert await store.get_metadata("/Docs/b.txt") is None

