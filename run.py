"""RDR launcher.

Starts the web server and keeps it running. If the app exits with the
restart code (after an "Update from Git" or "Restart" in admin settings),
the server is started again with the freshly pulled code.

    python run.py            # normal start, opens the browser
    python run.py --no-browser
    python run.py --port 9000
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import RESTART_EXIT_CODE, load_config  # noqa: E402


def wait_and_open(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).read(1)
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser(description="RDR - Radio Distribution Room")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="developer auto-reload")
    args = ap.parse_args()

    if os.name == "nt":  # name the console window so it is easy to find / close
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("RDR - Radio Distribution Room")
        except Exception:  # noqa: BLE001
            pass

    first = True
    while True:
        cfg = load_config()  # re-read each loop so a port change applies after restart
        host = args.host or cfg["host"]
        port = args.port or int(cfg["port"])
        url = f"http://localhost:{port}/"
        print(f"\n  RDR  ->  {url}   (Ctrl+C to stop)\n", flush=True)
        if first and cfg.get("open_browser", True) and not args.no_browser:
            threading.Thread(target=wait_and_open, args=(url,), daemon=True).start()
        first = False
        cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port), "--log-level", "warning"]
        if args.reload:
            cmd.append("--reload")
        https_proc = None
        if cfg.get("https"):
            from app.services import certs
            if certs.ready():
                hport = int(cfg.get("https_port", 8443))
                print(f"  HTTPS ->  https://<this-pc>:{hport}/   (phones: open /trust once)\n", flush=True)
                from app.config import DATA_DIR
                https_log = open(DATA_DIR / "https.log", "a", encoding="utf-8")
                https_log.write(f"\n== https server start {time.strftime('%Y-%m-%d %H:%M:%S')} port {hport}\n"); https_log.flush()
                https_proc = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(hport), "--log-level", "info",
                     "--ssl-keyfile", str(certs.SRV_KEY), "--ssl-certfile", str(certs.SRV_CRT)], cwd=ROOT, stdout=https_log, stderr=subprocess.STDOUT)
            else:
                print("  HTTPS is enabled but no certificate exists. Re-issue it in Admin > Settings.", flush=True)
        try:
            code = subprocess.call(cmd, cwd=ROOT)
        except KeyboardInterrupt:
            code = 0
        finally:
            if https_proc is not None and https_proc.poll() is None:
                https_proc.terminate()
                try:
                    https_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    https_proc.kill()
        if code == 0:
            return 0
        if code != RESTART_EXIT_CODE:
            return code
        print("\n  Restarting RDR...\n", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
