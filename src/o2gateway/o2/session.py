from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from o2gateway.security.session_crypto import SecretBox, write_private
from o2gateway.settings import Settings


@dataclass
class O2Cookie:
    name: str
    value: str
    domain: str = "cloud.o2online.es"
    path: str = "/"


@dataclass
class O2Session:
    validation_key: str = ""
    cookies: list[O2Cookie] = field(default_factory=list)
    user_agent: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_authenticated(self) -> bool:
        return bool(self.validation_key.strip())

    @property
    def cookie_header(self) -> str:
        return "; ".join("%s=%s" % (cookie.name, cookie.value) for cookie in self.cookies if cookie.name and cookie.value)


class O2SessionStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.o2_session_file)
        self.box = SecretBox(settings.app_encryption_key_file)

    def read(self) -> Optional[O2Session]:
        if not self.path.exists():
            encrypted = self.path.with_suffix(self.path.suffix + ".enc")
            if encrypted.exists():
                return self._read_path(encrypted)
            return None
        session = self._read_path(self.path)
        if session and self.box.enabled:
            self.save(session)
        return session

    def save(self, session: O2Session) -> None:
        payload = json.dumps(serialize_session(session), ensure_ascii=False, indent=2).encode("utf-8")
        target = self.path.with_suffix(self.path.suffix + ".enc") if self.box.enabled else self.path
        write_private(str(target), self.box.encrypt(payload))
        if target != self.path and self.path.exists():
            self.path.unlink()

    def delete(self) -> None:
        for path in [self.path, self.path.with_suffix(self.path.suffix + ".enc")]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _read_path(self, path: Path) -> Optional[O2Session]:
        try:
            raw = self.box.decrypt(path.read_bytes())
            return deserialize_session(json.loads(raw.decode("utf-8")))
        except Exception:
            if self.box.enabled and path == self.path:
                try:
                    return deserialize_session(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    return None
            return None


def serialize_session(session: O2Session) -> dict[str, Any]:
    return {
        "validationKey": session.validation_key,
        "cookies": [asdict(cookie) for cookie in session.cookies],
        "userAgent": session.user_agent,
        "createdAt": session.created_at,
    }


def deserialize_session(payload: dict[str, Any]) -> O2Session:
    cookies_payload = payload.get("cookies") or []
    cookies = []
    if isinstance(cookies_payload, dict):
        cookies = [O2Cookie(name=str(k), value=str(v)) for k, v in cookies_payload.items()]
    else:
        for item in cookies_payload:
            if isinstance(item, dict) and item.get("name") and item.get("value"):
                cookies.append(
                    O2Cookie(
                        name=str(item.get("name")),
                        value=str(item.get("value")),
                        domain=str(item.get("domain") or "cloud.o2online.es"),
                        path=str(item.get("path") or "/"),
                    )
                )
    return O2Session(
        validation_key=str(payload.get("validationKey") or payload.get("validation_key") or ""),
        cookies=cookies,
        user_agent=str(payload.get("userAgent") or payload.get("user_agent") or ""),
        created_at=str(payload.get("createdAt") or payload.get("created_at") or datetime.now(timezone.utc).isoformat()),
    )
