"""Local job database for the Electron app. Downloads stay on disk if history is cleared."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import error_classes  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL,
    source_urls TEXT NOT NULL,
    download_dir TEXT NOT NULL,
    stop_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT '',
    sequence INTEGER NOT NULL DEFAULT 0,
    filename TEXT NOT NULL DEFAULT '',
    destination TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    bytes_read INTEGER NOT NULL DEFAULT 0,
    bytes_total INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    UNIQUE (job_id, url)
);
CREATE INDEX IF NOT EXISTS items_job_status ON items (job_id, status);
CREATE INDEX IF NOT EXISTS items_job_sequence ON items (job_id, sequence);
"""

# 3.0: History sorts newest first and filters by status and site without reading every row.
INDEXES = """
CREATE INDEX IF NOT EXISTS items_updated ON items (updated_at);
CREATE INDEX IF NOT EXISTS items_status_updated ON items (status, updated_at);
CREATE INDEX IF NOT EXISTS items_host ON items (host);
CREATE INDEX IF NOT EXISTS items_url ON items (url);
"""
MEDIA_TYPES = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "avif", "heic", "heif", "bmp", "tif", "tiff", "jxl", "svg"},
    "video": {"mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "ts", "m2ts", "3gp", "mpg", "mpeg"},
    "audio": {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma", "alac"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst"},
}


def file_type(filename: str | None, url: str | None = "") -> str:
    """image, video, audio, archive or other, from the saved name (or the link when there is none)."""
    name = str(filename or "") or str(url or "").split("?")[0]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for kind, extensions in MEDIA_TYPES.items():
        if ext in extensions:
            return kind
    return "other"


