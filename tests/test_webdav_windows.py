import pytest

from o2gateway.webdav import xml
from o2gateway.webdav.locks import WebDavLockService


class RecordingDatabase:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query: str, args: tuple = ()) -> None:
        self.executed.append((query, args))


def test_windows_proppatch_multistatus_accepts_requested_properties():
    body = b"""\
<D:propertyupdate xmlns:D="DAV:" xmlns:Z="urn:schemas-microsoft-com:">
  <D:set><D:prop><Z:Win32FileAttributes>00000020</Z:Win32FileAttributes></D:prop></D:set>
</D:propertyupdate>
"""

    payload = xml.proppatch_multistatus("/dav/file.txt", body).decode("utf-8")

    assert "Win32FileAttributes" in payload
    assert "HTTP/1.1 200 OK" in payload
    assert "/dav/file.txt" in payload


@pytest.mark.asyncio
async def test_release_path_removes_resource_lock():
    database = RecordingDatabase()
    locks = WebDavLockService(database)  # type: ignore[arg-type]

    await locks.release_path("/file.txt")

    assert database.executed[-1] == (
        "delete from locks where path = ?",
        ("/file.txt",),
    )
