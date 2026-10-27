"""3.1 completion actions: verify a job's files, write its manifest, and the broken-link report."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))

import job_report  # noqa: E402
from job_store import JobStore  # noqa: E402


def make_job(base: Path) -> tuple[Path, int]:
    folder = base / "dl"
    folder.mkdir()
    (folder / "good.jpg").write_bytes(b"\xff\xd8" + b"x" * 98)
    (folder / "short.mp4").write_bytes(b"y" * 10)
    (folder / "login.jpg").write_bytes(b"<!DOCTYPE html><html>sign in</html>")
    db = base / "jobs.sqlite"
    store = JobStore(db)
    job = store.start_job(["https://bunkr.cr/a/Album1?token=secret"], str(folder))
    now = time.time()
    rows = [
        ("https://cdn.test/good.jpg?sig=abc", "good.jpg", "completed", 100, {"creator": "alice", "album": "Beach"}),
        ("https://cdn.test/short.mp4", "short.mp4", "completed", 50, {}),
        ("https://cdn.test/login.jpg", "login.jpg", "completed", 0, {}),
        ("https://cdn.test/gone.jpg", "", "failed", 0, {}),
    ]
    for url, name, status, total, identity in rows:
        store.conn.execute(
            "INSERT INTO items (job_id, url, host, filename, destination, status, bytes_total, updated_at, identity, error, error_category) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job, url, "cdn.test", name, str(folder / name) if name else "", status, total, now, json.dumps(identity),
             "HTTP 404" if status == "failed" else "", "not_found" if status == "failed" else ""))
    store.finish_job(job, "partial")
    store.close()
    return db, job


class After(unittest.TestCase):
    def test_verify_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, job = make_job(Path(tmp))
            out = job_report.after(db, {"job": job, "verify": True, "manifest": True, "version": "3.1.0"})
            self.assertTrue(out["ok"], out)
            problems = {p["name"]: p["problem"] for p in out["verify"]["problems"]}
            self.assertEqual(out["verify"]["checked"], 3)
            self.assertIn("server sent 50", problems["short.mp4"])
            self.assertEqual(problems["login.jpg"], "a web page, not the file")
            self.assertNotIn("good.jpg", problems)
            manifest_path = Path(out["manifest"])
            self.assertTrue(manifest_path.name.endswith(".nexdrop.json"))
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(data["sources"], ["https://bunkr.cr/a/Album1"])  # no query string
            good = next(f for f in data["files"] if f["name"] == "good.jpg")
            self.assertEqual((good["creator"], good["album"], good["size"], good["path"]), ("alice", "Beach", 100, "good.jpg"))
            self.assertEqual(good["url"], "https://cdn.test/good.jpg")
            self.assertEqual(len(good["sha256"]), 64)
            self.assertEqual(data["counts"], {"completed": 3, "failed": 1, "skipped": 0})
            self.assertNotIn("secret", manifest_path.read_text(encoding="utf-8"))
            # a second run never overwrites the manifest
            before = manifest_path.read_bytes()
            job_report.after(db, {"job": job, "manifest": True})
            self.assertEqual(manifest_path.read_bytes(), before)

    def test_nothing_chosen_reads_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, job = make_job(Path(tmp))
            out = job_report.after(db, {"job": job})
            self.assertEqual(len(out["paths"]), 3)
            self.assertNotIn("manifest", out)
            self.assertNotIn("verify", out)
            self.assertFalse(job_report.after(db, {"job": 999})["ok"])


class Broken(unittest.TestCase):
    def test_report_groups_and_leaves_out_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, _ = make_job(Path(tmp))
            dest = Path(tmp) / "report.md"
            out = job_report.broken(db, {"days": 30, "dest": str(dest), "version": "3.1.0"})
            self.assertTrue(out["ok"], out)
            text = dest.read_text(encoding="utf-8")
            self.assertIn("| cdn.test | Deleted | 1 |", text)
            self.assertNotIn("gone.jpg", text)
            with_links = job_report.broken(db, {"days": 30, "links": True})["text"]
            self.assertIn("https://cdn.test/gone.jpg", with_links)
            self.assertFalse(job_report.broken(db, {"dest": str(Path(tmp) / "x.exe")})["ok"])


if __name__ == "__main__":
    unittest.main()
