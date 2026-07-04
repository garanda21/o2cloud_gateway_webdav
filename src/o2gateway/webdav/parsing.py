from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlparse

from fastapi import Request

from o2gateway.cloud.base import ByteRange, normalize_cloud_path


def cloud_path_from_request(path_base: str, request_path: str) -> str:
    base = path_base.rstrip("/")
    value = request_path
    if base and value.startswith(base):
        value = value[len(base) :]
    return normalize_cloud_path(unquote(value))


def href_for_cloud_path(path_base: str, cloud_path: str, is_folder: bool) -> str:
    base = path_base.rstrip("/")
    if cloud_path == "/":
        href = base or "/"
    else:
        parts = [quote_segment(part) for part in cloud_path.strip("/").split("/")]
        href = (base or "") + "/" + "/".join(parts)
    if is_folder and not href.endswith("/"):
        href += "/"
    return href or "/"


def quote_segment(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def parse_depth(value: Optional[str]) -> str:
    if value is None:
        return "infinity"
    normalized = value.strip().lower()
    if normalized in ("0", "1", "infinity"):
        return normalized
    return "0"


def parse_range(value: Optional[str]) -> ByteRange:
    if not value:
        return None
    match = re.match(r"bytes=(\d*)-(\d*)$", value.strip())
    if not match:
        return None
    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        return None
    if not start_raw:
        return (-int(end_raw), None)
    return (int(start_raw), int(end_raw) if end_raw else None)


def destination_to_cloud_path(request: Request, path_base: str) -> str:
    raw = request.headers.get("destination")
    if not raw:
        raise ValueError("missing Destination header")
    parsed = urlparse(raw)
    path = parsed.path if parsed.scheme else raw
    return cloud_path_from_request(path_base, path)


def overwrite_enabled(value: Optional[str]) -> bool:
    return (value or "T").upper() != "F"


def timeout_seconds(value: Optional[str]) -> int:
    if not value:
        return 3600
    for part in value.split(","):
        part = part.strip().lower()
        if part.startswith("second-"):
            try:
                return int(part.split("-", 1)[1])
            except ValueError:
                return 3600
    return 3600


def lock_token(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.strip().strip("<>")
