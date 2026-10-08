"""Concurrent MKCOLs for the same path must create one provider folder.

The provider never rejects a duplicate folder name: it creates "name (1)".
"""
from __future__ import annotations

import asyncio

import pytest

from o2gateway.o2.api import O2Item
from o2gateway.operations.errors import CloudAlreadyExists
from test_o2_overlay_store import FakeMovistarApi, build


class FakeFolderApi(FakeMovistarApi):
    def __init__(self):
        super().__init__()
        self.folders: list[O2Item] = []

    async def list_folder(self, folder_id):
        files = await super().list_folder(folder_id)
        return [folder for folder in self.folders if folder.parent_id == folder_id] + files

    async def create_folder(self, parent_folder_id, name):
        await asyncio.sleep(0.05)  # provider round trip: lets a concurrent MKCOL interleave
        taken = {folder.name for folder in self.folders if folder.parent_id == parent_folder_id}
        final_name, counter = name, 1
        while final_name in taken:
            final_name = "%s (%d)" % (name, counter)
            counter += 1
        folder = O2Item("f%d" % (len(self.folders) + 1), final_name, parent_folder_id, True)
        self.folders.append(folder)
        return folder


async def _store(tmp_path):
    _, store = await build(tmp_path)
    api = FakeFolderApi()
    store.api = api
    return api, store


async def test_concurrent_creates_of_the_same_folder_make_one_provider_folder(tmp_path):
    api, store = await _store(tmp_path)

    results = await asyncio.gather(
        store.create_folder("/33"),
        store.create_folder("/33"),
        return_exceptions=True,
    )

    assert [folder.name for folder in api.folders] == ["33"]
    assert sum(isinstance(result, CloudAlreadyExists) for result in results) == 1


async def test_creating_an_existing_folder_is_rejected_without_calling_the_provider(tmp_path):
    api, store = await _store(tmp_path)
    await store.create_folder("/33")

    with pytest.raises(CloudAlreadyExists):
        await store.create_folder("/33")

    assert len(api.folders) == 1


async def test_different_folders_are_still_created_concurrently(tmp_path):
    api, store = await _store(tmp_path)

    await asyncio.gather(*(store.create_folder("/%02x" % index) for index in range(8)))

    assert sorted(folder.name for folder in api.folders) == ["%02x" % index for index in range(8)]