def _cell(value) -> object:
    """A CSV cell that a spreadsheet won't run as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


COMMIT_EVERY = 1.0  # seconds; file events are committed in batches, and always at the end of a run



class JobStore:
    def __init__(self, path: Path, *, check: bool = True) -> None:
        """`check` runs an integrity check first (a download run does; History searches don't)."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.moved_aside: Path | None = None
        if check and self.path.exists() and self._health() == "corrupt":
            self.moved_aside = self._move_aside()
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        # WAL keeps per-file updates cheap and lets the window read while a run writes.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self.conn.executescript(SCHEMA)
        self._add_naming_columns()
        self.conn.executescript(INDEXES)
        self._register_functions()
        self.conn.execute("PRAGMA user_version = 4")
        self.conn.commit()
        self._dirty = 0
        self._last_commit = time.monotonic()

    # Smart naming record per file (2.5). Older databases get the columns added; rows they
    # already have keep empty values.
    NAMING_COLUMNS = {
        "original_name": "TEXT NOT NULL DEFAULT ''",
        "folder": "TEXT NOT NULL DEFAULT ''",
        "title": "TEXT NOT NULL DEFAULT ''",
        "parent_title": "TEXT NOT NULL DEFAULT ''",
        "name_source": "TEXT NOT NULL DEFAULT ''",
        "name_confidence": "REAL NOT NULL DEFAULT 0",
        "name_strategy": "TEXT NOT NULL DEFAULT ''",
        # 2.6 (smart naming V2): what the file is and where it came from, as JSON. Links have
        # no query strings. Used to learn from names you change in History.
        "identity": "TEXT NOT NULL DEFAULT ''",
        # 3.0: why a file failed, as one category (error_classes.py)
        "error_category": "TEXT NOT NULL DEFAULT ''",
    }

    def _add_naming_columns(self) -> None:
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(items)")}
        for name, kind in self.NAMING_COLUMNS.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {name} {kind}")

    def close(self) -> None:
        self.flush()
        self.conn.close()

    def flush(self) -> None:
        """Commit file events that are still waiting for their batch."""
        if self._dirty:
            self.conn.commit()
            self._dirty = 0
        self._last_commit = time.monotonic()

    def _changed(self) -> None:
        self._dirty += 1
        if time.monotonic() - self._last_commit >= COMMIT_EVERY:
            self.flush()

    def _health(self) -> str:
        """'ok', 'corrupt', or 'unknown' (busy or locked: another NexDrop process has it open)."""
        try:
            connection = sqlite3.connect(self.path, timeout=5)
            try:
                row = connection.execute("PRAGMA quick_check").fetchone()
            finally:
                connection.close()
        except sqlite3.OperationalError as error:
            text = str(error).lower()
            if "locked" in text or "busy" in text or "unable to open" in text:
                return "unknown"  # never move a database aside just because it is in use
            return "corrupt"
        except sqlite3.DatabaseError:
            return "corrupt"
        return "ok" if row and row[0] == "ok" else "corrupt"

    def _healthy(self) -> bool:
        return self._health() != "corrupt"

    def _move_aside(self) -> Path | None:
        """Keep a damaged database (and its journal files) under a new name; a fresh one is created."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        broken = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
        try:
            self.path.replace(broken)
        except OSError:
            return None  # in use: keep working with it rather than lose it
        for extra in ("-wal", "-shm"):
            side = self.path.with_name(self.path.name + extra)
            if side.exists():
                try:
                    side.replace(broken.with_name(broken.name + extra))
                except OSError:
                    pass
        return broken

    def start_job(self, urls: list[str], download_dir: str) -> int:
        now = time.time()
        cursor = self.conn.execute(
            "INSERT INTO jobs(created_at, updated_at, status, source_urls, download_dir) VALUES(?, ?, 'running', ?, ?)",
            (now, now, "\n".join(urls), download_dir),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_job(self, job_id: int, status: str, reason: str = "") -> None:
        self.flush()
        self.conn.execute(
            "UPDATE jobs SET status = ?, stop_reason = ?, updated_at = ? WHERE id = ?",
            (status, reason[:500], time.time(), job_id),
        )
        self.conn.commit()

    def note(self, job_id: int, payload: dict) -> None:
        url = str(payload.get("url") or "")
        if not url:
            return
        event = str(payload.get("event") or "")
        status = {
            "success": "completed",
            "skipped": "skipped",
            "failure": "failed",
            "retry": "retrying",
            "cancelled": "cancelled",
        }.get(event, "queued")
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO items(job_id, url, host, sequence, filename, destination, status, bytes_read, bytes_total, retries, error, updated_at,
                              error_category)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, url) DO UPDATE SET
                host = CASE WHEN excluded.host != '' THEN excluded.host ELSE items.host END,
                sequence = CASE WHEN items.sequence > 0 THEN items.sequence ELSE excluded.sequence END,
                filename = CASE WHEN excluded.filename != '' THEN excluded.filename ELSE items.filename END,
                destination = CASE WHEN excluded.destination != '' THEN excluded.destination ELSE items.destination END,
                status = excluded.status,
                bytes_read = CASE WHEN excluded.bytes_read > 0 THEN excluded.bytes_read ELSE items.bytes_read END,
                bytes_total = CASE WHEN excluded.bytes_total > 0 THEN excluded.bytes_total ELSE items.bytes_total END,
                retries = items.retries + CASE WHEN excluded.status = 'retrying' THEN 1 ELSE 0 END,
                error = excluded.error,
                error_category = excluded.error_category,
                updated_at = excluded.updated_at
            """,
            (
                job_id,
                url,
                str(payload.get("host") or ""),
                int(payload.get("sequence") or 0),
                str(payload.get("name") or ""),
                str(payload.get("destination") or "")[:1000],
                status,
                int(payload.get("bytes_read") or 0),
                int(payload.get("bytes_total") or 0),
                1 if status == "retrying" else 0,
                "" if status != "failed" else str(payload.get("status") or "")[:300],
                now,
                "" if status != "failed" else str(payload.get("category") or "")[:40],
            ),
        )
        naming = payload.get("naming")
        if isinstance(naming, dict):
            try:
                confidence = float(naming.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            identity = naming.get("identity")
            identity = json.dumps(identity, ensure_ascii=False)[:4000] if isinstance(identity, dict) else ""
            self.conn.execute(
                """
                UPDATE items SET original_name = ?, folder = ?, title = ?, parent_title = ?, name_source = ?,
                    name_confidence = ?, name_strategy = ?,
                    identity = CASE WHEN ? != '' THEN ? ELSE identity END
                WHERE job_id = ? AND url = ? AND (original_name = '' OR identity = '')
                """,
                (
                    str(naming.get("original") or "")[:500],
                    str(naming.get("folder") or "")[:1000],
                    str(naming.get("title") or "")[:300],
                    str(naming.get("parent") or "")[:300],
                    str(naming.get("source") or "")[:100],
                    confidence,
                    str(naming.get("strategy") or "")[:20],
                    identity,
                    identity,
                    job_id,
                    url,
                ),
            )
        self._changed()

    def recover(self) -> list[int]:
        rows = self.conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return []
        now = time.time()
        self.conn.executemany(
            "UPDATE jobs SET status = 'interrupted', stop_reason = 'closed while running', updated_at = ? WHERE id = ?",
            [(now, job_id) for job_id in ids],
        )
        marks = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE items SET status = 'interrupted', updated_at = ? WHERE job_id IN ({marks}) AND status IN ('queued', 'downloading', 'retrying')",
            (now, *ids),
        )
        self.conn.commit()
        return ids

    def backup(self, dest: Path) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        self.conn.backup(target)
        target.close()

    def counts(self, job_id: int) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM items WHERE job_id = ? GROUP BY status",
            (job_id,),
        ).fetchall()
        found = {str(row["status"]): int(row["n"]) for row in rows}
        found["total"] = sum(found.values())
        return found


    def _where(self, query: dict) -> tuple[str, list]:
        """SQL filter for a History query: text words, site, result, age in days and file type."""
        where = []
        args: list = []
        status = str(query.get("status") or "completed")
        if status in {"completed", "failed", "skipped"}:
            where.append("items.status = ?")
            args.append(status)
        elif status == "missing":
            where.append("items.status = 'completed' AND items.destination != ''")
        host = str(query.get("host") or "")
        if host:
            where.append("items.host = ?")
            args.append(host.lower().removeprefix("www."))
        try:
            days = float(query.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        if days > 0:
            where.append("items.updated_at >= ?")
            args.append(time.time() - days * 86400)
        kind = str(query.get("type") or "")
        if kind in MEDIA_TYPES or kind == "other":
            where.append("nx_type(items.filename, items.url) = ?")
            args.append(kind)
        for word in str(query.get("text") or "").split()[:8]:
            pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(items.filename LIKE ? ESCAPE '\\' OR items.url LIKE ? ESCAPE '\\' OR items.host LIKE ? ESCAPE '\\'"
                " OR jobs.source_urls LIKE ? ESCAPE '\\')"
            )
            args.extend([pattern] * 4)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return f"FROM items JOIN jobs ON jobs.id = items.job_id {clause}", args

    def _register_functions(self) -> None:
        self.conn.create_function("nx_type", 2, file_type, deterministic=True)

    ROW_FIELDS = """items.id, items.url, items.host, items.filename, items.destination, items.status,
                    items.bytes_total, items.bytes_read, items.updated_at, items.error, items.error_category,
                    jobs.source_urls, jobs.download_dir"""
    MISSING_SCAN = 5000  # "Saved, file missing" checks the newest files this many at most

    def search(self, text: str = "", host: str = "", status: str = "completed", limit: int = 200,
               days: float = 0, kind: str = "") -> dict:
        """Files from earlier runs, newest first. Words must all appear in the name, link, site or source link."""
        query = {"text": text, "host": host, "status": status, "days": days, "type": kind}
        base, args = self._where(query)
        limit = max(1, min(500, int(limit)))
        note = ""
        if status == "missing":
            rows = self.conn.execute(
                f"SELECT {self.ROW_FIELDS} {base} ORDER BY items.updated_at DESC LIMIT ?", (*args, self.MISSING_SCAN)
            ).fetchall()
            gone = [row for row in rows if not Path(str(row["destination"])).exists()]
            total = len(gone)
            if len(rows) >= self.MISSING_SCAN:
                note = f"Checked the newest {self.MISSING_SCAN:,} saved files."
            rows = gone[:limit]
        else:
            total = int(self.conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0])
            rows = self.conn.execute(
                f"SELECT {self.ROW_FIELDS} {base} ORDER BY items.updated_at DESC LIMIT ?", (*args, limit)
            ).fetchall()
        items = [self._item(row) for row in rows]
        hosts = [r[0] for r in self.conn.execute(
            "SELECT host FROM items WHERE host != '' GROUP BY host ORDER BY COUNT(*) DESC LIMIT 60").fetchall()]
        return {"ok": True, "total": total, "items": items, "hosts": hosts, "note": note}

    @staticmethod
    def _item(row) -> dict:
        sources = [line for line in str(row["source_urls"]).split("\n") if line]
        item = {
            "id": int(row["id"]),
            "url": row["url"],
            "host": row["host"],
            "name": row["filename"] or Path(str(row["url"]).split("?")[0]).name,
            "bytes": int(row["bytes_total"] or row["bytes_read"] or 0),
            "status": row["status"],
            "at": float(row["updated_at"]),
            "source": sources[0] if sources else "",
            "hasPath": bool(row["destination"]),
        }
        if row["status"] == "completed" and row["destination"]:
            item["missing"] = not Path(str(row["destination"])).exists()
        if row["status"] == "failed":
            category = row["error_category"] or error_classes.classify(str(row["error"] or ""), str(row["url"]))
            item.update(error=str(row["error"] or "")[:300], category=category,
                        label=error_classes.LABELS.get(category, error_classes.LABELS["other"]))
        return item

    EXPORT_MAX = 100_000

    def export_csv(self, query: dict, dest: Path) -> int:
        """Every file matching a History query (newest first, at most EXPORT_MAX) as CSV. Returns the row count."""
        import csv
        import datetime

        base, args = self._where(query)
        rows = self.conn.execute(
            f"SELECT {self.ROW_FIELDS} {base} ORDER BY items.updated_at DESC LIMIT ?", (*args, self.EXPORT_MAX)
        ).fetchall()
        missing_only = str(query.get("status")) == "missing"
        count = 0
        with dest.open("w", encoding="utf-8-sig", newline="") as handle:
            out = csv.writer(handle)
            out.writerow(["date", "result", "file", "site", "bytes", "link", "pasted link", "problem", "details", "path"])
            for row in rows:
                if missing_only and Path(str(row["destination"])).exists():
                    continue
                item = self._item(row)
                when = datetime.datetime.fromtimestamp(item["at"]).strftime("%Y-%m-%d %H:%M:%S")
                result = "saved, file missing" if item.get("missing") else item["status"]
                out.writerow([_cell(v) for v in (when, result, item["name"], item["host"], item["bytes"], item["url"],
                                                 item["source"], item.get("label", ""), item.get("error", ""),
                                                 row["destination"])])
                count += 1
        return count

    def health(self, days: float = 30) -> list[dict]:
        """Per site over the last `days`: files saved and failed, the main reasons, the last success."""
        since = time.time() - days * 86400
        rows = self.conn.execute(
            """SELECT host, status, error_category, error, url, COUNT(*) AS n, MAX(updated_at) AS last,
                      SUM(CASE WHEN status = 'completed' THEN bytes_total ELSE 0 END) AS bytes
               FROM items WHERE host != '' AND updated_at >= ?
               GROUP BY host, status, error_category""",
            (since,),
        ).fetchall()
        sites: dict[str, dict] = {}
        for row in rows:
            site = sites.setdefault(row["host"], {"host": row["host"], "saved": 0, "failed": 0, "skipped": 0,
                                                  "bytes": 0, "lastSaved": 0.0, "lastFailed": 0.0, "reasons": {}})
            n = int(row["n"])
            if row["status"] == "completed":
                site["saved"] += n
                site["bytes"] += int(row["bytes"] or 0)
                site["lastSaved"] = max(site["lastSaved"], float(row["last"] or 0))
            elif row["status"] == "failed":
                site["failed"] += n
                site["lastFailed"] = max(site["lastFailed"], float(row["last"] or 0))
                category = row["error_category"] or error_classes.classify(str(row["error"] or ""), str(row["url"]))
                site["reasons"][category] = site["reasons"].get(category, 0) + n
            elif row["status"] == "skipped":
                site["skipped"] += n
        out = []
        for site in sites.values():
            done = site["saved"] + site["failed"]
            site["rate"] = round(site["saved"] / done, 3) if done else None
            reasons = sorted(site.pop("reasons").items(), key=lambda kv: -kv[1])[:3]
            site["reasons"] = [{"category": c, "label": error_classes.LABELS.get(c, c), "count": n,
                                "action": error_classes.ACTIONS.get(c, "")} for c, n in reasons]
            # "working" needs a recent success; a site whose files mostly fail is "failing"
            if site["rate"] is None:
                site["state"] = "unknown"
            elif site["rate"] >= 0.9:
                site["state"] = "working"
            elif site["rate"] >= 0.5 or site["lastSaved"] >= site["lastFailed"]:
                site["state"] = "unstable"
            else:
                site["state"] = "failing"
            out.append(site)
        out.sort(key=lambda s: -(s["saved"] + s["failed"]))
        return out[:60]

    def locate(self, item_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT items.destination, jobs.download_dir FROM items JOIN jobs ON jobs.id = items.job_id WHERE items.id = ?",
            (int(item_id),),
        ).fetchone()
        if row is None:
            return None
        return {"destination": row["destination"], "folder": row["download_dir"]}


    # ─── renaming a file from History (smart naming V2 learns from it) ─────────

    def rename(self, item_id: int, new_name: str, corrections: Path | None) -> dict:
        """Rename a downloaded file and, when `corrections` is set, remember the name for its post or album."""
        row = self.conn.execute(
            """SELECT items.filename, items.destination, items.sequence, items.title, items.name_source, items.identity,
                      jobs.download_dir FROM items JOIN jobs ON jobs.id = items.job_id WHERE items.id = ?""",
            (int(item_id),),
        ).fetchone()
        if row is None or not row["destination"]:
            return {"ok": False, "error": "That file is not in the history."}
        old = Path(row["destination"])
        root = Path(row["download_dir"])
        try:
            old.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return {"ok": False, "error": "That file is outside its download folder."}
        if not old.is_file():
            return {"ok": False, "error": "The file is no longer where it was saved."}
        name = clean_filename(new_name, old.suffix)
        if not name:
            return {"ok": False, "error": "Type a name."}
        target = old.with_name(name)
        if target == old:
            return {"ok": True, "name": name, "learned": False}
        if target.exists():
            return {"ok": False, "error": f"{name} already exists in that folder."}
        old.rename(target)
        self.conn.execute("UPDATE items SET filename = ?, destination = ?, updated_at = ? WHERE id = ?",
                          (name, str(target), time.time(), int(item_id)))
        self.conn.commit()
        learned = False
        if corrections is not None:
            learned = learn_correction(corrections, row, name)
        return {"ok": True, "name": name, "learned": learned}


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)


