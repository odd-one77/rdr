"""RDR tray application (Windows).

Runs the server in the background with no console window and puts an icon
in the notification area. Left-click opens Admin > Settings; right-click
offers the kiosk, the mirror, a restart and Quit. Start it with pythonw.exe
so no window appears:

    python\\pythonw.exe tray.py

Falls back to the console launcher if the tray cannot be created.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import DATA_DIR, load_config  # noqa: E402
from app.launcher import Supervisor  # noqa: E402

SINGLE_INSTANCE_PORT = 47831   # bound while the tray runs; a second copy just opens Settings


def _single_instance() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def _urls() -> dict:
    cfg = load_config()
    base = f"http://localhost:{cfg.get('port', 8080)}"
    return {"settings": base + "/admin/settings", "kiosk": base + "/", "mirror": base + "/mirror", "admin": base + "/admin"}


def _open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    lock = _single_instance()
    if lock is None:
        webbrowser.open(_urls()["settings"])   # already running: just show settings
        return 0

    try:
        import pystray
        from PIL import Image
    except ImportError:
        print("pystray / Pillow not installed; running in console mode instead.", flush=True)
        from run import main as console_main
        return console_main()

    sup = Supervisor(log_to_file=True)
    state = {"status": "starting"}

    icon_path = ROOT / "app" / "static" / "img" / "icon.png"
    image = Image.open(icon_path) if icon_path.exists() else Image.new("RGBA", (64, 64), (226, 0, 26, 255))

    def title() -> str:
        u = _urls()["kiosk"]
        return f"RDR - {state['status']}  {u}"

    def open_(key):
        def _go(icon=None, item=None):
            webbrowser.open(_urls()[key])
        return _go

    def do_restart(icon=None, item=None):
        sup.restart()

    def do_quit(icon, item=None):
        sup.stop()
        icon.visible = False
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open settings", open_("settings"), default=True),
        pystray.MenuItem("Open kiosk", open_("kiosk")),
        pystray.MenuItem("Open mirror", open_("mirror")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: f"Server: {state['status']}", None, enabled=False),
        pystray.MenuItem("Restart server", do_restart),
        pystray.MenuItem("Open data folder", lambda icon, item: _open_folder(DATA_DIR)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit RDR", do_quit),
    )
    icon = pystray.Icon("RDR", image, title(), menu)

    def on_change(st: str) -> None:
        state["status"] = st
        try:
            icon.title = title()
            icon.update_menu()
            if st == "running":
                icon.notify(f"Kiosk: {_urls()['kiosk']}\nLeft-click the icon for settings.", "RDR is running")
        except Exception:  # noqa: BLE001
            pass

    sup.on_change = on_change
    sup.open_browser_override = load_config().get("open_browser", True)

    def run_server():
        sup.run_forever()
        # server stopped on its own (crash or port in use): keep the icon so the user can restart or quit
        state["status"] = "stopped"
        try:
            icon.title = title(); icon.update_menu()
            icon.notify("The server stopped. Right-click the RDR icon to restart it or open the log in the data folder.", "RDR stopped")
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=run_server, daemon=True, name="rdr-supervisor").start()
    icon.run()
    sup.stop()
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
