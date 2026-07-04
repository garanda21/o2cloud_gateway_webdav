from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Iterable, Optional

from lxml import etree

from o2gateway.cloud.base import CloudItemMetadata
from o2gateway.webdav.locks import WebDavLock

DAV = "DAV:"
NSMAP = {"D": DAV}


def multistatus(items: Iterable[tuple[str, CloudItemMetadata]]) -> bytes:
    root = etree.Element("{%s}multistatus" % DAV, nsmap=NSMAP)
    for href, item in items:
        response = etree.SubElement(root, "{%s}response" % DAV)
        etree.SubElement(response, "{%s}href" % DAV).text = href
        propstat = etree.SubElement(response, "{%s}propstat" % DAV)
        prop = etree.SubElement(propstat, "{%s}prop" % DAV)
        etree.SubElement(prop, "{%s}displayname" % DAV).text = item.name
        resourcetype = etree.SubElement(prop, "{%s}resourcetype" % DAV)
        if item.is_folder:
            etree.SubElement(resourcetype, "{%s}collection" % DAV)
        if item.created_at:
            etree.SubElement(prop, "{%s}creationdate" % DAV).text = item.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if item.modified_at:
            etree.SubElement(prop, "{%s}getlastmodified" % DAV).text = format_datetime(item.modified_at.astimezone(timezone.utc), usegmt=True)
        if not item.is_folder:
            etree.SubElement(prop, "{%s}getcontentlength" % DAV).text = str(item.size or 0)
            etree.SubElement(prop, "{%s}getcontenttype" % DAV).text = item.content_type or "application/octet-stream"
            if item.etag:
                etree.SubElement(prop, "{%s}getetag" % DAV).text = item.etag
        supportedlock = etree.SubElement(prop, "{%s}supportedlock" % DAV)
        lockentry = etree.SubElement(supportedlock, "{%s}lockentry" % DAV)
        lockscope = etree.SubElement(lockentry, "{%s}lockscope" % DAV)
        etree.SubElement(lockscope, "{%s}exclusive" % DAV)
        locktype = etree.SubElement(lockentry, "{%s}locktype" % DAV)
        etree.SubElement(locktype, "{%s}write" % DAV)
        etree.SubElement(propstat, "{%s}status" % DAV).text = "HTTP/1.1 200 OK"
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def lockdiscovery(lock: WebDavLock) -> bytes:
    root = etree.Element("{%s}prop" % DAV, nsmap=NSMAP)
    discovery = etree.SubElement(root, "{%s}lockdiscovery" % DAV)
    active = etree.SubElement(discovery, "{%s}activelock" % DAV)
    locktype = etree.SubElement(active, "{%s}locktype" % DAV)
    etree.SubElement(locktype, "{%s}write" % DAV)
    lockscope = etree.SubElement(active, "{%s}lockscope" % DAV)
    etree.SubElement(lockscope, "{%s}exclusive" % DAV)
    etree.SubElement(active, "{%s}depth" % DAV).text = "0"
    owner = etree.SubElement(active, "{%s}owner" % DAV)
    owner.text = lock.owner
    etree.SubElement(active, "{%s}timeout" % DAV).text = "Second-%d" % max(0, int(lock.expires_at - _now()))
    locktoken = etree.SubElement(active, "{%s}locktoken" % DAV)
    etree.SubElement(locktoken, "{%s}href" % DAV).text = lock.token
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def error_response(message: str) -> bytes:
    root = etree.Element("{%s}error" % DAV, nsmap=NSMAP)
    etree.SubElement(root, "{%s}responsedescription" % DAV).text = message
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def _now() -> float:
    import time

    return time.time()


def http_date(value: Optional[datetime]) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    return format_datetime(value.astimezone(timezone.utc), usegmt=True)

