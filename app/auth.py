"""Admin password and signed cookie sessions.

* Admin: a single user-defined password (PBKDF2 hashed in settings).
* Tech: admin-defined usernames with no password; the kiosk asks the tech to
  pick who they are and which site they are at. That choice is kept in a
  signed cookie so every transaction records who did it and where.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from typing import Optional

from fastapi import Request
from itsdangerous import BadSignature, URLSafeSerializer

from .config import DATA_DIR, ensure_dirs
from .db import get_setting, set_setting

ADMIN_COOKIE = "rdr_admin"
TECH_COOKIE = "rdr_tech"
_ITER = 200_000


def _secret_key() -> str:
    ensure_dirs()
    path = DATA_DIR / "secret.key"
    if path.exists():
        return path.read_text("utf-8").strip()
    key = secrets.token_urlsafe(32)
    path.write_text(key, "utf-8")
    return key


_serializer: URLSafeSerializer | None = None


def serializer() -> URLSafeSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeSerializer(_secret_key(), salt="rdr-session")
    return _serializer


# -- password -----------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITER)
    return f"pbkdf2${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


def admin_password_set(db: sqlite3.Connection) -> bool:
    return bool(get_setting(db, "admin_password_hash"))


def set_admin_password(db: sqlite3.Connection, password: str) -> None:
    set_setting(db, "admin_password_hash", hash_password(password))


def check_admin_password(db: sqlite3.Connection, password: str) -> bool:
    stored = get_setting(db, "admin_password_hash")
    return bool(stored) and verify_password(password, stored)


# -- sessions -----------------------------------------------------------------

def admin_token() -> str:
    return serializer().dumps({"admin": True, "nonce": secrets.token_hex(8)})


def is_admin(request: Request) -> bool:
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw:
        return False
    try:
        data = serializer().loads(raw)
    except BadSignature:
        return False
    return bool(data.get("admin"))


def tech_token(tech_id: int, site_id: int) -> str:
    return serializer().dumps({"tech_id": tech_id, "site_id": site_id})


def current_tech(request: Request) -> Optional[dict]:
    raw = request.cookies.get(TECH_COOKIE)
    if not raw:
        return None
    try:
        data = serializer().loads(raw)
    except BadSignature:
        return None
    if "tech_id" not in data or "site_id" not in data:
        return None
    return {"tech_id": int(data["tech_id"]), "site_id": int(data["site_id"])}
