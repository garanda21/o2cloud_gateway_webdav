from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


SENSITIVE_PATTERNS = [
    re.compile(r"(validationkey=)[^&\s]+", re.IGNORECASE),
    re.compile(r"((?:[?&])(?:k|token|downloadtoken|downloadToken|playbacktoken|playbackToken|access_token|accessToken)=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(authorization:\s*)[^\n\r]+", re.IGNORECASE),
    re.compile(r"(cookie:\s*)[^\n\r]+", re.IGNORECASE),
    re.compile(r"(set-cookie:\s*)[^\n\r]+", re.IGNORECASE),
    re.compile(r"(password[\"'\s:=]+)[^,\"'\s]+", re.IGNORECASE),
]

SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "validationkey",
    "validation_key",
    "validationKey",
    "k",
    "token",
    "downloadtoken",
    "downloadToken",
    "playbacktoken",
    "playbackToken",
    "access_token",
    "accessToken",
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        is_cookie = {"name", "value", "domain", "path"}.issubset(value.keys())
        return {
            key: ("***" if key in SENSITIVE_KEYS or (is_cookie and key == "value") else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for pattern in SENSITIVE_PATTERNS:
        text = pattern.sub(r"\1***", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if hasattr(record, "operation_id"):
            payload["operationId"] = getattr(record, "operation_id")
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str, log_file: str) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter())
    root.addHandler(stream)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
