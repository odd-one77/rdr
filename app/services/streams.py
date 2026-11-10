"""RTSP -> HLS relay for the mirror's programme feed.

One ffmpeg process per site, started when a mirror page for that site asks
for it and stopped after it has not asked for a while. Segments are written
under data/streams/<site_id>/ and served as static files, so the mirror only
needs hls.js (or Safari's built-in HLS).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..config import DATA_DIR, ROOT_DIR

STREAM_DIR = DATA_DIR / "streams"
IDLE_STOP_SECONDS = 45

_lock = threading.Lock()
_procs: dict[int, dict] = {}   # site_id -> {proc, url, started, last_ping, transcode, log}


def ffmpeg_path() -> str | None:
    """ffmpeg bundled by the installer (tools/), next to the app, or on PATH."""
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    for cand in (ROOT_DIR / "tools" / exe, ROOT_DIR / exe):
        if cand.exists():
            return str(cand)
    return shutil.which("ffmpeg")


def _site_dir(site_id: int) -> Path:
    d = STREAM_DIR / str(site_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _command(ffmpeg: str, url: str, out: Path, transcode: bool) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
           "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-flags", "low_delay", "-i", url, "-an"]
    if transcode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-g", "30", "-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "1500k", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-f", "hls", "-hls_time", "1", "-hls_list_size", "4", "-hls_flags", "delete_segments+append_list+omit_endlist",
            "-hls_segment_type", "mpegts", "-hls_segment_filename", str(out / "seg%05d.ts"), str(out / "index.m3u8")]
    return cmd


def ensure_running(site_id: int, url: str, transcode: bool = False) -> dict:
    """Called by the mirror every few seconds. Starts ffmpeg if needed, records the ping."""
    with _lock:
        entry = _procs.get(site_id)
        now = time.time()
        if entry and entry["proc"].poll() is None and entry["url"] == url and entry["transcode"] == transcode:
            entry["last_ping"] = now
            return _public(site_id, entry)
        if entry:
            _stop_locked(site_id)
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            return {"running": False, "error": "ffmpeg not found. Put ffmpeg.exe in the tools folder or install ffmpeg."}
        out = _site_dir(site_id)
        for f in out.glob("*"):
            f.unlink(missing_ok=True)
        log = open(out / "ffmpeg.log", "w", encoding="utf-8")
        proc = subprocess.Popen(_command(ffmpeg, url, out, transcode), stdout=log, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        entry = {"proc": proc, "url": url, "started": now, "last_ping": now, "transcode": transcode, "log": log}
        _procs[site_id] = entry
        return _public(site_id, entry)


def _public(site_id: int, entry: dict) -> dict:
    alive = entry["proc"].poll() is None
    playlist = STREAM_DIR / str(site_id) / "index.m3u8"
    info = {"running": alive, "ready": alive and playlist.exists(), "playlist": f"/streams/{site_id}/index.m3u8",
            "uptime": int(time.time() - entry["started"]), "transcode": entry["transcode"]}
    if not alive:
        info["error"] = _tail_log(site_id) or f"ffmpeg exited with code {entry['proc'].returncode}"
    return info


def _tail_log(site_id: int, lines: int = 3) -> str:
    try:
        text = (STREAM_DIR / str(site_id) / "ffmpeg.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()
        return " | ".join(text[-lines:])
    except OSError:
        return ""


def _stop_locked(site_id: int) -> None:
    entry = _procs.pop(site_id, None)
    if not entry:
        return
    proc = entry["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        entry["log"].close()
    except Exception:  # noqa: BLE001
        pass


def stop(site_id: int) -> None:
    with _lock:
        _stop_locked(site_id)


def stop_all() -> None:
    with _lock:
        for sid in list(_procs):
            _stop_locked(sid)


def status() -> dict:
    with _lock:
        return {"ffmpeg": ffmpeg_path(), "streams": {sid: _public(sid, e) for sid, e in _procs.items()}}


def _reaper() -> None:
    while True:
        time.sleep(10)
        with _lock:
            for sid, e in list(_procs.items()):
                if time.time() - e["last_ping"] > IDLE_STOP_SECONDS or e["proc"].poll() is not None and time.time() - e["last_ping"] > 5:
                    _stop_locked(sid)


threading.Thread(target=_reaper, daemon=True, name="rdr-stream-reaper").start()