def clean_filename(text: str, extension: str) -> str:
    """A Windows-safe file name that keeps the file's extension."""
    name = _BAD_CHARS.sub("-", " ".join(str(text or "").split())).strip().rstrip(". ")
    if not name:
        return ""
    if extension and not name.lower().endswith(extension.lower()):
        name += extension
    if _RESERVED.match(name):
        name = f"_{name}"
    return name[:200]


_SOURCE_CONTEXT = {  # the name came from ... so the correction belongs to ...
    "caption": "post", "post title": "post", "forum post": "post", "tweet": "post", "post description": "post",
    "album title": "album", "gallery/creator name": "profile", "thread title": "thread",
}


def strip_number(stem: str, sequence: int) -> str:
    """"Beach Day-004" -> "Beach Day"; "004_Beach Day" -> "Beach Day" (only this file's own number)."""
    if sequence > 0:
        for pattern in (rf"[-_ ]0*{sequence}$", rf"^0*{sequence}[-_ ]"):
            stem = re.sub(pattern, "", stem)
    return stem.strip(" -_.")


def learn_correction(path: Path, row, new_name: str) -> bool:
    """Write {"context", "title", "use"} so the next files with this title from the same post,
    album or thread get your name. Nothing is learned without such a place."""
    try:
        identity = json.loads(row["identity"] or "{}")
    except ValueError:
        identity = {}
    contexts = identity.get("context") if isinstance(identity, dict) else None
    title = str(row["title"] or "")
    if not isinstance(contexts, dict) or not contexts or not title:
        return False
    kind = _SOURCE_CONTEXT.get(str(row["name_source"] or "").split(" (")[0], "")
    for candidate in (kind, "post", "album", "thread", "profile"):
        if candidate and contexts.get(candidate):
            context = str(contexts[candidate])
            break
    else:
        return False
    use = strip_number(Path(new_name).stem, int(row["sequence"] or 0))
    if not use or use.casefold() == title.casefold():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"context": context, "title": title, "use": use, "at": round(time.time())},
                                ensure_ascii=False) + "\n")
    return True


