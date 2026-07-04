from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Request
from fastapi.responses import Response

from o2gateway.settings import Settings, read_secret


@dataclass(frozen=True)
class LocalCredentials:
    username: str
    password: str


class LocalAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.webdav = LocalCredentials(
            settings.webdav_username,
            read_secret(settings.webdav_password, settings.webdav_password_file) or "change-me-webdav",
        )
        self.admin = LocalCredentials(
            settings.admin_username,
            read_secret(settings.admin_password, settings.admin_password_file) or "change-me-admin",
        )
        self.session_secret = (
            read_secret(None, settings.admin_session_secret_file)
            or read_secret(None, settings.app_encryption_key_file)
            or "dev-session-secret-change-me"
        ).encode("utf-8")

    def check_basic_header(self, authorization: Optional[str], credentials: LocalCredentials) -> bool:
        if not authorization or not authorization.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return False
        username, sep, password = raw.partition(":")
        if not sep:
            return False
        return hmac.compare_digest(username, credentials.username) and hmac.compare_digest(password, credentials.password)

    def require_webdav(self, request: Request) -> Optional[Response]:
        if self.check_basic_header(request.headers.get("authorization"), self.webdav):
            return None
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="O2Cloud WebDAV", charset="UTF-8"'},
        )

    def check_admin_password(self, username: str, password: str) -> bool:
        return hmac.compare_digest(username, self.admin.username) and hmac.compare_digest(password, self.admin.password)

    def create_admin_cookie(self, username: str) -> str:
        issued = str(int(time.time()))
        nonce = secrets.token_urlsafe(12)
        body = "%s:%s:%s" % (username, issued, nonce)
        sig = hmac.new(self.session_secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
        return "%s:%s" % (body, sig)

    def validate_admin_cookie(self, value: Optional[str], max_age_seconds: int = 12 * 3600) -> bool:
        if not value:
            return False
        try:
            username, issued, nonce, sig = value.split(":", 3)
            body = "%s:%s:%s" % (username, issued, nonce)
            expected = hmac.new(self.session_secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return False
            if int(issued) + max_age_seconds < time.time():
                return False
            return username == self.admin.username
        except Exception:
            return False

    def csrf_token(self, session_cookie: str) -> str:
        return hmac.new(self.session_secret, ("csrf:" + session_cookie).encode("utf-8"), hashlib.sha256).hexdigest()

    def validate_csrf(self, request: Request) -> bool:
        cookie = request.cookies.get("admin_session")
        token = request.headers.get("x-csrf-token") or request.query_params.get("csrf")
        return bool(cookie and token and hmac.compare_digest(token, self.csrf_token(cookie)))


def require_admin(auth: LocalAuth, request: Request) -> Optional[Response]:
    if auth.validate_admin_cookie(request.cookies.get("admin_session")):
        return None
    return Response(status_code=303, headers={"Location": "/admin/login"})

