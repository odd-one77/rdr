"""Passwords and signed cookie sessions.

* Admin: a single user-defined password (PBKDF2 hashed in settings).
* Tech: admin-defined usernames with no password; the kiosk asks the tech to
  pick who they are and which site they are at. That choice is kept in a
  signed cookie so every transaction records who did it and where.
* Access code: one shared code a browser enters once before it sees anything,
  for when RDR is reachable from outside the room (see security.py). Its hash
  lives in config.json, so it belongs to the machine and not to the event data.

Admin and access cookies are tied to the hash they were issued for, so changing
the password or the code logs every other browser out.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from typing import Optional

from fastapi import Request
from itsdangerous import BadSignature, URLSafeSerializer

from .config import DATA_DIR, ensure_dirs, load_config, save_config, write_private
from .db import get_db, get_setting, set_setting

ADMIN_COOKIE = "rdr_admin"
TECH_COOKIE = "rdr_tech"
ACCESS_COOKIE = "rdr_access"
MIN_PASSWORD = 8          # new admin passwords and access codes; older, shorter passwords keep working
_ITER = 600_000


def _secret_key() -> str:
    ensure_dirs()
    path = DATA_DIR / "secret.key"
    if path.exists():
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path.read_text("utf-8").strip()
    key = secrets.token_urlsafe(32)
    write_private(path, key)
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


def needs_rehash(stored: str) -> bool:
    try:
        return int(stored.split("$")[1]) < _ITER
    except (ValueError, IndexError):
        return False


def _bound_to(stored: str) -> str:
    """Short fingerprint of a stored hash. A cookie carrying it stops working when the hash changes."""
    return hmac.new(serializer().secret_keys[-1], ("bind:" + (stored or "")).encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def admin_password_set(db: sqlite3.Connection) -> bool:
    return bool(get_setting(db, "admin_password_hash"))


def set_admin_password(db: sqlite3.Connection, password: str) -> None:
    set_setting(db, "admin_password_hash", hash_password(password))


def check_admin_password(db: sqlite3.Connection, password: str) -> bool:
    stored = get_setting(db, "admin_password_hash")
    return bool(stored) and verify_password(password, stored)


def upgrade_admin_hash(db: sqlite3.Connection, password: str) -> None:
    """After a correct login: move a hash made with fewer rounds up to today's count."""
    stored = get_setting(db, "admin_password_hash")
    if stored and needs_rehash(stored):
        set_admin_password(db, password)


# -- access code --------------------------------------------------------------

ACCESS_DAYS = 90


def access_code_set(cfg: dict | None = None) -> bool:
    return bool((cfg if cfg is not None else load_config()).get("access_code_hash"))


def set_access_code(code: str | None) -> None:
    cfg = load_config()
    if code:
        cfg["access_code_hash"] = hash_password(code)
    else:
        cfg.pop("access_code_hash", None)
    save_config(cfg)


def check_access_code(code: str) -> bool:
    stored = load_config().get("access_code_hash")
    return bool(stored) and verify_password(code, stored)


def access_token() -> str:
    return serializer().dumps({"access": _bound_to(load_config().get("access_code_hash", "")), "ts": int(time.time())})


def access_age(cookies: dict, cfg: dict | None = None) -> int | None:
    """Seconds since this browser entered the current access code, or None when it has not."""
    stored = (cfg if cfg is not None else load_config()).get("access_code_hash")
    raw = cookies.get(ACCESS_COOKIE)
    if not stored or not raw:
        return None
    try:
        data = serializer().loads(raw)
        age = int(time.time() - int(data.get("ts", 0)))
    except (BadSignature, ValueError, TypeError, AttributeError):
        return None
    if not 0 <= age < ACCESS_DAYS * 86400 or not hmac.compare_digest(str(data.get("access", "")), _bound_to(stored)):
        return None
    return age


def has_access(cookies: dict, cfg: dict | None = None) -> bool:
    """True when this browser has entered the current access code."""
    return access_age(cookies, cfg) is not None


# -- sessions -----------------------------------------------------------------

ADMIN_IDLE_SECONDS = 20 * 60


def request_is_https(request: Request) -> bool:
    info = getattr(request.state, "rdr", None)
    if info is not None:
        return bool(info.https)
    return request.url.scheme == "https"


def set_cookie(response, request: Request, name: str, value: str, max_age: int) -> None:
    """Every session cookie goes out the same way: not readable by scripts, not sent on
    cross-site requests, and https-only when the browser reached RDR over https."""
    response.set_cookie(name, value, max_age=max_age, samesite="lax", httponly=True, secure=request_is_https(request), path="/")


def admin_token(db: sqlite3.Connection | None = None) -> str:
    if db is None:
        with get_db() as own:
            stored = get_setting(own, "admin_password_hash") or ""
    else:
        stored = get_setting(db, "admin_password_hash") or ""
    return serializer().dumps({"admin": _bound_to(stored), "ts": int(time.time()), "nonce": secrets.token_hex(8)})


def is_admin(request: Request) -> bool:
    """Admin cookie is valid for ADMIN_IDLE_SECONDS; admin pages refresh it on every visit.
    It also has to match the password it was issued for."""
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw:
        return False
    try:
        data = serializer().loads(raw)
        if not data.get("admin") or (time.time() - int(data.get("ts", 0))) >= ADMIN_IDLE_SECONDS:
            return False
    except (BadSignature, ValueError, TypeError, AttributeError):
        return False
    with get_db() as db:
        stored = get_setting(db, "admin_password_hash")
    return bool(stored) and hmac.compare_digest(str(data["admin"]), _bound_to(stored))


def tech_token(tech_id: int, site_id: int) -> str:
    return serializer().dumps({"tech_id": tech_id, "site_id": site_id})


def current_tech(request: Request) -> Optional[dict]:
    raw = request.cookies.get(TECH_COOKIE)
    if not raw:
        return None
    try:
        data = serializer().loads(raw)
        return {"tech_id": int(data["tech_id"]), "site_id": int(data["site_id"])}
    except (BadSignature, KeyError, ValueError, TypeError):
        return None


def live_tech(db: sqlite3.Connection, request: Request, active: bool = True, site_id: Optional[int] = None) -> Optional[dict]:
    """The tech session, checked against the database.

    The cookie lasts 30 days and outlives the rows it names: a tech or site deleted or
    switched off in Admin, a reset, a restored backup. Anything that shows the kiosk or
    writes a transaction asks here, so a stale session is treated as no session.
    With active=False the tech and site only have to exist (enough to credit an admin edit).
    site_id is the site a kiosk tab was opened with; it stands in for the cookie's site.
    """
    sess = current_tech(request)
    if sess is None:
        return None
    if site_id is not None:
        sess = dict(sess, site_id=site_id)
    on = " AND active = 1" if active else ""
    if not db.execute(f"SELECT 1 FROM techs WHERE id = ?{on}", (sess["tech_id"],)).fetchone():
        return None
    if not db.execute(f"SELECT 1 FROM sites WHERE id = ?{on}", (sess["site_id"],)).fetchone():
        return None
    return sess
