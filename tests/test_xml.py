from datetime import datetime, timezone

from o2gateway.cloud.base import CloudItemMetadata
from o2gateway.webdav.xml import multistatus


def test_multistatus_contains_collection_and_file_props():
    payload = multistatus(
        [
            (
                "/dav/",
                CloudItemMetadata(
                    id="root",
                    name="",
                    type="folder",
                    path="/",
                    modified_at=datetime(2026, 7, 4, tzinfo=timezone.utc),
                ),
            ),
            (
                "/dav/a.txt",
                CloudItemMetadata(
                    id="a",
                    name="a.txt",
                    type="file",
                    path="/a.txt",
                    size=3,
                    content_type="text/plain",
                    etag='W/"a"',
                ),
            ),
        ]
    )
    text = payload.decode("utf-8")
    assert "<D:multistatus" in text
    assert "<D:collection" in text
    assert "<D:getcontentlength>3</D:getcontentlength>" in text
    assert '<D:getetag>W/"a"</D:getetag>' in text

