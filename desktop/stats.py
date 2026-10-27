"""Download statistics (3.1, Library > Statistics).

    stats.py summary <jobs.sqlite> <stats-runs.jsonl>    stdin: {"days": 0 for all time, or a number}
    stats.py csv     <jobs.sqlite> <stats-runs.jsonl>    stdin: {"days": ..., "dest": file.csv}

Everything is read from what NexDrop already keeps: History (jobs.sqlite) for files, sites and
types, and one line per run in stats-runs.jsonl (time spent, bytes, peak speed, same-content
files), which the session appends when a run ends. Nothing is collected while downloading, so
there is no cost during a run. Nothing leaves the PC.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_store import file_type  # noqa: E402

MAX_RUN_LINES = 20_000


def _runs(path: Path, since: float) -> list[dict]:
    out = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and float(row.get("t") or 0) >= since:
                    out.append(row)
    except OSError:
        return []
    return out[-MAX_RUN_LINES:]


def append_run(path: Path, row: dict) -> None:
    """One line per finished run; the file is trimmed to the newest MAX_RUN_LINES lines."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        if path.stat().st_size > 4_000_000:
            lines = path.read_text(encoding="utf-8").splitlines()[-MAX_RUN_LINES:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def summary(jobs_db: Path, runs_file: Path, days: float = 0, now: float | None = None) -> dict:
    now = now or time.time()
    since = now - days * 86400 if days else 0.0
    totals = {"files": 0, "bytes": 0, "saved": 0, "failed": 0, "skipped": 0}
    sites: dict[str, dict] = {}
    types: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    if jobs_db.is_file():
        conn = sqlite3.connect(f"file:{jobs_db}?mode=ro", uri=True, timeout=10)
        rows = conn.execute(
            "SELECT host, status, filename, url, CASE WHEN bytes_total > 0 THEN bytes_total ELSE bytes_read END, updated_at "
            "FROM items WHERE updated_at >= ? AND status IN ('completed', 'failed', 'skipped')", (since,))
        for host, status, filename, url, size, updated in rows:
            key = {"completed": "saved", "failed": "failed", "skipped": "skipped"}[status]
            totals[key] += 1
            totals["files"] += 1
            site = sites.setdefault(host or "unknown", {"site": host or "unknown", "saved": 0, "failed": 0, "skipped": 0, "bytes": 0})
            site[key] += 1
            if status != "completed":
                continue
            size = int(size or 0)
            totals["bytes"] += size
            site["bytes"] += size
            kind = file_type(filename, url)
            entry = types.setdefault(kind, {"type": kind, "saved": 0, "bytes": 0})
            entry["saved"] += 1
            entry["bytes"] += size
            day = datetime.fromtimestamp(float(updated or 0)).date().isoformat()
            bucket = by_day.setdefault(day, {"saved": 0, "bytes": 0})
            bucket["saved"] += 1
            bucket["bytes"] += size
        conn.close()
    runs = _runs(runs_file, since)
    seconds = sum(float(r.get("seconds") or 0) for r in runs)
    run_bytes = sum(int(r.get("bytes") or 0) for r in runs)
    today = datetime.fromtimestamp(now).date()

    def series(keys: list[str], pick) -> list[dict]:
        merged: dict[str, dict] = {k: {"key": k, "saved": 0, "bytes": 0} for k in keys}
        for day, bucket in by_day.items():
            key = pick(date.fromisoformat(day))
            if key in merged:
                merged[key]["saved"] += bucket["saved"]
                merged[key]["bytes"] += bucket["bytes"]
        return list(merged.values())

    daily = [(today - timedelta(days=n)).isoformat() for n in range(29, -1, -1)]
    weekly = list(dict.fromkeys(_week(today - timedelta(weeks=n)) for n in range(11, -1, -1)))
    monthly = []
    year, month = today.year, today.month
    for _ in range(12):
        monthly.insert(0, f"{year}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return {
        "ok": True,
        "days": days,
        "totals": totals,
        "duplicates": sum(int(r.get("duplicates") or 0) for r in runs),
        "runs": len(runs),
        "seconds": round(seconds, 1),
        # speed while runs were going, from the runs' own bytes and time
        "averageSpeed": int(run_bytes / seconds) if seconds > 0 else 0,
        "peakSpeed": max((int(r.get("peak") or 0) for r in runs), default=0),
        "sites": sorted(sites.values(), key=lambda s: (-s["saved"], -s["bytes"]))[:25],
        "types": sorted(types.values(), key=lambda t: -t["bytes"]),
        "daily": series(daily, lambda d: d.isoformat()),
        "weekly": series(weekly, _week),
        "monthly": series(monthly, lambda d: f"{d.year}-{d.month:02d}"),
    }


def _cell(value):
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def write_csv(data: dict, dest: Path) -> None:
    with dest.open("w", newline="", encoding="utf-8-sig") as handle:
        out = csv.writer(handle)
        t = data["totals"]
        out.writerow(["section", "name", "files saved", "failed", "skipped", "bytes"])
        out.writerow(["total", "all", t["saved"], t["failed"], t["skipped"], t["bytes"]])
        out.writerow(["total", "same-content files", data["duplicates"], "", "", ""])
        out.writerow(["total", "download time (s)", data["seconds"], "", "", ""])
        out.writerow(["total", "average speed (B/s)", data["averageSpeed"], "", "", ""])
        out.writerow(["total", "peak speed (B/s)", data["peakSpeed"], "", "", ""])
        for site in data["sites"]:
            out.writerow(["site", _cell(site["site"]), site["saved"], site["failed"], site["skipped"], site["bytes"]])
        for kind in data["types"]:
            out.writerow(["type", kind["type"], kind["saved"], "", "", kind["bytes"]])
        for section in ("daily", "weekly", "monthly"):
            for row in data[section]:
                out.writerow([section, row["key"], row["saved"], "", "", row["bytes"]])


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[1] not in ("summary", "csv"):
        print(json.dumps({"ok": False, "error": "usage: stats.py summary|csv <jobs.sqlite> <stats-runs.jsonl>"}))
        return 0
    try:
        body = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    try:
        days = max(0.0, min(36500.0, float(body.get("days") or 0)))
    except (TypeError, ValueError):
        days = 0.0
    data = summary(Path(sys.argv[2]), Path(sys.argv[3]), days)
    if sys.argv[1] == "summary":
        print(json.dumps(data, ensure_ascii=False))
        return 0
    dest = str(body.get("dest") or "")
    if not dest.lower().endswith(".csv"):
        print(json.dumps({"ok": False, "error": "The export must be a .csv file."}))
        return 0
    try:
        write_csv(data, Path(dest))
        print(json.dumps({"ok": True, "path": dest}, ensure_ascii=False))
    except OSError as error:
        print(json.dumps({"ok": False, "error": f"Could not write the file: {error.strerror or error}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
