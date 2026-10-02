"""The entrypoint must end up with a writable HOME.

Chromium (via Playwright) needs one for its crashpad database. Without it the
assisted login dies instantly with

    chrome_crashpad_handler: --database is required
    <process did exit: exitCode=null, signal=SIGTRAP>

and the only visible symptom is a black noVNC screen, which is very hard to
diagnose. Reproduced on 2026-09-07 with rootless podman and --userns=keep-id:
the container starts directly as the host UID, so the root bootstrap that
chowns /home/o2gateway never runs, and podman injects a passwd entry whose
home is the image WORKDIR (/app) -- root-owned and unwritable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "docker" / "home.sh"


def _run(env_home: str | None, a: Path, b: Path, c: Path) -> str:
    env = dict(os.environ)
    env["FAKE_HOME_A"] = str(a)
    env["FAKE_HOME_B"] = str(b)
    env["FAKE_HOME_C"] = str(c)
    if env_home is None:
        env.pop("HOME", None)
    else:
        env["HOME"] = env_home
    # source the real helper the entrypoint uses, so the test breaks if it changes
    out = subprocess.run(
        [
            "/bin/sh",
            "-c",
            '. "$1"; ensure_writable_home "$2" "$3" "$4"',
            "sh",
            str(SCRIPT),
            env["FAKE_HOME_A"],
            env["FAKE_HOME_B"],
            env["FAKE_HOME_C"],
        ],
        capture_output=True, text=True, check=False, env=env,
    )
    return out.stdout.strip()


def test_unwritable_home_falls_back_to_the_first_writable_candidate(tmp_path):
    # /app in the real image: exists but not writable by this user
    unwritable = tmp_path / "app"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    a = tmp_path / "home-o2gateway"
    b = tmp_path / "config-home"
    c = tmp_path / "tmp-home"

    assert _run(str(unwritable), a, b, c) == str(a)
    assert a.is_dir()


def test_second_candidate_is_used_when_the_first_cannot_be_created(tmp_path):
    unwritable = tmp_path / "app"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    # a lives under a read-only parent, so mkdir -p fails
    blocked_parent = tmp_path / "blocked"
    blocked_parent.mkdir()
    blocked_parent.chmod(0o500)
    a = blocked_parent / "home-o2gateway"
    b = tmp_path / "config-home"
    c = tmp_path / "tmp-home"

    assert _run(str(unwritable), a, b, c) == str(b)
    assert b.is_dir()


def test_a_writable_home_is_left_alone(tmp_path):
    good = tmp_path / "already-good"
    good.mkdir()
    a = tmp_path / "home-o2gateway"

    assert _run(str(good), a, tmp_path / "b", tmp_path / "c") == str(good)
    assert not a.exists(), "must not create candidates when HOME already works"


def test_unset_home_still_resolves(tmp_path):
    a = tmp_path / "home-o2gateway"
    assert _run(None, a, tmp_path / "b", tmp_path / "c") == str(a)


def test_home_still_has_a_value_when_no_candidate_works(tmp_path):
    """Even on total failure HOME must not be left unset.

    An unset HOME breaks tools in a different and more confusing way than an
    unwritable one, so the helper falls back to the first candidate as a name
    even though it could not create it.
    """
    unwritable = tmp_path / "app"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)

    env = dict(os.environ)
    env["HOME"] = str(unwritable)
    out = subprocess.run(
        [
            "/bin/sh",
            "-c",
            '. "$1"; ensure_writable_home "$2" "$3" || true; printf "%s" "$HOME"',
            "sh",
            str(SCRIPT),
            str(blocked / "a"),
            str(blocked / "b"),
        ],
        capture_output=True, text=True, check=False, env=env,
    )
    assert out.stdout.strip().endswith(("/a", str(unwritable)))
    assert "no writable HOME found" in out.stderr
