"""RDR console launcher.

Starts the web server and keeps it running in this window. If the app exits
with the restart code (after "Update & restart" in admin settings), the
server is started again with the freshly pulled code.

    python run.py            # normal start, opens the browser
    python run.py --no-browser
    python run.py --port 9000

For the Windows tray application (no console window) use tray.py.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.launcher import Supervisor  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="RDR - Radio Distribution Room")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="developer auto-reload")
    args = ap.parse_args()

    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("RDR - Radio Distribution Room")
        except Exception:  # noqa: BLE001
            pass

    sup = Supervisor(open_browser=False if args.no_browser else None, reload=args.reload, port=args.port, host=args.host)
    sup.on_change = lambda st: print(f"  RDR {st}  ->  {sup.url}" + (f"   https: {sup.https_url}" if sup.https_url else "") + "   (Ctrl+C to stop)", flush=True)
    try:
        return sup.run_forever()
    except KeyboardInterrupt:
        sup.stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())
