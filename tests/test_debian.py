"""Debian deployment: the bundle build, the installer script and a clean stop on SIGTERM."""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import pytest

from app import __version__

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="needs bash on Linux or macOS")


def test_installer_scripts_parse():
    for name in ("install_debian.sh", "build_debian.sh"):
        subprocess.run(["bash", "-n", str(ROOT / "installer" / name)], check=True)


def test_debian_bundle_contents(tmp_path):
    subprocess.run(["bash", str(ROOT / "installer" / "build_debian.sh")], check=True, env={**os.environ, "RDR_DIST": str(tmp_path)}, capture_output=True)
    top = f"rdr-debian-{__version__}"
    with tarfile.open(tmp_path / f"{top}.tar.gz") as tf:
        names = set(tf.getnames())
        version = tf.extractfile(f"{top}/VERSION").read().decode()
        script = tf.getmember(f"{top}/installer/install_debian.sh")
    assert version == __version__
    for need in ("run.py", "requirements.txt", "Dockerfile", ".dockerignore", "app/main.py", "app/static/css/rdr.css", "app/templates/base.html"):
        assert f"{top}/{need}" in names, need
    assert script.mode & 0o111
    assert not [n for n in names if "__pycache__" in n or n.startswith(f"{top}/data") or n.endswith((".db", ".exe"))]


def test_sigterm_stops_the_server_cleanly(tmp_path):
    """systemd and Docker stop RDR with SIGTERM: run.py must take its web server down and exit 0."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--no-browser", "--host", "127.0.0.1", "--port", str(port)],
                            env={**os.environ, "RDR_DATA_DIR": str(tmp_path / "data")}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 40
        up = False
        while time.time() < deadline and proc.poll() is None:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).read(1)
                up = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        assert up, "server did not start"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
        with pytest.raises(Exception):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
    finally:
        if proc.poll() is None:
            proc.kill()
