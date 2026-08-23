from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional


PACKAGE_NAME = "o2cloud-webdav-gateway"


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: Optional[str]


def get_build_info(configured_version: Optional[str] = None, configured_commit: Optional[str] = None) -> BuildInfo:
    return BuildInfo(
        version=(configured_version or _package_version()).removeprefix("v"),
        commit=_normalize_commit(configured_commit or os.environ.get("GIT_COMMIT") or _local_git_commit()),
    )


def _package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        from o2gateway import __version__

        return __version__


def _local_git_commit() -> Optional[str]:
    repository = Path(__file__).resolve().parents[3]
    if not (repository / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _normalize_commit(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip()
    if not normalized or normalized.lower() in {"unknown", "none", "null"}:
        return None
    return normalized[:12]
