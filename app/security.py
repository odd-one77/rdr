"""What stands between the network and the app.

RDR was written for one trusted room network. This module is what makes it safe
to put behind a reverse proxy or a tunnel as well. Every request passes through
``SecurityMiddleware`` before any page or API code runs:

1. Where did it come from? Direct from the local network, or from outside:
   through a proxy (a listed proxy address, any forwarding header, or a public
   address in the Host header) or straight from a public address. Forwarding
   headers are only believed about the client address when a proxy sent them.
2. Host check. Only local names, IP addresses and the configured public
   addresses are served, which stops DNS rebinding against a server on a LAN.
3. Outside requests are refused until remote access is switched on, and Admin
   stays local unless that is allowed too.
4. Requests that change something must come from RDR's own pages (Origin check).
5. Access code. A browser enters the shared code once and keeps a signed cookie.
6. Request bodies are capped, and every response carries a content security
   policy and the usual protective headers.

Wrong passwords and codes are counted per client address by ``limiter``.
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from urllib.parse import quote, urlsplit

from starlette.requests import Request

from . import auth
from .config import load_config

FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "forwarded",
                   "x-real-ip", "cf-connecting-ip", "true-client-ip", "via")
# names that never resolve on the public internet, plus common router domains
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain", ".localhost", ".fritz.box",
                  ".corp", ".intranet", ".private", ".localnet")
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# what a browser without the access code may still load: the code form, its stylesheet and logo, the up check
OPEN_PATHS = ("/access", "/api/ping", "/favicon.ico")
OPEN_PREFIXES = ("/static/",)
ACCESS_REFRESH_SECONDS = 7 * 86400   # a browser in daily use never reaches the 90 day limit

MB = 1024 * 1024
BODY_LIMITS = (   # first matching prefix wins
    ("/admin/backup", 512 * MB),
    ("/hub/api/", 256 * MB),   # a site node sending its copy of an event
    ("/admin/import", 32 * MB),
    ("/admin", 8 * MB),
    ("/api/kiosk/state", 64 * 1024),
)
BODY_LIMIT_DEFAULT = MB


# -- settings -----------------------------------------------------------------

def clean_hosts(text: str) -> list[str]:
    """Public addresses as typed in Settings (one per line or comma separated), reduced to bare host names."""
    out: list[str] = []
    for raw in (text or "").replace(",", "\n").split():
        h = raw.strip().lower()
        if "//" in h:
            h = urlsplit(h).hostname or ""
        h = _split_host(h.split("/")[0])[0].strip(".")
        if h and all(c.isalnum() or c in "-.:" for c in h) and h not in out:
            out.append(h)
    return out[:20]


def clean_proxies(text: str) -> tuple[list[str], list[str]]:
    """Proxy addresses as typed in Settings: single addresses or ranges. Returns (good, rejected)."""
    good: list[str] = []
    bad: list[str] = []
    for raw in (text or "").replace(",", "\n").split():
        try:
            net = ipaddress.ip_network(raw.strip(), strict=False)
        except ValueError:
            bad.append(raw.strip())
            continue
        if net.prefixlen < (8 if net.version == 4 else 32):   # "everything is my proxy" would let anyone forge addresses
            bad.append(raw.strip())
            continue
        s = str(net.network_address) if net.num_addresses == 1 else str(net)
        if s not in good:
            good.append(s)
    return good[:20], bad


@lru_cache(maxsize=16)
def _networks(entries: tuple[str, ...]) -> tuple:
    nets = []
    for e in entries:
        try:
            nets.append(ipaddress.ip_network(e, strict=False))
        except ValueError:
            pass
    return tuple(nets)


def _ip(text: str):
    try:
        ip = ipaddress.ip_address((text or "").strip().strip("[]").split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _is_proxy(addr: str, cfg: dict) -> bool:
    ip = _ip(addr)
    if ip is None:
        return False
    return any(ip.version == n.version and ip in n for n in _networks(tuple(cfg.get("trusted_proxies") or ())))


def _split_host(value: str) -> tuple[str, str]:
    """'Name:8080' or '[::1]:8080' to (name, port), lower case."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        host, _, rest = v[1:].partition("]")
        return host, rest.lstrip(":")
    if v.count(":") == 1:
        host, _, port = v.partition(":")
        return host, port
    return v, ""


@lru_cache(maxsize=1)
def _own_names() -> frozenset:
    names = {"localhost"}
    try:
        names.add(socket.gethostname().lower())
        names.add(socket.getfqdn().lower())
    except OSError:
        pass
    return frozenset(n for n in names if n)


