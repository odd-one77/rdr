"""Update the application in place, then restart.

Two modes, picked automatically:

* **git**     - the folder is a git checkout and git is installed (developer
                setups). Runs ``git pull --ff-only``.
* **release** - installed from the Windows setup exe (no git). Downloads the
                ``rdr-app-<version>.zip`` asset of the latest GitHub release and
                unpacks it over the install folder. The repo is private, so a
                GitHub token with read access is stored in data/config.json.

Both then reinstall requirements and ask the launcher (run.py) to restart.
Only code is touched: data/ (database, logo, config) is never written.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..config import RESTART_EXIT_CODE, ROOT_DIR, load_config

DEFAULT_REPO = "odd-one77/rdr"
# Top-level entries the release zip may replace. Anything else (data/, python/,
# .venv/) is left alone.
UPDATABLE = {"app", "run.py", "requirements.txt", "README.md", "RDR.bat", "rdr.sh", "VERSION", "installer", "tests"}

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


def mode() -> str:
    return "git" if git_available() else "release"


def _version_tuple(v: str) -> tuple:
    out = []
    for part in v.strip().lstrip("vV").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


# -- GitHub releases ----------------------------------------------------------

def _gh_request(url: str, token: str | None, accept: str = "application/vnd.github+json", timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "RDR-updater", "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def latest_release(repo: str, token: str | None) -> dict:
    data = json.loads(_gh_request(f"https://api.github.com/repos/{repo}/releases/latest", token))
    asset = next((a for a in data.get("assets", []) if a["name"].startswith("rdr-app-") and a["name"].endswith(".zip")), None)
    return {
        "tag": data.get("tag_name", ""),
        "version": data.get("tag_name", "").lstrip("vV"),
        "name": data.get("name") or data.get("tag_name"),
        "published": (data.get("published_at") or "")[:10],
        "url": data.get("html_url"),
        "asset_url": asset["url"] if asset else None,
        "asset_name": asset["name"] if asset else None,
        "asset_size": asset["size"] if asset else 0,
    }


def _describe_http_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return "GitHub refused the request (401/403). The repo is private: add a GitHub token with read access in Settings."
        if e.code == 404:
            return "No release found (404). Publish a release with an rdr-app-*.zip asset, or check the repo name/token."
        return f"GitHub returned HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"Could not reach GitHub: {e.reason}"
    return str(e)


# -- status -------------------------------------------------------------------

def status(fetch: bool = False) -> dict:
    cfg = load_config()
    info = {
        "version": __version__, "python": sys.version.split()[0], "root": str(ROOT_DIR),
        "mode": mode(), "repo": cfg.get("github_repo") or DEFAULT_REPO, "has_token": bool(cfg.get("github_token")),
        "git": False, "behind": None, "latest": None, "update_available": False,
    }
    if info["mode"] == "git":
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
        info["update_available"] = bool(info["behind"])
        code, out = _run(["git", "status", "--porcelain", "--untracked-files=no"])
        info["dirty"] = bool(out.strip()) if code == 0 else False
    elif fetch:
        try:
            rel = latest_release(info["repo"], cfg.get("github_token"))
            info["latest"] = rel
            info["update_available"] = bool(rel["asset_url"]) and _version_tuple(rel["version"]) > _version_tuple(__version__)
        except Exception as e:  # noqa: BLE001 - shown to the admin
            info["fetch_error"] = _describe_http_error(e)
    return info


def last_output() -> list[str]:
    with _lock:
        return list(_last_output)


# -- update -------------------------------------------------------------------

def _pip_install(lines: list[str]) -> bool:
    req = ROOT_DIR / "requirements.txt"
    if not req.exists():
        return True
    code, out = _run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req)], timeout=600)
    lines += ["$ pip install -r requirements.txt", out or "(up to date)"]
    if code != 0:
        lines.append("!! pip install failed")
    return code == 0


def _update_git(lines: list[str]) -> bool:
    code, out = _run(["git", "pull", "--ff-only"], timeout=180)
    lines += ["$ git pull --ff-only", out or "(no output)"]
    if code != 0:
        lines.append("!! git pull failed. Local changes may be blocking a fast-forward.")
        return False
    return _pip_install(lines)


def _update_release(lines: list[str]) -> bool:
    cfg = load_config()
    repo, token = cfg.get("github_repo") or DEFAULT_REPO, cfg.get("github_token")
    try:
        rel = latest_release(repo, token)
    except Exception as e:  # noqa: BLE001
        lines.append("!! " + _describe_http_error(e))
        return False
    lines.append(f"Latest release: {rel['tag']} ({rel['published']}); installed: v{__version__}")
    if not rel["asset_url"]:
        lines.append("!! The release has no rdr-app-*.zip asset")
        return False
    if _version_tuple(rel["version"]) <= _version_tuple(__version__):
        lines.append("Already up to date.")
        return False
    try:
        blob = _gh_request(rel["asset_url"], token, accept="application/octet-stream", timeout=300)
    except Exception as e:  # noqa: BLE001
        lines.append("!! Download failed: " + _describe_http_error(e))
        return False
    lines.append(f"Downloaded {rel['asset_name']} ({len(blob) // 1024} KB)")
    try:
        apply_zip(blob, lines)
    except Exception as e:  # noqa: BLE001
        lines.append(f"!! Could not unpack update: {e}")
        return False
    return _pip_install(lines)


def apply_zip(blob: bytes, lines: list[str] | None = None) -> int:
    """Unpack an app zip over ROOT_DIR, replacing only UPDATABLE entries. Returns files written."""
    lines = lines if lines is not None else []
    written = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        # Tolerate a single top-level folder inside the zip (e.g. "rdr-app-0.2.0/app/...").
        prefix = ""
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) == 1 and not (tops & UPDATABLE):
            prefix = tops.pop() + "/"
        # Remove old app/ tree first so deleted modules do not linger.
        if any(n[len(prefix):].startswith("app/") for n in names):
            shutil.rmtree(ROOT_DIR / "app", ignore_errors=True)
        for n in names:
            rel = n[len(prefix):]
            top = rel.split("/", 1)[0]
            if top not in UPDATABLE or ".." in rel:
                continue
            dest = ROOT_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(n) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            written += 1
    lines.append(f"Unpacked {written} files")
    return written


def update(restart: bool = True) -> dict:
    """Pull or download the newest code, reinstall requirements, and (optionally) restart."""
    lines = [f"== Update started {datetime.now():%Y-%m-%d %H:%M:%S} ({mode()} mode)"]
    ok = _update_git(lines) if mode() == "git" else _update_release(lines)
    lines.append("== Update finished " + ("OK" if ok else "with errors or nothing to do"))
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
