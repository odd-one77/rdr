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


RELAY_KINDS = ("rtsp", "dshow", "decklink")   # kinds that go through ffmpeg -> HLS


def classify_feed(url: str) -> str:
    """rtsp | dshow | decklink | youtube | hls | file | unknown, from the configured feed address."""
    u = (url or "").strip().lower()
    if not u:
        return "unknown"
    if u.startswith("rtsp://") or u.startswith("rtsps://"):
        return "rtsp"
    if u.startswith("dshow:"):
        return "dshow"
    if u.startswith("decklink:"):
        return "decklink"
    if "youtube.com/" in u or "youtu.be/" in u:
        return "youtube"
    if ".m3u8" in u:
        return "hls"
    if u.split("?")[0].endswith((".mp4", ".webm", ".mov", ".m4v")):
        return "file"
    return "unknown"


def youtube_embed(url: str) -> str | None:
    """Turn any YouTube watch/short/live/share link into a muted autoplaying embed URL."""
    import re
    from urllib.parse import parse_qs, urlparse
    u = urlparse(url.strip())
    vid = None
    if "youtu.be" in u.netloc:
        vid = u.path.strip("/").split("/")[0]
    elif "youtube.com" in u.netloc:
        if u.path.startswith(("/embed/", "/live/", "/shorts/", "/v/")):
            vid = u.path.split("/")[2] if len(u.path.split("/")) > 2 else None
        else:
            vid = parse_qs(u.query).get("v", [None])[0]
    if not vid or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
        return None
    return f"https://www.youtube.com/embed/{vid}?autoplay=1&mute=1&controls=0&loop=1&playlist={vid}&rel=0&modestbranding=1&playsinline=1"


FFMPEG_MISSING = ("ffmpeg not found. Put ffmpeg.exe in the tools folder next to the app." if sys.platform.startswith("win")
                  else "ffmpeg not found. Install it with: apt install ffmpeg")


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


def _input_args(url: str) -> tuple[list[str], bool]:
    """ffmpeg input arguments for a feed address. Returns (args, must_transcode).

    rtsp://host/path                      network camera / encoder
    dshow:Blackmagic WDM Capture          DirectShow device (DeckLink, USB capture) on Windows;
                                          options after '?', e.g. dshow:Name?video_size=1920x1080&framerate=25
    decklink:DeckLink Mini Recorder       native DeckLink input (needs an ffmpeg build with decklink support)
    """
    kind = classify_feed(url)
    if kind == "dshow":
        name, _, query = url[len("dshow:"):].partition("?")
        args = ["-f", "dshow", "-rtbufsize", "256M"]
        for part in query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                args += [f"-{k}", v]
        return args + ["-i", f"video={name.strip()}"], True
    if kind == "decklink":
        name, _, query = url[len("decklink:"):].partition("?")
        args = ["-f", "decklink"]
        for part in query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                args += [f"-{k}", v]
        return args + ["-i", name.strip()], True
    return ["-rtsp_transport", "tcp", "-fflags", "nobuffer", "-flags", "low_delay", "-i", url], False


def list_capture_devices() -> str:
    """Names ffmpeg can see for dshow: / decklink: feeds (Windows), as plain text for the settings page."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return "ffmpeg not found."
    out = []
    for args in (["-list_devices", "true", "-f", "dshow", "-i", "dummy"], ["-sources", "decklink"]):
        try:
            p = subprocess.run([ffmpeg, "-hide_banner"] + args, capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = (p.stderr + p.stdout).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            text = str(e)
        out.append(f"$ ffmpeg {' '.join(args)}\n{text}")
    return "\n\n".join(out)


def _command(ffmpeg: str, url: str, out: Path, transcode: bool) -> list[str]:
    inp, must = _input_args(url)
    transcode = transcode or must
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin"] + inp + ["-an"]
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
            return {"running": False, "error": FFMPEG_MISSING}
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
