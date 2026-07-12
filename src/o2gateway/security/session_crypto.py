from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

from o2gateway.settings import read_secret


class SecretBox:
    def __init__(self, key_file: Optional[str]) -> None:
        raw = read_secret(None, key_file)
        self.fernet = _fernet_from_raw(raw) if raw else None

    @property
    def enabled(self) -> bool:
        return self.fernet is not None

    def encrypt(self, data: bytes) -> bytes:
        if self.fernet is None:
            return data
        return self.fernet.encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        if self.fernet is None:
            return data
        return self.fernet.decrypt(data)


def _fernet_from_raw(raw: str) -> Fernet:
    value = raw.strip()
    try:
        return Fernet(value.encode("utf-8"))
    except Exception:
        digest = base64.urlsafe_b64encode(value.encode("utf-8").ljust(32, b"0")[:32])
        return Fernet(digest)


def write_private(path: str, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).chmod(0o600)
        os.replace(temporary, target)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
