"""Media library index (3.1): one catalog of downloaded files for the Library window.

    library.py <command> <library.sqlite>      one JSON line on stdin, one JSON object on stdout

Commands:
  sync      {"jobs": jobs.sqlite, "roots": [folders]}   import saved files from History, scan the
            folders, find files that were moved or renamed outside NexDrop, mark missing ones
  work      {"seconds": 20, "thumbs": dir}               metadata, fingerprints and thumbnails for
            files that have none yet (ffprobe/ffmpeg), until the time is up; one file at a time
  query     {"text", "kind", "host", "creator", "album", "sort", "group", "offset", "limit", "missing"}
  groups    {"kind": "exact"|"similar", "text", "threshold"}   duplicate groups with their reasons
  facets    {}                                       sites, creators and albums for the filters
  storage   {"roots": [folders]}                     space used per folder/site/type and free space
  file      {"id": n}                                one file with its path (for preview and actions)
  moved     {"id": n, "path": new path}               record a move or rename done by the app

What it stores is local only. Files are never deleted or changed by this module; the app's file
actions (set aside, rename, Recycle Bin after confirmation) record their result with `moved`.

Fingerprints, all computed by the bundled ffmpeg so no image library is needed:
  image  64-bit difference hash of a 9x8 grey thumbnail (robust to resizing and re-encoding)
  video  the same hash at 10/30/50/70/90 % of the duration, plus the duration
  audio  1 bit per 0.1 s: whether the loudness rises, over the first two minutes
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_store import MEDIA_TYPES, file_type  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    ext TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'other',
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    quick TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    duration REAL NOT NULL DEFAULT 0,
    codec TEXT NOT NULL DEFAULT '',
    bitrate INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL DEFAULT '',
    meta_done INTEGER NOT NULL DEFAULT 0,
    phash TEXT NOT NULL DEFAULT '',
    vhash TEXT NOT NULL DEFAULT '',
    ahash TEXT NOT NULL DEFAULT '',
    fp_done INTEGER NOT NULL DEFAULT 0,
    thumb TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    creator TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    saved_at REAL NOT NULL DEFAULT 0,
    history_id INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_size ON files (size);
CREATE INDEX IF NOT EXISTS files_kind ON files (kind, saved_at);
CREATE INDEX IF NOT EXISTS files_host ON files (host);
CREATE INDEX IF NOT EXISTS files_creator ON files (creator);
CREATE INDEX IF NOT EXISTS files_album ON files (album);
CREATE INDEX IF NOT EXISTS files_quick ON files (size, quick);
CREATE INDEX IF NOT EXISTS files_history ON files (history_id);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SKIP_SUFFIXES = (".part", ".nxmeta", ".tmp", ".crdownload", ".partial")
QUICK_BLOCK = 64 * 1024
WORK_KINDS = ("image", "video", "audio")
SIMILAR_IMAGE = 6  # of 64 bits: re-encoded or resized copies are usually 0-4 apart
SIMILAR_VIDEO = 8  # average bits per frame
SIMILAR_AUDIO = 0.12  # share of differing bits
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flashes on Windows


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


def _state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def quick_hash(path: Path, size: int) -> str:
    """Size plus the first and last 64 KB: finds a file again after a move or rename without reading it all."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(QUICK_BLOCK))
        if size > QUICK_BLOCK * 2:
            handle.seek(-QUICK_BLOCK, os.SEEK_END)
            digest.update(handle.read(QUICK_BLOCK))
    return digest.hexdigest()


def full_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _kind(name: str) -> str:
    return file_type(name)


# ─── sync: History + folders → catalog ────────────────────────────────────────


