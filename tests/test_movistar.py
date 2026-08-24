from pathlib import Path
from types import SimpleNamespace

import pytest

from o2gateway.o2.movistar import MovistarCloudApiClient


class FakeMovistarClient(MovistarCloudApiClient):
    def __init__(self) -> None:
        self.settings = SimpleNamespace(o2_api_base_url="https://micloud.movistar.es/sapi/")
        self.pictures = [
            {
                "id": "picture-%03d" % index,
                "name": "photo-%03d.JPG" % index,
                "folder": 10,
                "size": 1024 + index,
                "url": "/download/picture-%03d" % index,
                "mediatype": "picture",
            }
            for index in range(201)
        ]
        self.pictures.append(
            {
                "id": "other-folder",
                "name": "elsewhere.JPG",
                "folder": 11,
                "size": 42,
                "url": "/download/other-folder",
                "mediatype": "picture",
            }
        )

    async def _json(self, method, resource, query, body=None, form=False):
        assert method == "POST"
        if resource == "media":
            return {"data": {"media": [{"id": "raw-1"}], "more": False}}
        if resource == "media/file":
            return {
                "data": {
                    "mediaserverurl": "https://micloud.movistar.es",
                    "files": [
                        {
                            "id": "raw-1",
                            "name": "photo-raw.ARW",
                            "folder": 10,
                            "size": 2048,
                            "url": "/download/raw-1",
                            "mediatype": "file",
                        }
                    ],
                }
            }
        if resource == "media/picture":
            offset = int(query["offset"])
            limit = int(query["limit"])
            return {
                "data": {
                    "mediaserverurl": "https://micloud.movistar.es",
                    "pictures": self.pictures[offset : offset + limit],
                }
            }
        raise AssertionError("unexpected resource %s" % resource)


@pytest.mark.asyncio
async def test_movistar_lists_paginated_pictures_alongside_generic_files():
    client = FakeMovistarClient()

    items = await client._load_files("10")

    extensions = [Path(item.name).suffix.upper() for item in items]
    assert extensions.count(".ARW") == 1
    assert extensions.count(".JPG") == 201
    assert all(item.parent_id == "10" for item in items)
    assert all(item.id != "other-folder" for item in items)
