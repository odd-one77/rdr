"""Update the application from git without reinstalling.

Runs ``git pull --ff-only`` in the repository, reinstalls requirements into
the current interpreter, and asks the launcher to restart the server.
Output of the last run is kept in memory so the settings page can show it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime

from .. import __version__
from ..config import RESTART_EXIT_CODE, ROOT_DIR

_last_output: list[str] = []
_lock = threading.Lock()


def _run(args: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(
            args, cwd=ROOT_DIR, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{args[0]} is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return 124, "Timed out"


def git_available() -> bool:
    return (ROOT_DIR / ".git").exists() and _run(["git", "--version"])[0] == 0


def status(fetch: bool = False) -> dict:
    info = {"version": __version__, "git": False, "python": sys.version.split()[0], "root": str(ROOT_DIR)}
    if not git_available():
        info["error"] = "Not a git checkout (or git is not installed). Update from git is unavailable."
        return info
    info["git"] = True

    def _val(args: list[str], fallback: str) -> str:
        code, out = _run(args)
        return out if code == 0 and out else fallback

    info["commit"] = _val(["git", "rev-parse", "--short", "HEAD"], "none yet")
    info["branch"] = _val(["git", "rev-parse", "--abbrev-ref", "HEAD"], "main")
    info["last_commit"] = _val(["git", "log", "-1", "--format=%ci  %s"], "no commits yet")
    info["remote"] = _val(["git", "remote", "get-url", "origin"], "no remote configured")
    if fetch:
        code, out = _run(["git", "fetch", "--quiet"], timeout=60)
        info["fetch_ok"] = code == 0
        if code != 0:
            info["fetch_error"] = out
    code, out = _run(["git", "rev-list", "--count", "HEAD..@{u}"])
    info["behind"] = int(out) if code == 0 and out.isdigit() else None
    code, out = _run(["git", "status", "--porcelain", "--untracked-files=no"])
    info["dirty"] = bool(out.strip()) if code == 0 else False
    return info


def last_output() -> list[str]:
    with _lock:
        return list(_last_output)


def update(restart: bool = True) -> dict:
    """Pull, reinstall requirements, and (optionally) restart. Returns a report."""
    lines = [f"== Update started {datetime.now():%Y-%m-%d %H:%M:%S}"]
    ok = True
    code, out = _run(["git", "pull", "--ff-only"], timeout=180)
    lines += [f"$ git pull --ff-only", out or "(no output)"]
    if code != 0:
        ok = False
        lines.append("!! git pull failed. Local changes may be blocking a fast-forward.")
    else:
        req = ROOT_DIR / "requirements.txt"
        if req.exists():
            code, out = _run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)], timeout=600)
            lines += ["$ pip install -r requirements.txt", out or "(up to date)"]
            if code != 0:
                ok = False
                lines.append("!! pip install failed")
    lines.append("== Update finished " + ("OK" if ok else "with errors"))
    with _lock:
        _last_output[:] = lines
    if ok and restart:
        schedule_restart()
    return {"ok": ok, "lines": lines, "restarting": ok and restart}


def schedule_restart(delay: float = 0.8) -> None:
    """Exit with the restart code once the current response has been sent."""
    def _exit():
        os._exit(RESTART_EXIT_CODE)
    threading.Timer(delay, _exit).start()
