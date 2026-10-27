"""3.1 statistics: totals, sites, types and activity from History and the per-run log, and a real run
that writes the run line."""

import csv
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stats  # noqa: E402
from job_store import JobStore  # noqa: E402
from local_server import LocalServer  # noqa: E402
from test_jobs_at_once import collect, start_session  # noqa: E402


class Summary(unittest.TestCase):
    def test_totals_sites_types_and_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = JobStore(base / "jobs.sqlite")
            job = store.start_job(["https://a.test/album"], tmp)
            now = time.time()
            rows = [("https://a.test/1.jpg", "1.jpg", "completed", 1000, now), ("https://a.test/2.mp4", "2.mp4", "completed", 5000, now - 86400),
                    ("https://b.test/3.jpg", "", "failed", 0, now), ("https://b.test/4.jpg", "4.jpg", "skipped", 0, now),
                    ("https://a.test/old.jpg", "old.jpg", "completed", 7, now - 400 * 86400)]
            for url, name, status, size, when in rows:
                store.conn.execute("INSERT INTO items (job_id, url, host, filename, status, bytes_total, updated_at) VALUES (?,?,?,?,?,?,?)",
                                   (job, url, url.split("/")[2], name, status, size, when))
            store.conn.commit()
            store.close()
            runs = base / "stats-runs.jsonl"
            stats.append_run(runs, {"t": now, "seconds": 10, "bytes": 6000, "peak": 900, "duplicates": 1})
            stats.append_run(runs, {"t": now, "seconds": 2, "bytes": 0, "peak": 100, "duplicates": 0})
            out = stats.summary(base / "jobs.sqlite", runs, days=30, now=now)
            self.assertEqual(out["totals"], {"files": 4, "bytes": 6000, "saved": 2, "failed": 1, "skipped": 1})
            self.assertEqual((out["averageSpeed"], out["peakSpeed"], out["duplicates"], out["seconds"]), (500, 900, 1, 12))
            self.assertEqual(out["sites"][0], {"site": "a.test", "saved": 2, "failed": 0, "skipped": 0, "bytes": 6000})
            self.assertEqual({t["type"]: t["bytes"] for t in out["types"]}, {"image": 1000, "video": 5000})
            self.assertEqual(len(out["daily"]), 30)
            self.assertEqual(out["daily"][-1]["saved"], 1)
            self.assertEqual(out["daily"][-2]["bytes"], 5000)
            self.assertEqual(len(out["monthly"]), 12)
            self.assertEqual(sum(w["saved"] for w in out["weekly"]), 2)
            self.assertEqual(stats.summary(base / "jobs.sqlite", runs, days=0, now=now)["totals"]["saved"], 3)  # all time
            dest = base / "s.csv"
            stats.write_csv(out, dest)
            with dest.open(encoding="utf-8-sig") as handle:
                table = list(csv.reader(handle))
            self.assertEqual(table[1], ["total", "all", "2", "1", "1", "6000"])
            self.assertIn(["site", "a.test", "2", "0", "0", "6000"], table)

    def test_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = stats.summary(Path(tmp) / "none.sqlite", Path(tmp) / "none.jsonl")
            self.assertEqual((out["totals"]["files"], out["averageSpeed"], out["peakSpeed"]), (0, 0, 0))


class RunLine(unittest.TestCase):
    def test_a_run_appends_one_line(self):
        with tempfile.TemporaryDirectory() as tmp, LocalServer() as server:
            base = Path(tmp)
            events = collect({0: start_session(base / "appdata", 0, [server.url("ok", f"s{time.time_ns()}.jpg", size=40_000)], base / "dl")})[0]
            self.assertEqual(events[-1]["saved"], 1)
            lines = (base / "appdata" / "stats-runs.jsonl").read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[-1])
            self.assertEqual((len(lines), row["saved"], row["bytes"]), (1, 1, 40_000))
            self.assertGreater(row["seconds"], 0)


if __name__ == "__main__":
    unittest.main()
