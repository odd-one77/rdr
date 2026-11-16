"""Unit tests for the release-zip updater (no network)."""
import io
import zipfile

from app.services import updater


def test_version_tuple():
    assert updater._version_tuple("v0.2.0") > updater._version_tuple("0.1.9")
    assert updater._version_tuple("1.0") == (1, 0)
    assert updater._version_tuple("0.1.0-beta") == (0, 1, 0)


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_apply_zip_replaces_only_app_files(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "ROOT_DIR", tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "old_module.py").write_text("old")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rdr.db").write_text("precious")
    (tmp_path / "python").mkdir()
    (tmp_path / "python" / "python.exe").write_text("runtime")

    blob = _zip({
        "rdr-app-9.9.9/app/main.py": "new main",
        "rdr-app-9.9.9/app/routers/api.py": "new api",
        "rdr-app-9.9.9/run.py": "new run",
        "rdr-app-9.9.9/VERSION": "9.9.9",
        "rdr-app-9.9.9/data/rdr.db": "EVIL",          # must be ignored
        "rdr-app-9.9.9/python/python.exe": "EVIL",    # must be ignored
        "rdr-app-9.9.9/../escape.txt": "EVIL",        # must be ignored
    })
    lines: list[str] = []
    n = updater.apply_zip(blob, lines)
    assert n == 4
    assert (tmp_path / "app" / "main.py").read_text() == "new main"
    assert (tmp_path / "app" / "routers" / "api.py").read_text() == "new api"
    assert not (tmp_path / "app" / "old_module.py").exists()      # stale module removed
    assert (tmp_path / "data" / "rdr.db").read_text() == "precious"
    assert (tmp_path / "python" / "python.exe").read_text() == "runtime"
    assert not (tmp_path.parent / "escape.txt").exists()
    assert (tmp_path / "VERSION").read_text() == "9.9.9"


def test_apply_zip_reads_windows_built_zip(tmp_path, monkeypatch):
    """Compress-Archive writes backslash names and folder entries; a Debian install must unpack them the same."""
    monkeypatch.setattr(updater, "ROOT_DIR", tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "old_module.py").write_text("old")
    blob = _zip({
        "app\\static\\": "",
        "app\\main.py": "new main",
        "app\\static\\css\\rdr.css": "css",
        "run.py": "new run",
        "data\\rdr.db": "EVIL",
        "app\\..\\..\\escape.txt": "EVIL",
    })
    assert updater.apply_zip(blob) == 3
    assert (tmp_path / "app" / "main.py").read_text() == "new main"
    assert (tmp_path / "app" / "static" / "css" / "rdr.css").read_text() == "css"
    assert (tmp_path / "app" / "static").is_dir()
    assert not (tmp_path / "app" / "old_module.py").exists()
    assert not (tmp_path / "data").exists()
    assert not (tmp_path.parent / "escape.txt").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app", "run.py"]


def test_apply_zip_without_top_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "ROOT_DIR", tmp_path)
    blob = _zip({"app/__init__.py": "x", "requirements.txt": "fastapi"})
    assert updater.apply_zip(blob) == 2
    assert (tmp_path / "requirements.txt").read_text() == "fastapi"


def test_status_reports_mode(monkeypatch):
    monkeypatch.setattr(updater, "git_available", lambda: False)
    info = updater.status(fetch=False)
    assert info["mode"] == "release" and info["repo"] == updater.DEFAULT_REPO and info["latest"] is None
