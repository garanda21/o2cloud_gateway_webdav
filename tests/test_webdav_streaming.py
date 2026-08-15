from __future__ import annotations

from starlette.requests import Request

from o2gateway.cloud.base import CloudItemMetadata
from o2gateway.settings import Settings
from o2gateway.webdav.router import _put


class UploadStore:
    def __init__(self) -> None:
        self.streamed = bytearray()
        self.stream_calls = 0
        self.file_calls = 0

    async def get_metadata(self, path):
        return None

    async def upload_stream(self, path, chunks, size, local_tmp_path, *, overwrite=True):
        self.stream_calls += 1
        with open(local_tmp_path, "wb") as handle:
            async for chunk in chunks:
                self.streamed.extend(chunk)
                handle.write(chunk)
        assert len(self.streamed) == size
        return _metadata(path, size)

    async def upload(self, path, local_tmp_path, *, overwrite=True):
        self.file_calls += 1
        with open(local_tmp_path, "rb") as handle:
            content = handle.read()
        return _metadata(path, len(content))


async def test_put_uses_streaming_store_when_content_length_is_known(tmp_path):
    store = UploadStore()
    request = _request([b"hello ", b"world"], content_length=11)
    settings = Settings(upload_spool_dir=str(tmp_path), upload_max_file_mb=1)

    response = await _put(settings, store, "/hello.txt", request, "test-op")  # type: ignore[arg-type]

    assert response.status_code == 201
    assert store.stream_calls == 1
    assert store.file_calls == 0
    assert store.streamed == b"hello world"


async def test_put_falls_back_to_file_upload_without_content_length(tmp_path):
    store = UploadStore()
    request = _request([b"chunked body"])
    settings = Settings(upload_spool_dir=str(tmp_path), upload_max_file_mb=1)

    response = await _put(settings, store, "/chunked.txt", request, "test-op")  # type: ignore[arg-type]

    assert response.status_code == 201
    assert store.stream_calls == 0
    assert store.file_calls == 1


def _request(parts: list[bytes], content_length: int | None = None) -> Request:
    messages = [
        {"type": "http.request", "body": part, "more_body": index < len(parts) - 1}
        for index, part in enumerate(parts)
    ]

    async def receive():
        return messages.pop(0)

    headers = [] if content_length is None else [(b"content-length", str(content_length).encode("ascii"))]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": "/dav/file",
            "raw_path": b"/dav/file",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 80),
        },
        receive,
    )


def _metadata(path: str, size: int) -> CloudItemMetadata:
    return CloudItemMetadata(id="id", name=path.rsplit("/", 1)[-1], type="file", path=path, size=size)