def host_allowed(host: str, cfg: dict) -> bool:
    """A name this server answers to. Stops a hostile web page from reaching a LAN server through
    a DNS name it controls, and keeps the app off names nobody configured."""
    if not host:
        return False
    if _ip(host) is not None:
        return True
    if not all(c.isalnum() or c in "-._" for c in host):
        return False
    host = host.rstrip(".")
    if "." not in host or host.endswith(LOCAL_SUFFIXES) or host in _own_names():
        return True
    return host in (cfg.get("public_hosts") or ()) or host in (cfg.get("local_hosts") or ())


def _public_peer(peer: str, server: str) -> bool:
    """A peer on the public internet. A public IPv6 address in the server's own /64 is a LAN neighbour."""
    ip = _ip(peer)
    if ip is None or not ip.is_global:
        return False
    own = _ip(server)
    if ip.version == 6 and own is not None and own.version == 6:
        return ip not in ipaddress.ip_network(f"{own}/64", strict=False)
    return True


# -- who is asking -------------------------------------------------------------

@dataclass
class Origin:
    peer: str            # the address that opened the connection
    client: str          # the browser's address as far as it can be trusted
    proxied: bool        # came through a reverse proxy or tunnel
    remote: bool         # from outside the local network (proxied, or a public address)
    https: bool
    host: str            # Host header without the port
    proxy_listed: bool   # the peer is one of the proxy addresses in Settings

    def describe(self) -> str:
        if self.proxied:
            via = f"the proxy at {self.peer}"
            if not self.proxy_listed:
                via += ", which is not listed as a proxy address"
            return f"Through {via}, for the browser at {self.client}."
        if self.remote:
            return f"Directly from the public address {self.peer}."
        return f"Directly from {self.peer} on the local network."


