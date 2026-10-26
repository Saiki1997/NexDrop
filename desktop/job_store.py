"""Local job database for the Electron app. Downloads stay on disk if history is cleared."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

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


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self._healthy():
            broken = self.path.with_suffix(".corrupt")
            self.path.replace(broken)
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        # WAL keeps per-file updates cheap and lets the window read while a run writes.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA)
        self._add_naming_columns()
        self.conn.execute("PRAGMA user_version = 3")
        self.conn.commit()

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
    }

    def _add_naming_columns(self) -> None:
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(items)")}
        for name, kind in self.NAMING_COLUMNS.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {name} {kind}")

    def close(self) -> None:
        self.conn.close()

    def _healthy(self) -> bool:
        try:
            connection = sqlite3.connect(self.path)
            row = connection.execute("PRAGMA quick_check").fetchone()
            connection.close()
        except sqlite3.DatabaseError:
            return False
        return bool(row) and row[0] == "ok"

    def start_job(self, urls: list[str], download_dir: str) -> int:
        now = time.time()
        cursor = self.conn.execute(
            "INSERT INTO jobs(created_at, updated_at, status, source_urls, download_dir) VALUES(?, ?, 'running', ?, ?)",
            (now, now, "\n".join(urls), download_dir),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_job(self, job_id: int, status: str, reason: str = "") -> None:
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
            INSERT INTO items(job_id, url, host, sequence, filename, destination, status, bytes_read, bytes_total, retries, error, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self.conn.commit()

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


    def search(self, text: str = "", host: str = "", status: str = "completed", limit: int = 200) -> dict:
        """Files from earlier runs, newest first. Words must all appear in the name, link, site or source link."""
        where = []
        args: list = []
        if status in {"completed", "failed", "skipped"}:
            where.append("items.status = ?")
            args.append(status)
        if host:
            where.append("items.host = ?")
            args.append(host.lower().removeprefix("www."))
        for word in str(text or "").split()[:8]:
            pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(items.filename LIKE ? ESCAPE '\\' OR items.url LIKE ? ESCAPE '\\' OR items.host LIKE ? ESCAPE '\\'"
                " OR jobs.source_urls LIKE ? ESCAPE '\\')"
            )
            args.extend([pattern] * 4)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        base = f"FROM items JOIN jobs ON jobs.id = items.job_id {clause}"
        total = int(self.conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0])
        rows = self.conn.execute(
            f"""SELECT items.id, items.url, items.host, items.filename, items.destination, items.status,
                       items.bytes_total, items.bytes_read, items.updated_at, jobs.source_urls, jobs.download_dir
                {base} ORDER BY items.updated_at DESC LIMIT ?""",
            (*args, max(1, min(500, int(limit)))),
        ).fetchall()
        items = []
        for row in rows:
            sources = [line for line in str(row["source_urls"]).split("\n") if line]
            items.append({
                "id": int(row["id"]),
                "url": row["url"],
                "host": row["host"],
                "name": row["filename"] or Path(str(row["url"]).split("?")[0]).name,
                "bytes": int(row["bytes_total"] or row["bytes_read"] or 0),
                "status": row["status"],
                "at": float(row["updated_at"]),
                "source": sources[0] if sources else "",
                "hasPath": bool(row["destination"]),
            })
        hosts = [r[0] for r in self.conn.execute(
            "SELECT host FROM items WHERE host != '' GROUP BY host ORDER BY COUNT(*) DESC LIMIT 60").fetchall()]
        return {"ok": True, "total": total, "items": items, "hosts": hosts}

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
    store = JobStore(Path(sys.argv[2]))
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
                                      str(query.get("status") or "completed"), int(query.get("limit") or 200)), ensure_ascii=False))
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
