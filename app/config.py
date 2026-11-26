"""Paths and runtime configuration.

Everything the app writes at runtime lives under ``data/`` next to the
repository so the code folder can be updated from git without touching
the database, uploaded logos or import staging files.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = ROOT_DIR / "app"
# RDR_DATA_DIR lets the data live elsewhere (e.g. a local disk when the code is on a share).
DATA_DIR = Path(os.environ.get("RDR_DATA_DIR") or (ROOT_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
IMPORT_DIR = DATA_DIR / "imports"
DB_PATH = DATA_DIR / "rdr.db"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8080,
    "open_browser": True,
}

# Exit code the launcher (run.py) treats as "restart me".
RESTART_EXIT_CODE = 3


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, IMPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def write_private(path: Path, text: str) -> None:
    """Write a file only this account can read (keys, tokens, password hashes), replacing it in one step."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(tmp, 0o600)   # an older file keeps its old mode otherwise
    except OSError:
        pass
    os.replace(tmp, path)


_cache_lock = threading.Lock()
_cache: tuple[tuple[int, int], dict] | None = None


def load_config() -> dict:
    """The saved configuration. Read on every request, so it is cached until the file changes."""
    global _cache
    ensure_dirs()
    try:
        st = CONFIG_PATH.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return dict(DEFAULT_CONFIG)
    with _cache_lock:
        if _cache is None or _cache[0] != stamp:
            cfg = dict(DEFAULT_CONFIG)
            try:
                loaded = json.loads(CONFIG_PATH.read_text("utf-8"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except (OSError, ValueError):
                pass
            _cache = (stamp, cfg)
        return copy.deepcopy(_cache[1])


def save_config(cfg: dict) -> None:
    global _cache
    ensure_dirs()
    with _cache_lock:
        write_private(CONFIG_PATH, json.dumps(cfg, indent=2))
        _cache = None


def is_hub() -> bool:
    """This server is the hub (many events, offsite copy of every site node) rather than an RDR for one event."""
    role = (os.environ.get("RDR_ROLE") or load_config().get("role") or "").strip().lower()
    return role == "hub"


HUB_DIR = DATA_DIR / "hub"            # hub: hub.db and one folder per event
NODE_DIR = DATA_DIR / "node"          # site node: the copy last agreed with the hub, and the sync state
