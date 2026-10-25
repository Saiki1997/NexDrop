"""Local job database for the Electron app. Downloads stay on disk if history is cleared."""

from __future__ import annotations

import json
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
        self.conn.execute("PRAGMA user_version = 1")
        self.conn.commit()

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
    elif sys.argv[1] == "locate":
        print(json.dumps(store.locate(int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 0), ensure_ascii=False))
    else:
        raise SystemExit(f"Unknown command {sys.argv[1]}")
    store.close()


if __name__ == "__main__":
    main()
