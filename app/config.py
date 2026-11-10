"""Paths and runtime configuration.

Everything the app writes at runtime lives under ``data/`` next to the
repository so the code folder can be updated from git without touching
the database, uploaded logos or import staging files.
"""
from __future__ import annotations

import json
import os
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


def load_config() -> dict:
    ensure_dirs()
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), "utf-8")
