from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "docker" / "xdisplay.sh"


def _prepare(display: str, runtime_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/sh",
            "-c",
            '. "$1"; x_prepare_display "$2" "$3"',
            "sh",
            str(SCRIPT),
            display,
            str(runtime_dir),
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def _artifacts(runtime_dir: Path, display_number: int, lock_pid: str) -> tuple[Path, Path]:
    socket_dir = runtime_dir / ".X11-unix"
    socket_dir.mkdir()
    lock_file = runtime_dir / f".X{display_number}-lock"
    socket_file = socket_dir / f"X{display_number}"
    lock_file.write_text(lock_pid, encoding="ascii")
    socket_file.touch()
    return lock_file, socket_file


def test_prepare_removes_stale_lock_and_socket(tmp_path: Path) -> None:
    lock_file, socket_file = _artifacts(tmp_path, 99, "99999999\n")

    result = _prepare(":99", tmp_path)

    assert result.returncode == 0
    assert "Removing stale X display artifacts" in result.stderr
    assert not lock_file.exists()
    assert not socket_file.exists()


def test_prepare_refuses_to_remove_live_display(tmp_path: Path) -> None:
    lock_file, socket_file = _artifacts(tmp_path, 99, f" {os.getpid()} \n")

    result = _prepare(":99", tmp_path)

    assert result.returncode == 1
    assert "already active" in result.stderr
    assert lock_file.exists()
    assert socket_file.exists()


def test_prepare_supports_display_screen_suffix(tmp_path: Path) -> None:
    lock_file, socket_file = _artifacts(tmp_path, 42, "not-a-pid\n")

    result = _prepare("localhost:42.0", tmp_path)

    assert result.returncode == 0
    assert not lock_file.exists()
    assert not socket_file.exists()


def test_prepare_rejects_invalid_display_without_deleting_files(tmp_path: Path) -> None:
    lock_file, socket_file = _artifacts(tmp_path, 99, "99999999\n")

    result = _prepare(":invalid", tmp_path)

    assert result.returncode == 2
    assert "Invalid X display" in result.stderr
    assert lock_file.exists()
    assert socket_file.exists()
