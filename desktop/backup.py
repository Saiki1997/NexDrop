"""Back up and restore NexDrop's own data (settings, queue, history, learned names), never secrets.

    backup.py create  <appdata folder> <backup.zip>
    backup.py inspect <backup.zip>
    backup.py restore <appdata folder> <backup.zip>

Saved sign-ins, cookies, API keys and the Telegram session are left out on purpose: they are
encrypted for this Windows user and would not open on another PC anyway. Restoring never
deletes anything: each file it replaces is kept beside it as "<name>.before-restore-<time>".
Every command prints one JSON line.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

FORMAT = "nexdrop-backup"
VERSION = 1
# Plain files and SQLite databases that make up NexDrop's state. Nothing else is read or written.
FILES = ("gui-settings.json", "runs.json", "completed-urls.json", "order-manifest.jsonl", "folder-registry.jsonl",
         "naming-corrections.jsonl")
DATABASES = ("jobs.sqlite", "cyberdrop.db")
# Keys of gui-settings.json that are not portable or not wanted in a backup
DROP_SETTINGS = ("window", "active_run")
MAX_ENTRY = 4 * 1024 * 1024 * 1024  # a single entry larger than this is refused on restore


def create(appdata: Path, dest: Path) -> dict:
    included: list[str] = []
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in FILES:
            path = appdata / name
            if not path.is_file():
                continue
            if name == "gui-settings.json":
                try:
                    settings = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                for key in DROP_SETTINGS:
                    settings.pop(key, None)
                zf.writestr(name, json.dumps(settings, indent=2))
            else:
                zf.write(path, name)
            included.append(name)
        for name in DATABASES:
            path = appdata / name
            if not path.is_file():
                continue
            copy = Path(tmp) / name
            # a consistent copy even while the window reads it (WAL)
            source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            zf.write(copy, name)
            included.append(name)
        manifest = {"format": FORMAT, "version": VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "files": included, "secrets": "not included"}
        zf.writestr("nexdrop-backup.json", json.dumps(manifest, indent=2))
    return {"ok": True, "path": str(dest), "files": included}


def _manifest(zf: zipfile.ZipFile) -> dict:
    try:
        manifest = json.loads(zf.read("nexdrop-backup.json"))
    except (KeyError, ValueError):
        raise ValueError("This is not a NexDrop backup.") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("This is not a NexDrop backup.")
    if int(manifest.get("version") or 0) > VERSION:
        raise ValueError("This backup was made by a newer NexDrop. Update NexDrop first.")
    return manifest


def _entries(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Only the known file names at the top level; anything else in the zip is ignored."""
    allowed = set(FILES) | set(DATABASES)
    return [info for info in zf.infolist() if info.filename in allowed and info.file_size <= MAX_ENTRY]


def inspect(source: Path) -> dict:
    with zipfile.ZipFile(source) as zf:
        manifest = _manifest(zf)
        files = [{"name": info.filename, "bytes": info.file_size} for info in _entries(zf)]
    return {"ok": True, "created": manifest.get("created", ""), "files": files}


def restore(appdata: Path, source: Path) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    restored: list[str] = []
    kept: list[str] = []
    with zipfile.ZipFile(source) as zf:
        _manifest(zf)
        entries = _entries(zf)
        if not entries:
            raise ValueError("The backup has no NexDrop files in it.")
        appdata.mkdir(parents=True, exist_ok=True)
        for info in entries:
            target = appdata / info.filename
            incoming = appdata / f"{info.filename}.restoring"
            with zf.open(info) as src, incoming.open("wb") as out:
                while chunk := src.read(1024 * 1024):
                    out.write(chunk)
            if info.filename in DATABASES and not _is_sqlite(incoming):
                incoming.unlink()
                continue
            if target.exists():
                aside = appdata / f"{info.filename}.before-restore-{stamp}"
                target.rename(aside)
                kept.append(aside.name)
                for suffix in ("-wal", "-shm"):  # the old journal belongs to the old database
                    journal = appdata / f"{info.filename}{suffix}"
                    if journal.exists():
                        journal.rename(appdata / f"{aside.name}{suffix}")
            incoming.rename(target)
            restored.append(info.filename)
    return {"ok": True, "restored": restored, "keptAside": kept}


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                return False
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if command == "create" and len(sys.argv) == 4:
            result = create(Path(sys.argv[2]), Path(sys.argv[3]))
        elif command == "inspect" and len(sys.argv) == 3:
            result = inspect(Path(sys.argv[2]))
        elif command == "restore" and len(sys.argv) == 4:
            result = restore(Path(sys.argv[2]), Path(sys.argv[3]))
        else:
            raise SystemExit(__doc__)
    except (OSError, ValueError, zipfile.BadZipFile, sqlite3.Error) as error:
        result = {"ok": False, "error": str(error) if isinstance(error, ValueError) else f"{type(error).__name__}: {error}"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