def _headers(scope) -> dict:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def origin_of(scope: dict, cfg: dict) -> Origin:
    headers = _headers(scope)
    peer = (scope.get("client") or ("", 0))[0] or ""
    server = (scope.get("server") or ("", 0))[0] or ""
    host = _split_host(headers.get("host", ""))[0]
    listed = _is_proxy(peer, cfg)
    forwarded = any(h in headers for h in FORWARD_HEADERS)
    proxied = listed or forwarded or host in (cfg.get("public_hosts") or ())
    client = peer
    peer_ip = _ip(peer)
    if proxied and (listed or (peer_ip is not None and not peer_ip.is_global)):
        # Only a proxy may speak for someone else. A listed proxy is believed. A sender on the local
        # network that adds forwarding headers is believed only for telling clients apart when counting
        # wrong passwords; it is counted itself as well (see Limiter). The rightmost address that is not
        # a listed proxy is the one the nearest proxy actually saw.
        chain = [p.strip() for p in headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if not chain and headers.get("x-real-ip"):
            chain = [headers["x-real-ip"].strip()]
        for hop in reversed(chain):
            hop_ip = _ip(hop)
            if hop_ip is None:
                break
            client = str(hop_ip)
            if not _is_proxy(client, cfg):
                break
    proto = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return Origin(peer=peer, client=client or peer, proxied=proxied, remote=proxied or _public_peer(peer, server),
                  https=scope.get("scheme") == "https" or proto == "https", host=host, proxy_listed=listed)


def code_needed(origin: Origin, cfg: dict) -> bool:
    """Does this request have to show the access code cookie?"""
    if not cfg.get("access_code_hash"):
        return False
    if origin.remote:
        return True
    # "Only through the proxy" depends on recognising the proxy, so it needs a proxy address listed
    proxy_only = cfg.get("access_scope") == "proxy" and bool(cfg.get("trusted_proxies"))
    return not proxy_only


def same_origin(headers: dict, cfg: dict) -> bool:
    """For requests that change something: did the browser send it from one of RDR's own pages?"""
    origin = headers.get("origin")
    if origin is None:
        # Older browsers and scripts send no Origin. A browser that says the request came from
        # another site is refused; otherwise the SameSite cookies are the protection.
        return headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")
    parts = urlsplit(origin.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False   # includes "Origin: null" from sandboxed frames and data: pages
    if parts.netloc.lower() == headers.get("host", "").strip().lower():
        return True
    name = parts.hostname.lower()
    forwarded = _split_host(headers.get("x-forwarded-host", "").split(",")[0])[0]
    if forwarded and name == forwarded:
        return True
    return name in (cfg.get("public_hosts") or ())


# -- wrong password counting ----------------------------------------------------

class Limiter:
    """Counts wrong passwords and codes per client address and locks that address out for a while.

    Five wrong tries inside fifteen minutes start a lock of 30 seconds, doubling with each further
    try up to fifteen minutes. An address that speaks for many clients (a proxy that is not listed)
    gets a larger allowance, and all outside clients together are capped too, so a spread of
    addresses cannot keep guessing. Local clients are never locked out by what outside ones do.
    """
    WINDOW = 15 * 60
    FREE = 5
    FREE_SHARED = 30
    FREE_REMOTE = 100

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[tuple[str, str], list[float]] = {}

    def _keys(self, kind: str, origin: Origin) -> list[tuple[tuple[str, str], int]]:
        keys = [((kind, origin.client), self.FREE)]
        if origin.peer != origin.client and not origin.proxy_listed:
            keys.append(((kind, "peer " + origin.peer), self.FREE_SHARED))
        if origin.remote:
            keys.append(((kind, "remote"), self.FREE_REMOTE))
        return keys

    def _locked_for(self, key: tuple[str, str], free: int, now: float) -> int:
        fails = [t for t in self._fails.get(key, ()) if now - t < self.WINDOW]
        if fails:
            self._fails[key] = fails
        else:
            self._fails.pop(key, None)
        if len(fails) < free:
            return 0
        until = fails[-1] + min(self.WINDOW, 30 * 2 ** min(len(fails) - free, 10))
        return max(0, int(until - now + 0.999))

    def wait(self, kind: str, origin: Origin) -> int:
        """Seconds this client still has to wait before another try, 0 when it may try now."""
        now = time.time()
        with self._lock:
            return max(self._locked_for(key, free, now) for key, free in self._keys(kind, origin))

    def fail(self, kind: str, origin: Origin) -> None:
        now = time.time()
        with self._lock:
            if len(self._fails) > 20000:   # a flood of forged addresses must not eat the memory
                self._fails = {k: v for k, v in self._fails.items() if now - v[-1] < self.WINDOW}
            for key, _ in self._keys(kind, origin):
                self._fails.setdefault(key, []).append(now)
        via = f" through {origin.peer}" if origin.peer != origin.client else ""
        print(f"[security] wrong {kind} from {origin.client}{via}", flush=True)

    def ok(self, kind: str, origin: Origin) -> None:
        with self._lock:
            self._fails.pop((kind, origin.client), None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


limiter = Limiter()


def wait_text(seconds: int) -> str:
    if seconds >= 90:
        return f"Too many wrong tries. Try again in {round(seconds / 60)} minutes."
    return f"Too many wrong tries. Try again in {seconds} seconds."


def origin_from(request: Request) -> Origin:
    found = getattr(request.state, "rdr", None)
    return found if found is not None else origin_of(request.scope, load_config())


def is_remote(request: Request) -> bool:
    return origin_from(request).remote


# -- responses the middleware gives itself ---------------------------------------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · RDR</title><style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0e0f12;color:#ececf0;font:14px/1.45 "Segoe UI",Inter,Roboto,Arial,sans-serif}}
main{{width:min(440px,100% - 32px);background:#15171b;border:1px solid #2a2d34;border-radius:6px;padding:24px}}
h1{{margin:0 0 8px;font-size:20px;font-weight:600}}p{{margin:0 0 8px;color:#9095a0}}p:last-child{{margin:0}}
</style></head><body><main><h1>{title}</h1>{body}</main></body></html>"""

PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def refusal(status: int, title: str, *lines: str, api: bool = False) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    if api:
        body = json.dumps({"error": title, "detail": " ".join(lines) or title}).encode()
        ctype = b"application/json"
    else:
        body = _PAGE.format(title=escape(title), body="".join(f"<p>{escape(ln)}</p>" for ln in lines)).encode()
        ctype = b"text/html; charset=utf-8"
    return status, [(b"content-type", ctype), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store"),
                    (b"content-security-policy", PAGE_CSP.encode())], body


class _TooLarge(Exception):
    pass


def csp_for(path: str, nonce: str) -> str:
    if path.startswith(("/uploads/", "/streams/")):
        # files an admin uploaded (the event logo may be an SVG) never get to run anything
        return "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    # the mirror plays whatever feed address the admin set, so it alone may load media from elsewhere
    feed = " https: http:" if path == "/mirror" else ""
    return ("default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            f"media-src 'self' blob:{feed}; "
            f"connect-src 'self'{feed}; "
            "worker-src 'self' blob:; "
            "frame-src https://www.youtube.com https://www.youtube-nocookie.com; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


def _body_limit(path: str) -> int:
    for prefix, limit in BODY_LIMITS:
        if path.startswith(prefix):
            return limit
    return BODY_LIMIT_DEFAULT


def _with_headers(headers: list, path: str, nonce: str) -> list:
    have = {k.lower() for k, _ in headers}
    extra = [
        (b"content-security-policy", csp_for(path, nonce).encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"SAMEORIGIN"),
        # same-origin and not no-referrer: with no-referrer some browsers send "Origin: null" on forms
        (b"referrer-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(self), microphone=(), geolocation=(), payment=(), usb=()"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"x-robots-tag", b"noindex, nofollow"),
    ]
    return list(headers) + [(k, v) for k, v in extra if k not in have]


class SecurityMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":   # RDR has none; nothing may slip past the checks this way
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cfg = load_config()
        origin = origin_of(scope, cfg)
        nonce = secrets.token_urlsafe(16)
        state = scope.setdefault("state", {})
        state["rdr"] = origin
        state["csp_nonce"] = nonce
        path = scope.get("path", "/")
        method = scope.get("method", "GET").upper()
        headers = _headers(scope)
        api = path.startswith("/api/")

        refused, refresh = self._check(cfg, origin, path, method, headers, api, scope)
        if refused is not None:
            await self._send(send, *refused)
            return

        limit = _body_limit(path)
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await self._send(send, *refusal(413, "Too large", "That upload is larger than RDR accepts.", api=api))
            return
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _TooLarge
            return message

        started = False

        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                extra = list(message.get("headers", []))
                if refresh:
                    extra.append((b"set-cookie", refresh))
                message = dict(message, headers=_with_headers(extra, path, nonce))
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            if not started:
                await self._send(send, *refusal(413, "Too large", "That upload is larger than RDR accepts.", api=api))

    # -- the checks, in order ---------------------------------------------------
    def _check(self, cfg, origin: Origin, path: str, method: str, headers: dict, api: bool, scope):
        if not host_allowed(origin.host, cfg):
            return refusal(421, "Address not set up",
                           f"RDR does not answer to the name {origin.host or 'in this request'}.",
                           "Open RDR by its IP address, or add this name under Settings, Remote access.",
                           api=api), None
        if path.startswith("/hub/api/"):
            # site nodes: no browser and no cookies; every call carries the node's own key, checked (and
            # counted for wrong tries) by the hub router, so the access code and Origin checks do not apply
            return None, None
        if origin.remote and not (cfg.get("remote_access") and cfg.get("access_code_hash")):
            return refusal(403, "Remote access is off",
                           "This RDR only answers on its local network.",
                           "To open it up, set an access code under Settings, Remote access, from a computer on the local network.",
                           api=api), None
        if origin.remote and path.startswith("/admin") and not cfg.get("remote_admin"):
            return refusal(403, "Admin is local only", "Admin is only available on the local network.",
                           "This can be changed under Settings, Remote access.", api=api), None
        if method in UNSAFE_METHODS and not same_origin(headers, cfg):
            print(f"[security] refused {method} {path} from another site ({headers.get('origin', 'no origin')}) for {origin.client}", flush=True)
            return refusal(403, "Request refused", "This request did not come from an RDR page.",
                           "If RDR sits behind a proxy, the proxy has to pass the Host header on, or its public address has to be listed under Settings, Remote access.",
                           api=api), None
        if not code_needed(origin, cfg):
            return None, None
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return None, None
        cookies = Request(scope).cookies
        age = auth.access_age(cookies, cfg)
        if age is None:
            if api or method != "GET":
                return refusal(401, "Access code needed", "Enter the access code first.", api=True), None
            query = scope.get("query_string", b"").decode("latin-1")
            target = path + ("?" + query if query else "")
            return (303, [(b"location", ("/access?next=" + quote(target, safe="")).encode("latin-1")),
                          (b"content-length", b"0"), (b"cache-control", b"no-store")], b""), None
        if age > ACCESS_REFRESH_SECONDS:
            secure = "; Secure" if origin.https else ""
            cookie = (f"{auth.ACCESS_COOKIE}={auth.access_token()}; Max-Age={auth.ACCESS_DAYS * 86400}; "
                      f"Path=/; HttpOnly; SameSite=Lax{secure}")
            return None, cookie.encode("latin-1")
        return None, None

    async def _send(self, send, status: int, headers: list, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status, "headers": _with_headers(headers, "", "")})
        await send({"type": "http.response.body", "body": body})
