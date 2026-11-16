"""Process supervisor shared by run.py (console) and tray.py (tray application).

Starts the web server (and the optional HTTPS server), restarts it when the
app asks (exit code RESTART_EXIT_CODE after "Update & restart"), and shuts
everything down cleanly on quit.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from .config import DATA_DIR, RESTART_EXIT_CODE, ROOT_DIR, ensure_dirs, load_config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Supervisor:
    def __init__(self, open_browser: bool | None = None, log_to_file: bool = False, reload: bool = False,
                 port: int | None = None, host: str | None = None):
        self.open_browser_override = open_browser
        self.log_to_file = log_to_file
        self.reload = reload
        self.port_override = port
        self.host_override = host
        self.main: subprocess.Popen | None = None
        self.https: subprocess.Popen | None = None
        self._want_restart = False
        self._want_quit = False
        self._lock = threading.Lock()
        self._log = None
        self.url = ""
        self.https_url = ""
        self.on_change = None  # callback(status: str) for the tray

    # -- status ----------------------------------------------------------------
    def status(self) -> str:
        if self.main is None:
            return "stopped"
        return "running" if self.main.poll() is None else "stopped"

    def _emit(self, text: str) -> None:
        if self.on_change:
            try:
                self.on_change(text)
            except Exception:  # noqa: BLE001
                pass

    # -- lifecycle -------------------------------------------------------------
    def _open_log(self):
        if not self.log_to_file:
            return None
        ensure_dirs()
        path = DATA_DIR / "server.log"
        try:
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                path.replace(DATA_DIR / "server.log.1")
        except OSError:
            pass
        self._log = open(path, "a", encoding="utf-8", buffering=1)
        self._log.write(f"\n== RDR start {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        return self._log

    def start(self) -> None:
        cfg = load_config()
        host = self.host_override or cfg["host"]
        port = self.port_override or int(cfg["port"])
        self.url = f"http://localhost:{port}/"
        log = self._open_log()
        out = {"stdout": log, "stderr": subprocess.STDOUT} if log else {}
        # The app reads forwarding headers itself, and only from proxies listed in Settings (app/security.py),
        # so uvicorn must not rewrite the client address from them.
        safe = ["--no-proxy-headers", "--no-server-header"]
        cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port), "--log-level", "warning"] + safe
        if self.reload:
            cmd.append("--reload")
        self.main = subprocess.Popen(cmd, cwd=ROOT_DIR, creationflags=NO_WINDOW, **out)
        self.https = None
        self.https_url = ""
        if cfg.get("https"):
            from .services import certs
            if certs.ready():
                hport = int(cfg.get("https_port", 8443))
                self.https_url = f"https://<this-pc>:{hport}/"
                https_log = open(DATA_DIR / "https.log", "a", encoding="utf-8", buffering=1)
                https_log.write(f"\n== https server start {time.strftime('%Y-%m-%d %H:%M:%S')} port {hport}\n")
                self.https = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(hport), "--log-level", "info"] + safe +
                    ["--ssl-keyfile", str(certs.SRV_KEY), "--ssl-certfile", str(certs.SRV_CRT)],
                    cwd=ROOT_DIR, stdout=https_log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        self._emit("starting")

    def _terminate(self, proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    def stop(self) -> None:
        with self._lock:
            self._want_quit = True
            self._terminate(self.main)
            self._terminate(self.https)
        if self._log:
            try:
                self._log.write(f"== RDR stop {time.strftime('%Y-%m-%d %H:%M:%S')}\n"); self._log.close()
            except Exception:  # noqa: BLE001
                pass
            self._log = None
        self._emit("stopped")

    def restart(self) -> None:
        """Ask the loop to restart the server (used by the tray menu)."""
        with self._lock:
            self._want_restart = True
            self._terminate(self.main)

    def wait_until_up(self, timeout: float = 40) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.main is not None and self.main.poll() is not None and not self._want_restart:
                return False
            try:
                urllib.request.urlopen(self.url, timeout=1).read(1)
                return True
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        return False

    def run_forever(self) -> int:
        """Start, supervise, restart on request. Returns the last exit code."""
        first = True
        while not self._want_quit:
            self.start()
            if first:
                cfg = load_config()
                want = cfg.get("open_browser", True) if self.open_browser_override is None else self.open_browser_override
                if want:
                    threading.Thread(target=self._open_when_up, daemon=True).start()
            first = False
            if self.wait_until_up():
                self._emit("running")
            code = self.main.wait()
            self._terminate(self.https)
            if self._want_quit:
                return 0
            if code == RESTART_EXIT_CODE or self._want_restart:
                self._want_restart = False
                self._emit("restarting")
                time.sleep(1)
                continue
            self._emit("stopped")
            return code
        return 0

    def _open_when_up(self) -> None:
        if self.wait_until_up():
            webbrowser.open(self.url)
