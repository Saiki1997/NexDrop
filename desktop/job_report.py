"""After a job (3.1): verify its files, write its manifest, and the broken-link report.

    job_report.py after   <jobs.sqlite>   stdin: {"job": id, "verify": bool, "manifest": bool, "version": app}
    job_report.py broken  <jobs.sqlite>   stdin: {"days": 30, "links": bool, "dest": file.md, "version": app}

`after` only reads files. Verifying checks that each file the job saved is still there, has the
size the server sent and is not a web page saved under a media name. It can't compare against a
checksum from the site, because the sites don't publish one; the manifest records each file's
SHA-256 so a copy can be checked against it later.

The manifest is one JSON file per job, written beside the downloads as
"NexDrop manifest <date> job <id>.nexdrop.json" (the Library skips these). It never holds
sign-ins, cookies or query strings.

`broken` writes a Markdown report of failed files, grouped by site and reason, for a GitHub issue.
Links are left out unless asked for, and then they are reduced to their pattern ({id} for ids,
no secrets in queries). Nothing is uploaded: the file is saved where the person chose.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import error_classes  # noqa: E402
import source_registry  # noqa: E402

MANIFEST_SUFFIX = ".nexdrop.json"
WEB_PAGE_STARTS = (b"<!doctype html", b"<html", b"<head", b"<?xml")
MEDIA = {"jpg", "jpeg", "png", "gif", "webp", "avif", "heic", "bmp", "tif", "tiff", "jxl", "mp4", "m4v", "mov", "mkv",
         "webm", "avi", "wmv", "flv", "ts", "mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "zip", "rar", "7z", "pdf"}
CHUNK = 1024 * 1024


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _iso(stamp: float | None) -> str:
    if not stamp:
        return ""
    return datetime.fromtimestamp(float(stamp), tz=timezone.utc).replace(microsecond=0).isoformat()


def _plain_url(url: str) -> str:
    """The link without query string or fragment (those can hold tokens)."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return ""
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def _identity(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def check_file(path: Path, expected: int) -> str:
    """'' when the file looks complete, otherwise what is wrong."""
    try:
        size = path.stat().st_size
    except OSError:
        return "missing"
    if size == 0:
        return "empty"
    if expected and size != expected:
        return f"size {size:,} bytes, the server sent {expected:,}"
    if path.suffix.lower().lstrip(".") in MEDIA:
        try:
            with path.open("rb") as handle:
                head = handle.read(512).lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        except OSError:
            return "can't be read"
        if head.startswith(WEB_PAGE_STARTS):
            return "a web page, not the file"
    return ""


def job_rows(conn: sqlite3.Connection, job_id: int) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    rows = conn.execute("SELECT * FROM items WHERE job_id = ? ORDER BY sequence, id", (job_id,)).fetchall() if job else []
    return job, rows


def after(jobs_db: Path, body: dict) -> dict:
    try:
        job_id = int(body.get("job") or 0)
    except (TypeError, ValueError):
        job_id = 0
    if not job_id or not jobs_db.is_file():
        return {"ok": False, "error": "The job is not in History."}
    conn = _connect(jobs_db)
    job, rows = job_rows(conn, job_id)
    conn.close()
    if job is None:
        return {"ok": False, "error": "The job is not in History."}
    out: dict = {"ok": True, "job": job_id, "paths": [r["destination"] for r in rows if r["status"] == "completed" and r["destination"]]}
    want_verify, want_manifest = bool(body.get("verify")), bool(body.get("manifest"))
    files = []
    problems = []
    for row in rows:
        entry = {"url": _plain_url(row["url"]), "status": row["status"], "name": row["filename"] or ""}
        identity = _identity(row["identity"] if "identity" in row.keys() else "")
        for key in ("creator", "album", "title", "post", "thread"):
            if identity.get(key):
                entry[key] = str(identity[key])[:300]
        entry["site"] = row["host"]
        entry["updated"] = _iso(row["updated_at"])
        if row["status"] == "failed":
            entry["error"] = error_classes.LABELS.get(row["error_category"] or "", row["error_category"] or "other") if "error_category" in row.keys() else "failed"
        if row["status"] == "completed" and row["destination"]:
            path = Path(row["destination"])
            try:
                entry["path"] = str(path.relative_to(job["download_dir"]))
            except ValueError:
                entry["path"] = str(path)
            entry["size"] = int(row["bytes_total"] or row["bytes_read"] or 0)
            if want_verify:
                problem = check_file(path, int(row["bytes_total"] or 0))
                if problem:
                    problems.append({"name": path.name, "path": str(path), "problem": problem})
                    entry["verified"] = problem
                else:
                    entry["verified"] = "ok"
            if want_manifest and path.is_file():
                try:
                    entry["sha256"] = sha256(path)
                except OSError:
                    pass
        files.append(entry)
    if want_verify:
        checked = sum(1 for r in rows if r["status"] == "completed" and r["destination"])
        out["verify"] = {"checked": checked, "good": checked - len(problems), "problems": problems[:200]}
    if want_manifest:
        manifest = {
            "nexdrop": str(body.get("version") or ""),
            "engine": source_registry.ENGINE,
            "job": job_id,
            "created": _iso(job["created_at"]),
            "finished": _iso(job["updated_at"]),
            "status": job["status"],
            "sources": [_plain_url(u) for u in str(job["source_urls"] or "").splitlines() if u.strip()],
            "folder": job["download_dir"],
            "counts": {s: sum(1 for f in files if f["status"] == s) for s in ("completed", "failed", "skipped")},
            "files": files,
        }
        folder = Path(job["download_dir"])
        stamp = datetime.fromtimestamp(float(job["updated_at"] or time.time())).strftime("%Y-%m-%d %H%M")
        target = folder / f"NexDrop manifest {stamp} job {job_id}{MANIFEST_SUFFIX}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with open(target, "x", encoding="utf-8") as handle:  # never overwrites
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
            out["manifest"] = str(target)
        except FileExistsError:
            out["manifest"] = str(target)
        except OSError as error:
            out["manifestError"] = f"The manifest could not be written: {error.strerror or error}"
    return out


def broken(jobs_db: Path, body: dict) -> dict:
    try:
        days = max(1.0, min(3650.0, float(body.get("days") or 30)))
    except (TypeError, ValueError):
        days = 30.0
    links = bool(body.get("links"))
    since = time.time() - days * 86400
    groups: dict[tuple[str, str], dict] = {}
    if jobs_db.is_file():
        conn = _connect(jobs_db)
        has_category = "error_category" in [r[1] for r in conn.execute("PRAGMA table_info(items)")]
        category_column = "error_category" if has_category else "''"
        rows = conn.execute(
            f"SELECT host, url, error, {category_column} AS category, updated_at "
            "FROM items WHERE status = 'failed' AND updated_at >= ? ORDER BY updated_at DESC LIMIT 20000", (since,)).fetchall()
        conn.close()
        for row in rows:
            category = row["category"] or error_classes.classify(str(row["error"] or ""), str(row["url"]))
            group = groups.setdefault((row["host"] or "unknown", category), {"count": 0, "patterns": {}, "last": 0.0})
            group["count"] += 1
            group["last"] = max(group["last"], float(row["updated_at"] or 0))
            if links:
                pattern = source_registry.sanitize_url(row["url"])["pattern"]
                group["patterns"][pattern] = group["patterns"].get(pattern, 0) + 1
    lines = [
        "# NexDrop broken-link report",
        "",
        f"- NexDrop {body.get('version') or ''}, engine Cyberdrop-DL {source_registry.ENGINE} (patched)",
        f"- {platform.system()} {platform.release()}",
        f"- Failed files in the last {int(days)} days, grouped by site and reason",
        "- No sign-ins, cookies, file names or folders are included." + ("" if links else " Links are left out."),
        "",
    ]
    if not groups:
        lines.append("No failed files in this period.")
    else:
        lines += ["| Site | Reason | Files | Last seen |", "| --- | --- | ---: | --- |"]
        for (host, category), group in sorted(groups.items(), key=lambda kv: -kv[1]["count"]):
            lines.append(f"| {host} | {error_classes.LABELS.get(category, category)} | {group['count']} | {_iso(group['last'])[:10]} |")
        if links:
            lines += ["", "## Link patterns", "", "Ids are replaced by {id}; query strings that could hold secrets are removed.", ""]
            for (host, category), group in sorted(groups.items(), key=lambda kv: -kv[1]["count"]):
                for pattern, count in sorted(group["patterns"].items(), key=lambda kv: -kv[1])[:5]:
                    lines.append(f"- `{pattern}` ({error_classes.LABELS.get(category, category)}, {count})")
        lines += ["", "## What each reason means", ""]
        for category in sorted({c for _, c in groups}):
            lines.append(f"- **{error_classes.LABELS.get(category, category)}**: {error_classes.ACTIONS.get(category, '')}")
    text = "\n".join(lines) + "\n"
    dest = str(body.get("dest") or "")
    if not dest:
        return {"ok": True, "text": text, "groups": len(groups)}
    if not dest.lower().endswith((".md", ".txt")):
        return {"ok": False, "error": "The report must be a .md or .txt file."}
    try:
        Path(dest).write_text(text, encoding="utf-8")
    except OSError as error:
        return {"ok": False, "error": f"Could not write the report: {error.strerror or error}"}
    return {"ok": True, "path": dest, "groups": len(groups), "failed": sum(g["count"] for g in groups.values())}


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    jobs_db = Path(sys.argv[2]) if len(sys.argv) > 2 else Path()
    try:
        body = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    if command == "after":
        result = after(jobs_db, body)
    elif command == "broken":
        result = broken(jobs_db, body)
    else:
        result = {"ok": False, "error": "usage: job_report.py after|broken <jobs.sqlite>"}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