def read_corrections(path: Path) -> list[dict]:
    """The names NexDrop learned, newest rule per place and title."""
    rules: dict[tuple[str, str], dict] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("context") and row.get("title"):
                key = (str(row["context"]), str(row["title"]).casefold())
                if row.get("removed"):
                    rules.pop(key, None)
                else:
                    rules[key] = {"context": row["context"], "title": row["title"], "use": row.get("use", "")}
    return list(rules.values())


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: job_store.py recover|search|locate <jobs.sqlite> [item id]")
    # Only the start-up recovery checks integrity; History searches must stay quick.
    store = JobStore(Path(sys.argv[2]), check=sys.argv[1] == "recover")
    if sys.argv[1] == "recover":
        print(json.dumps({"interrupted": store.recover()}))
    elif sys.argv[1] == "search":
        # The query arrives as one JSON line on stdin, so search words never reach a command line.
        try:
            query = json.loads(sys.stdin.readline() or "{}")
        except json.JSONDecodeError:
            query = {}
        query = query if isinstance(query, dict) else {}
        print(json.dumps(store.search(str(query.get("text") or ""), str(query.get("host") or ""),
                                      str(query.get("status") or "completed"), int(query.get("limit") or 200),
                                      float(query.get("days") or 0), str(query.get("type") or "")), ensure_ascii=False))
    elif sys.argv[1] == "export":
        # {"query": History query, "dest": CSV path chosen in a save dialog} on stdin
        try:
            body = json.loads(sys.stdin.readline() or "{}")
        except json.JSONDecodeError:
            body = {}
        body = body if isinstance(body, dict) else {}
        dest = str(body.get("dest") or "")
        if not dest.lower().endswith(".csv"):
            print(json.dumps({"ok": False, "error": "The export must be a .csv file."}))
        else:
            try:
                count = store.export_csv(body.get("query") if isinstance(body.get("query"), dict) else {}, Path(dest))
                print(json.dumps({"ok": True, "count": count, "path": dest}, ensure_ascii=False))
            except OSError as error:
                print(json.dumps({"ok": False, "error": f"Could not write the file: {error.strerror or error}"}))
    elif sys.argv[1] == "health":
        days = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].replace(".", "", 1).isdigit() else 30
        print(json.dumps({"ok": True, "days": days, "sites": store.health(days)}, ensure_ascii=False))
    elif sys.argv[1] == "rename":
        # {"id": item id, "name": new name, "corrections": path or ""} as one JSON line on stdin
        try:
            query = json.loads(sys.stdin.readline() or "{}")
        except json.JSONDecodeError:
            query = {}
        query = query if isinstance(query, dict) else {}
        corrections = Path(query["corrections"]) if query.get("corrections") else None
        try:
            result = store.rename(int(query.get("id") or 0), str(query.get("name") or ""), corrections)
        except OSError as error:
            result = {"ok": False, "error": f"Could not rename: {error.strerror or error}"}
        print(json.dumps(result, ensure_ascii=False))
    elif sys.argv[1] in ("corrections-list", "corrections-remove", "corrections-clear"):
        # argv[3] is the corrections file; a removal arrives as {"context", "title"} on stdin
        path = Path(sys.argv[3]) if len(sys.argv) > 3 else None
        if path is None:
            raise SystemExit("usage: job_store.py corrections-list <jobs.sqlite> <corrections.jsonl>")
        if sys.argv[1] == "corrections-clear" and path.is_file():
            path.write_text("", encoding="utf-8")
        elif sys.argv[1] == "corrections-remove":
            try:
                query = json.loads(sys.stdin.readline() or "{}")
            except json.JSONDecodeError:
                query = {}
            if isinstance(query, dict) and query.get("context") and query.get("title"):
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"context": str(query["context"]), "title": str(query["title"]), "removed": True},
                                            ensure_ascii=False) + "\n")
        print(json.dumps({"ok": True, "rules": read_corrections(path)}, ensure_ascii=False))
    elif sys.argv[1] == "locate":
        print(json.dumps(store.locate(int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 0), ensure_ascii=False))
    else:
        raise SystemExit(f"Unknown command {sys.argv[1]}")
    store.close()


if __name__ == "__main__":
    main()