def _identity(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def sync_history(conn: sqlite3.Connection, jobs_db: Path) -> int:
    """Saved files from History that changed since the last sync. Returns how many rows were touched."""
    if not jobs_db.is_file():
        return 0
    since = float(_state(conn, "history_at", "0") or 0)
    src = sqlite3.connect(f"file:{jobs_db.as_posix()}?mode=ro", uri=True, timeout=10)
    src.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in src.execute("PRAGMA table_info(items)")}
        extra = ", ".join(f"items.{c}" for c in ("title", "parent_title", "identity") if c in columns) or "''"
        rows = src.execute(
            f"""SELECT items.id, items.url, items.host, items.filename, items.destination, items.bytes_total,
                       items.updated_at, jobs.source_urls, {extra}
                FROM items JOIN jobs ON jobs.id = items.job_id
                WHERE items.status = 'completed' AND items.destination != '' AND items.updated_at > ?
                ORDER BY items.updated_at""",
            (since,),
        ).fetchall()
    finally:
        src.close()
    touched = 0
    newest = since
    for row in rows:
        keys = row.keys()
        identity = _identity(row["identity"] if "identity" in keys else "")
        path = Path(str(row["destination"]))
        name = path.name
        source = next((line for line in str(row["source_urls"]).split("\n") if line), "")
        album = str(identity.get("album") or identity.get("thread") or (row["parent_title"] if "parent_title" in keys else "") or "")
        title = str(identity.get("post_title") or identity.get("media_title") or (row["title"] if "title" in keys else "") or "")
        values = {
            "path": str(path), "folder": str(path.parent), "name": name, "ext": path.suffix.lower().lstrip("."),
            "kind": _kind(name), "size": int(row["bytes_total"] or 0), "url": str(row["url"] or ""),
            "host": str(row["host"] or ""), "source": source, "creator": str(identity.get("creator") or "")[:200],
            "album": album[:200], "title": title[:300], "saved_at": float(row["updated_at"] or 0), "history_id": int(row["id"]),
        }
        # a file renamed in History keeps its row: find it by history id first
        old = conn.execute("SELECT id FROM files WHERE history_id = ?", (values["history_id"],)).fetchone()
        if old is None:
            old = conn.execute("SELECT id FROM files WHERE path = ?", (values["path"],)).fetchone()
        if old is not None:
            conn.execute(
                f"UPDATE files SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", (*values.values(), old["id"]))
        else:
            conn.execute(f"INSERT OR IGNORE INTO files ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                         tuple(values.values()))
        touched += 1
        newest = max(newest, values["saved_at"])
    _set_state(conn, "history_at", repr(newest))
    conn.commit()
    return touched


def _walk(root: Path, limit: int):
    stack = [root]
    seen = 0
    while stack and seen < limit:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not entry.name.startswith("."):
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and not entry.name.lower().endswith(SKIP_SUFFIXES) \
                        and ".part.seg" not in entry.name:
                    stat = entry.stat()
                    seen += 1
                    yield Path(entry.path), stat.st_size, stat.st_mtime
            except OSError:
                continue


def scan_roots(conn: sqlite3.Connection, roots: list[str], limit: int = 500_000) -> dict:
    """Add files found in the folders, mark gone ones missing, and relink files moved or renamed
    outside NexDrop (same size and the same first and last 64 KB)."""
    known = {row["path"]: (row["id"], row["size"], row["mtime"]) for row in conn.execute("SELECT id, path, size, mtime FROM files")}
    found: dict[str, tuple[int, float]] = {}
    for root in roots:
        base = Path(root)
        if base.is_dir():
            for path, size, mtime in _walk(base, limit):
                found[str(path)] = (size, mtime)
    added = relinked = gone = 0
    # rows whose file is no longer at its path, inside a scanned folder or anywhere
    lost = []
    for path, (row_id, size, _mtime) in known.items():
        if path in found:
            continue
        if not Path(path).exists():
            lost.append((row_id, size, path))
    lost_by_size: dict[int, list[tuple[int, str]]] = {}
    for row_id, size, path in lost:
        lost_by_size.setdefault(size, []).append((row_id, path))
    for path, (size, mtime) in found.items():
        if path in known:
            row_id, old_size, old_mtime = known[path]
            if old_size != size or abs(old_mtime - mtime) > 1:
                conn.execute("UPDATE files SET size = ?, mtime = ?, missing = 0, quick = '', sha256 = '', meta_done = 0, "
                             "fp_done = 0, thumb = '' WHERE id = ?", (size, mtime, row_id))
            continue
        target = Path(path)
        candidates = lost_by_size.get(size) or []
        match = None
        if candidates:
            try:
                quick = quick_hash(target, size)
            except OSError:
                continue
            for row_id, _old in candidates:
                row = conn.execute("SELECT quick FROM files WHERE id = ?", (row_id,)).fetchone()
                if row and row["quick"] == quick:
                    match = row_id
                    break
        if match is not None:
            conn.execute("UPDATE files SET path = ?, folder = ?, name = ?, ext = ?, mtime = ?, missing = 0 WHERE id = ?",
                         (path, str(target.parent), target.name, target.suffix.lower().lstrip("."), mtime, match))
            lost_by_size[size] = [c for c in candidates if c[0] != match]
            relinked += 1
        else:
            conn.execute("INSERT OR IGNORE INTO files (path, folder, name, ext, kind, size, mtime, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (path, str(target.parent), target.name, target.suffix.lower().lstrip("."), _kind(target.name), size, mtime, mtime))
            added += 1
    still_lost = {row_id for items in lost_by_size.values() for row_id, _ in items}
    if still_lost:
        conn.executemany("UPDATE files SET missing = 1 WHERE id = ?", [(i,) for i in still_lost])
        gone = len(still_lost)
    found_ids = [known[p][0] for p in found if p in known]
    if found_ids:
        conn.executemany("UPDATE files SET missing = 0 WHERE id = ? AND missing = 1", [(i,) for i in found_ids])
    conn.commit()
    return {"added": added, "relinked": relinked, "missing": gone, "scanned": len(found)}


def prepare_quick(conn: sqlite3.Connection, deadline: float) -> int:
    """Quick hashes for files that have none yet, so a later move or rename can be followed."""
    done = 0
    for row in conn.execute("SELECT id, path, size FROM files WHERE quick = '' AND missing = 0 LIMIT 2000").fetchall():
        if time.monotonic() > deadline:
            break
        path = Path(row["path"])
        try:
            stat = path.stat()
            conn.execute("UPDATE files SET quick = ?, size = ?, mtime = ? WHERE id = ?",
                         (quick_hash(path, stat.st_size), stat.st_size, stat.st_mtime, row["id"]))
            done += 1
        except OSError:
            conn.execute("UPDATE files SET missing = 1 WHERE id = ?", (row["id"],))
    conn.commit()
    return done


# ─── metadata, fingerprints and thumbnails (ffprobe / ffmpeg) ─────────────────


def tool(name: str) -> str | None:
    here = Path(__file__).resolve().parent.parent / "runtime" / "ffmpeg"
    for candidate in (here / f"{name}.exe", here / name):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def _run(args: list[str], timeout: float = 30, limit: int = 4 * 1024 * 1024) -> bytes | None:
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout[:limit]


def probe(path: Path, ffprobe: str | None) -> dict:
    """Dimensions, duration, codec, bitrate and creation date when the file has them."""
    if not ffprobe:
        return {}
    raw = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=30)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    main = video or audio or {}
    out: dict = {}
    still = video is not None and audio is None and video.get("codec_name") in ("mjpeg", "png", "webp", "gif", "bmp", "tiff", "jpegxl", "av1") \
        and int(video.get("nb_frames") or 1) <= 1 and float(fmt.get("duration") or 0) < 0.5
    if video:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        rotation = 0
        for side in video.get("side_data_list") or []:
            rotation = int(side.get("rotation") or rotation)
        if abs(rotation) in (90, 270):
            width, height = height, width
        out.update(width=width, height=height)
    codecs = [s.get("codec_name") for s in (video, audio) if s and s.get("codec_name")]
    out["codec"] = "/".join(codecs)[:60]
    for value in () if still else (fmt.get("duration"), main.get("duration")):
        try:
            if value and float(value) > 0:
                out["duration"] = round(float(value), 3)
                break
        except (TypeError, ValueError):
            pass
    for value in ((audio or {}).get("bit_rate") if not video else None, fmt.get("bit_rate"), main.get("bit_rate")):
        if value and str(value).isdigit():
            out["bitrate"] = int(value)
            break
    tags = {**(fmt.get("tags") or {}), **(main.get("tags") or {})}
    created = next((tags[k] for k in tags if k.lower() in ("creation_time", "date", "datetimeoriginal")), "")
    out["created"] = str(created)[:40]
    return out


def _dhash(gray: bytes) -> str:
    """64-bit difference hash of a 9x8 grey image (each pixel brighter than its right neighbour)."""
    if len(gray) < 72:
        return ""
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (gray[row * 9 + col] > gray[row * 9 + col + 1])
    return f"{bits:016x}"


def _frame_hash(ffmpeg: str, path: Path, at: float | None = None) -> str:
    args = [ffmpeg, "-v", "error"]
    if at is not None:
        args += ["-ss", f"{at:.3f}"]
    args += ["-i", str(path), "-frames:v", "1", "-vf", "scale=9:8:flags=area,format=gray", "-f", "rawvideo", "-"]
    raw = _run(args, timeout=30, limit=4096)
    return _dhash(raw or b"")


def image_hash(path: Path, ffmpeg: str | None) -> str:
    return _frame_hash(ffmpeg, path) if ffmpeg else ""


def video_hash(path: Path, ffmpeg: str | None, duration: float) -> str:
    if not ffmpeg or duration <= 0:
        return ""
    hashes = [_frame_hash(ffmpeg, path, duration * share) for share in (0.1, 0.3, 0.5, 0.7, 0.9)]
    return ",".join(hashes) if all(hashes) else ""


FRAME = 200  # samples at 2 kHz: 0.1 s


def audio_hash(path: Path, ffmpeg: str | None) -> str:
    if not ffmpeg:
        return ""
    raw = _run([ffmpeg, "-v", "error", "-i", str(path), "-t", "120", "-vn", "-ac", "1", "-ar", "2000", "-f", "s16le", "-"],
               timeout=60, limit=2000 * 2 * 121)
    if not raw or len(raw) < FRAME * 2 * 20:
        return ""
    import array

    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    energy = []
    for start in range(0, len(samples) - FRAME + 1, FRAME):
        energy.append(sum(abs(v) for v in samples[start:start + FRAME]))
    bits = "".join("1" if energy[i + 1] > energy[i] else "0" for i in range(len(energy) - 1))
    return bits


def thumbnail(path: Path, ffmpeg: str | None, folder: Path, row_id: int, kind: str, duration: float) -> str:
    """A 256 px JPEG in the thumbnail cache; the original is never loaded by the window."""
    if not ffmpeg:
        return ""
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{row_id}.jpg"
    args = [ffmpeg, "-v", "error", "-y"]
    if kind == "video" and duration > 2:
        args += ["-ss", f"{min(duration * 0.1, 30):.2f}"]
    args += ["-i", str(path), "-frames:v", "1", "-vf", "scale='min(256,iw)':-2", "-q:v", "5", str(dest)]
    if _run(args, timeout=30) is None or not dest.is_file():
        return ""
    return dest.name


def work(conn: sqlite3.Connection, seconds: float, thumbs: Path | None, fingerprints: bool = True) -> dict:
    """Process files without metadata until `seconds` pass. One file at a time, bounded memory."""
    deadline = time.monotonic() + max(1.0, seconds)
    quick = prepare_quick(conn, deadline)
    ffprobe, ffmpeg = tool("ffprobe"), tool("ffmpeg")
    done = 0
    placeholders = ",".join("?" * len(WORK_KINDS))
    while time.monotonic() < deadline:
        rows = conn.execute(
            f"SELECT id, path, kind FROM files WHERE meta_done = 0 AND missing = 0 AND kind IN ({placeholders}) "
            "ORDER BY saved_at DESC LIMIT 20", WORK_KINDS).fetchall()
        if not rows:
            break
        for row in rows:
            if time.monotonic() > deadline:
                break
            path = Path(row["path"])
            if not path.is_file():
                conn.execute("UPDATE files SET missing = 1 WHERE id = ?", (row["id"],))
                continue
            info = probe(path, ffprobe)
            fields = {"meta_done": 1, **{k: v for k, v in info.items() if k in ("width", "height", "duration", "codec", "bitrate", "created")}}
            duration = float(info.get("duration") or 0)
            if fingerprints and ffmpeg:
                if row["kind"] == "image":
                    fields["phash"] = image_hash(path, ffmpeg)
                elif row["kind"] == "video":
                    fields["vhash"] = video_hash(path, ffmpeg, duration)
                elif row["kind"] == "audio":
                    fields["ahash"] = audio_hash(path, ffmpeg)
                fields["fp_done"] = 1
            if thumbs is not None and row["kind"] in ("image", "video"):
                fields["thumb"] = thumbnail(path, ffmpeg, thumbs, int(row["id"]), row["kind"], duration)
            conn.execute(f"UPDATE files SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), row["id"]))
            done += 1
        conn.commit()
    left = int(conn.execute(
        f"SELECT COUNT(*) FROM files WHERE meta_done = 0 AND missing = 0 AND kind IN ({placeholders})", WORK_KINDS).fetchone()[0])
    return {"ok": True, "done": done, "left": left, "quick": quick, "ffmpeg": bool(ffmpeg and ffprobe)}


# ─── browsing ────────────────────────────────────────────────────────────────

SORTS = {
    "newest": "saved_at DESC", "oldest": "saved_at ASC", "name": "name COLLATE NOCASE ASC",
    "largest": "size DESC", "smallest": "size ASC", "longest": "duration DESC",
}
GROUPS = {"creator": "creator", "album": "album", "host": "host", "date": "date(saved_at, 'unixepoch', 'localtime')",
          "kind": "kind", "folder": "folder"}


def _filters(query: dict) -> tuple[str, list]:
    where, args = [], []
    if query.get("missing") == "only":
        where.append("missing = 1")
    elif not query.get("missing"):
        where.append("missing = 0")
    kind = str(query.get("kind") or "")
    if kind in (*MEDIA_TYPES, "other"):
        where.append("kind = ?")
        args.append(kind)
    for key in ("host", "creator", "album"):
        value = str(query.get(key) or "")
        if value:
            where.append(f"{key} = ?")
            args.append(value)
    if query.get("folder"):
        where.append("(folder = ? OR folder LIKE ? ESCAPE '\\')")
        folder = str(query["folder"]).rstrip("\\/")
        args += [folder, _like(folder) + os.sep + "%"]
    for word in str(query.get("text") or "").split()[:8]:
        pattern = f"%{_like(word)}%"
        where.append("(name LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR creator LIKE ? ESCAPE '\\' "
                     "OR album LIKE ? ESCAPE '\\' OR host LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\')")
        args += [pattern] * 6
    if query.get("date"):
        where.append("date(saved_at, 'unixepoch', 'localtime') = ?")
        args.append(str(query["date"])[:10])
    if query.get("minWidth"):
        where.append("width >= ?")
        args.append(int(query["minWidth"]))
    return (" WHERE " + " AND ".join(where)) if where else "", args


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


FIELDS = ("id, path, name, kind, ext, size, width, height, duration, codec, bitrate, created, thumb, url, host, "
          "source, creator, album, title, saved_at, missing, folder")


def _row(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def query(conn: sqlite3.Connection, body: dict) -> dict:
    where, args = _filters(body)
    total = int(conn.execute(f"SELECT COUNT(*) FROM files{where}", args).fetchone()[0])
    order = SORTS.get(str(body.get("sort") or "newest"), SORTS["newest"])
    group = GROUPS.get(str(body.get("group") or ""))
    offset = max(0, int(body.get("offset") or 0))
    limit = max(1, min(500, int(body.get("limit") or 200)))
    if group:
        heads = conn.execute(
            f"SELECT {group} AS g, COUNT(*) AS n, SUM(size) AS bytes FROM files{where} GROUP BY g ORDER BY n DESC LIMIT 300",
            args).fetchall()
        groups = [{"key": r["g"] or "", "count": int(r["n"]), "bytes": int(r["bytes"] or 0)} for r in heads]
        rows = conn.execute(f"SELECT {FIELDS}, {group} AS g FROM files{where} ORDER BY g, {order} LIMIT ? OFFSET ?",
                            (*args, limit, offset)).fetchall()
        return {"ok": True, "total": total, "groups": groups, "items": [_row(r) for r in rows]}
    rows = conn.execute(f"SELECT {FIELDS} FROM files{where} ORDER BY {order} LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    return {"ok": True, "total": total, "items": [_row(r) for r in rows]}


def facets(conn: sqlite3.Connection) -> dict:
    def top(column: str) -> list[str]:
        return [r[0] for r in conn.execute(
            f"SELECT {column} FROM files WHERE {column} != '' AND missing = 0 GROUP BY {column} ORDER BY COUNT(*) DESC LIMIT 200")]

    counts = {r["kind"]: int(r["n"]) for r in conn.execute("SELECT kind, COUNT(*) AS n FROM files WHERE missing = 0 GROUP BY kind")}
    missing = int(conn.execute("SELECT COUNT(*) FROM files WHERE missing = 1").fetchone()[0])
    pending = int(conn.execute("SELECT COUNT(*) FROM files WHERE meta_done = 0 AND missing = 0 AND kind IN ('image','video','audio')").fetchone()[0])
    return {"ok": True, "hosts": top("host"), "creators": top("creator"), "albums": top("album"), "kinds": counts,
            "missing": missing, "pending": pending}


def get_file(conn: sqlite3.Connection, row_id: int) -> dict | None:
    row = conn.execute(f"SELECT {FIELDS}, sha256, phash FROM files WHERE id = ?", (int(row_id),)).fetchone()
    return _row(row) if row else None


def moved(conn: sqlite3.Connection, row_id: int, new_path: str) -> dict:
    target = Path(new_path)
    if not target.is_file():
        conn.execute("UPDATE files SET missing = 1 WHERE id = ?", (int(row_id),))
    else:
        stat = target.stat()
        conn.execute("UPDATE files SET path = ?, folder = ?, name = ?, ext = ?, mtime = ?, missing = 0 WHERE id = ?",
                     (str(target), str(target.parent), target.name, target.suffix.lower().lstrip("."), stat.st_mtime, int(row_id)))
    conn.commit()
    return {"ok": True}


# ─── duplicate groups ─────────────────────────────────────────────────────────


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def join(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _bits(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def exact_groups(conn: sqlite3.Connection, deadline: float) -> list[dict]:
    """Same size first (cheap), then SHA-256 of only those files: identical content under any name or site."""
    sizes = [r[0] for r in conn.execute(
        "SELECT size FROM files WHERE missing = 0 AND size > 0 GROUP BY size HAVING COUNT(*) > 1 ORDER BY size DESC")]
    groups: list[dict] = []
    partial = False
    for size in sizes:
        rows = conn.execute("SELECT id, path, sha256, quick FROM files WHERE size = ? AND missing = 0", (size,)).fetchall()
        by_hash: dict[str, list[int]] = {}
        for row in rows:
            digest = row["sha256"]
            if not digest:
                if time.monotonic() > deadline:
                    partial = True
                    continue
                try:
                    digest = full_hash(Path(row["path"]))
                except OSError:
                    conn.execute("UPDATE files SET missing = 1 WHERE id = ?", (row["id"],))
                    continue
                conn.execute("UPDATE files SET sha256 = ? WHERE id = ?", (digest, row["id"]))
            by_hash.setdefault(digest, []).append(int(row["id"]))
        for digest, ids in by_hash.items():
            if len(ids) > 1:
                groups.append({"reason": "exact", "label": "Identical content (SHA-256)", "detail": digest[:16], "ids": ids})
    conn.commit()
    if partial:
        groups.append({"reason": "partial", "ids": []})
    return groups


def similar_groups(conn: sqlite3.Connection, threshold: int = SIMILAR_IMAGE) -> list[dict]:
    """Images whose hashes differ in at most `threshold` bits, videos with matching frames and duration,
    and audio with matching loudness patterns. Exact copies are listed by exact_groups instead."""
    out: list[dict] = []
    threshold = max(0, min(12, int(threshold)))
    rows = conn.execute("SELECT id, phash, sha256 FROM files WHERE phash != '' AND missing = 0").fetchall()
    union = _Union()
    distance: dict[int, int] = {}
    # pigeonhole: two hashes at most `threshold` bits apart agree on at least one of threshold + 1 bands
    bands = threshold + 1
    edges = [round(64 * i / bands) for i in range(bands + 1)]
    buckets: dict[tuple[int, int], list[int]] = {}
    hashes = {int(r["id"]): r["phash"] for r in rows}
    for row_id, value in hashes.items():
        number = int(value, 16)
        for band in range(bands):
            low, high = edges[band], edges[band + 1]
            buckets.setdefault((band, (number >> low) & ((1 << (high - low)) - 1)), []).append(row_id)
    compared: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > 2000:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair = (a, b) if a < b else (b, a)
                if pair in compared:
                    continue
                compared.add(pair)
                bits = _bits(hashes[a], hashes[b])
                if bits <= threshold:
                    union.join(a, b)
                    for x in pair:
                        distance[x] = max(distance.get(x, 0), bits)
    out += _collect(union, "similar-image", "Similar images", _image_detail(distance))

    videos = conn.execute("SELECT id, vhash, duration FROM files WHERE vhash != '' AND missing = 0 ORDER BY duration").fetchall()
    union = _Union()
    for i, a in enumerate(videos):
        for b in videos[i + 1:]:
            if b["duration"] > a["duration"] * 1.02 + 0.5:
                break
            frames = list(zip(a["vhash"].split(","), b["vhash"].split(",")))
            if frames and sum(_bits(x, y) for x, y in frames) / len(frames) <= SIMILAR_VIDEO:
                union.join(int(a["id"]), int(b["id"]))
    out += _collect(union, "similar-video", "Same video (frames and length match)", lambda ids: "")

    audio = conn.execute("SELECT id, ahash, duration FROM files WHERE ahash != '' AND missing = 0 ORDER BY duration").fetchall()
    union = _Union()
    for i, a in enumerate(audio):
        for b in audio[i + 1:]:
            if b["duration"] > a["duration"] * 1.02 + 0.5:
                break
            if audio_distance(a["ahash"], b["ahash"]) <= SIMILAR_AUDIO:
                union.join(int(a["id"]), int(b["id"]))
    out += _collect(union, "similar-audio", "Same recording (loudness pattern and length match)", lambda ids: "")
    return out


def _image_detail(distance: dict[int, int]):
    def detail(ids: list[int]) -> str:
        bits = max(distance.get(i, 0) for i in ids)
        return "the same picture at another size or quality" if bits == 0 else f"up to {bits} of 64 picture bits differ"
    return detail


def audio_distance(a: str, b: str, shift: int = 5) -> float:
    """Share of differing bits at the best alignment within ±0.5 s."""
    best = 1.0
    for offset in range(-shift, shift + 1):
        x, y = (a[offset:], b) if offset >= 0 else (a, b[-offset:])
        n = min(len(x), len(y))
        if n < 100:
            continue
        diff = sum(1 for i in range(n) if x[i] != y[i])
        best = min(best, diff / n)
    return best


def _collect(union: _Union, reason: str, label: str, detail) -> list[dict]:
    groups: dict[int, list[int]] = {}
    for item in list(union.parent):
        groups.setdefault(union.find(item), []).append(item)
    return [{"reason": reason, "label": label, "detail": detail(ids), "ids": sorted(ids)} for ids in groups.values() if len(ids) > 1]


def groups(conn: sqlite3.Connection, body: dict) -> dict:
    kind = str(body.get("kind") or "exact")
    deadline = time.monotonic() + float(body.get("seconds") or 60)
    found = exact_groups(conn, deadline) if kind == "exact" else similar_groups(conn, int(body.get("threshold") or SIMILAR_IMAGE))
    partial = any(g["reason"] == "partial" for g in found)
    found = [g for g in found if g["reason"] != "partial"]
    if kind != "exact":  # a similar group that is entirely one exact copy set is already in the exact list
        found = [g for g in found if len({_sha(conn, i) or i for i in g["ids"]}) > 1]
    text = str(body.get("text") or "").lower().split()
    result = []
    for group in found:
        files = [get_file(conn, i) for i in group["ids"]]
        files = [f for f in files if f]
        if len(files) < 2:
            continue
        if text and not all(any(w in f"{f['name']} {f['host']} {f['creator']} {f['album']} {f['url']}".lower() for f in files) for w in text):
            continue
        files.sort(key=lambda f: (f["saved_at"] or 0))
        hosts = sorted({f["host"] for f in files if f["host"]})
        result.append({**group, "files": files, "hosts": hosts, "crossSite": len(hosts) > 1,
                       "names": len({f["name"] for f in files}) > 1,
                       "wasted": sum(f["size"] for f in files[1:])})
    result.sort(key=lambda g: -g["wasted"])
    return {"ok": True, "groups": result[:500], "total": len(result), "partial": partial}


def _sha(conn: sqlite3.Connection, row_id: int) -> str:
    row = conn.execute("SELECT sha256 FROM files WHERE id = ?", (row_id,)).fetchone()
    return row[0] if row else ""


# ─── storage ─────────────────────────────────────────────────────────────────


def storage(conn: sqlite3.Connection, roots: list[str]) -> dict:
    disks = []
    seen = set()
    for root in roots:
        path = Path(root)
        probe_path = path
        while not probe_path.exists() and probe_path.parent != probe_path:
            probe_path = probe_path.parent
        try:
            usage = shutil.disk_usage(probe_path)
        except OSError:
            continue
        key = (usage.total, usage.free) if os.name != "nt" else (probe_path.drive or str(probe_path)).upper()
        if key in seen:
            continue
        seen.add(key)
        disks.append({"path": str(path), "total": usage.total, "free": usage.free, "used": usage.used})

    def split(column: str, limit: int = 12) -> list[dict]:
        return [{"key": r["k"] or "", "count": int(r["n"]), "bytes": int(r["b"] or 0)} for r in conn.execute(
            f"SELECT {column} AS k, COUNT(*) AS n, SUM(size) AS b FROM files WHERE missing = 0 GROUP BY k ORDER BY b DESC LIMIT ?",
            (limit,))]

    total = conn.execute("SELECT COUNT(*), SUM(size) FROM files WHERE missing = 0").fetchone()
    leftovers = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        count = 0
        for dirpath, dirnames, names in os.walk(base):
            for name in names:
                lower = name.lower()
                if lower.endswith((".part", ".nxmeta")) or ".part.seg" in lower:
                    full = Path(dirpath) / name
                    try:
                        stat = full.stat()
                    except OSError:
                        continue
                    if time.time() - stat.st_mtime > 7 * 86400:
                        leftovers.append({"path": str(full), "bytes": stat.st_size, "age": int(time.time() - stat.st_mtime)})
                        count += 1
            if count > 200:
                break
    empty = []
    for root in roots:
        base = Path(root)
        if base.is_dir():
            for dirpath, dirnames, names in os.walk(base, topdown=False):
                if not dirnames and not names and Path(dirpath) != base:
                    empty.append(dirpath)
                if len(empty) >= 200:
                    break
    return {"ok": True, "disks": disks, "files": int(total[0] or 0), "bytes": int(total[1] or 0),
            "byHost": split("host"), "byKind": split("kind"), "byFolder": split("folder", 15), "byCreator": split("creator"),
            "leftovers": leftovers[:200], "emptyFolders": empty[:200]}


# ─── command line ────────────────────────────────────────────────────────────


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: library.py sync|work|query|groups|facets|storage|file|moved <library.sqlite>")
    command, db = sys.argv[1], Path(sys.argv[2])
    try:
        body = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    conn = connect(db)
    try:
        if command == "sync":
            touched = sync_history(conn, Path(str(body.get("jobs") or ""))) if body.get("jobs") else 0
            roots = [str(r) for r in body.get("roots") or [] if str(r)]
            result = {"ok": True, "history": touched, **scan_roots(conn, roots)}
        elif command == "work":
            thumbs = Path(str(body["thumbs"])) if body.get("thumbs") else None
            result = work(conn, float(body.get("seconds") or 20), thumbs, body.get("fingerprints", True) is not False)
        elif command == "query":
            result = query(conn, body)
        elif command == "groups":
            result = groups(conn, body)
        elif command == "facets":
            result = facets(conn)
        elif command == "storage":
            result = storage(conn, [str(r) for r in body.get("roots") or [] if str(r)])
        elif command == "file":
            item = get_file(conn, int(body.get("id") or 0))
            result = {"ok": bool(item), "file": item}
        elif command == "moved":
            result = moved(conn, int(body.get("id") or 0), str(body.get("path") or ""))
        else:
            result = {"ok": False, "error": f"Unknown command {command}"}
    except (OSError, sqlite3.Error) as error:
        result = {"ok": False, "error": str(error)[:300]}
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
