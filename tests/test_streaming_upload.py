from __future__ import annotations

import json

import httpx

from o2gateway.o2.api import O2CloudApiClient, _StreamingMultipart
from o2gateway.o2.session import O2Session
from o2gateway.settings import Settings


async def test_streaming_multipart_tees_chunks_and_has_exact_content_length(tmp_path):
    source_events: list[str] = []

    async def source():
        source_events.append("first")
        yield b"hello "
        source_events.append("second")
        yield b"world"

    spool = tmp_path / "upload.tmp"
    stream = _StreamingMultipart(
        source(),
        str(spool),
        11,
        {"name": "hello.txt", "size": 11, "folderid": "root"},
        "hello.txt",
        "text/plain",
    )

    iterator = stream.__aiter__()
    prefix = await anext(iterator)
    first = await anext(iterator)

    assert source_events == ["first"]
    assert first == b"hello "
    assert stream.written == 6

    remainder = [chunk async for chunk in iterator]
    body = prefix + first + b"".join(remainder)
    assert source_events == ["first", "second"]
    assert spool.read_bytes() == b"hello world"
    assert len(body) == int(stream.headers["Content-Length"])
    assert b'filename="hello.txt"' in body
    assert json.dumps({"data": {"name": "hello.txt", "size": 11, "folderid": "root"}}, separators=(",", ":")).encode() in body


async def test_streaming_multipart_can_finish_spool_after_early_stop(tmp_path):
    async def source():
        yield b"first"
        yield b"second"

    spool = tmp_path / "upload.tmp"
    stream = _StreamingMultipart(source(), str(spool), 11, {}, "x.bin", "application/octet-stream")
    iterator = stream.__aiter__()
    await anext(iterator)  # multipart prefix
    await anext(iterator)  # first file chunk
    await iterator.aclose()

    await stream.finish_spooling()

    assert stream.complete
    assert spool.read_bytes() == b"firstsecond"


async def test_api_streams_multipart_to_provider_and_spools_body(tmp_path):
    received: list[bytes] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        received.append(await request.aread())
        return httpx.Response(200, json={"data": {"id": "remote-1", "name": "photo.jpg", "size": 11}})

    class SessionStore:
        def read(self):
            return O2Session(validation_key="validation-key")

        def save(self, session):
            pass

    client = O2CloudApiClient(Settings(), SessionStore())  # type: ignore[arg-type]
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    spool = tmp_path / "api-upload.tmp"

    async def source():
        yield b"hello "
        yield b"world"

    try:
        item = await client.upload_file_stream("root", "photo.jpg", source(), 11, str(spool))
    finally:
        await client.close()

    assert item.id == "remote-1"
    assert item.size == 11
    assert spool.read_bytes() == b"hello world"
    assert len(received) == 1
    assert b"hello world" in received[0]
    assert b'filename="photo.jpg"' in received[0]
