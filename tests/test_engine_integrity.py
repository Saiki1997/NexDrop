"""Download integrity in the bundled engine, against a local test server (no internet needed).

Covers the 3.0 P0 fixes: resume only appends bytes of the same file, truncated or web-page
downloads never count as saved, busy answers are retried inside the run, permanent ones are not,
and all hashes come from one read of the file.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_server import LocalServer, content  # noqa: E402

from cyberdrop_dl.clients import resume_validators  # noqa: E402


def run_bridge(urls: list[str], folder: Path, **extra) -> list[dict]:
    payload = {"urls": urls, "downloadFolder": str(folder), "orderMode": "parallel", "maxDownloads": 8,
               "perHost": 8, "attempts": 3, "ignoreHistory": True, "smartNames": False, **extra}
    env = {**os.environ, "PYTHONPATH": str(ROOT / "engine" / "vendor"), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, str(ROOT / "engine" / "bridge.py")], input=json.dumps(payload) + "\n",
                          capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), env=env, timeout=300)
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def by_url(events: list[dict], kind: str) -> dict[str, dict]:
    return {e["url"]: e for e in events if e.get("type") == kind and e.get("url")}


class ResumeValidators(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.part = Path(self.dir.name) / "a.mp4.part"
        self.part.write_bytes(b"x" * 100)

    def tearDown(self):
        self.dir.cleanup()

    def test_range_must_start_at_the_resume_point(self):
        self.assertEqual("", resume_validators.mismatch(self.part, {"Content-Range": "bytes 100-199/200"}, 100))
        self.assertIn("from 0", resume_validators.mismatch(self.part, {"Content-Range": "bytes 0-199/200"}, 100))
        self.assertIn("Content-Range", resume_validators.mismatch(self.part, {}, 100))

    def test_changed_size_or_etag_is_refused(self):
        resume_validators.save(self.part, {"ETag": '"v1"'}, 200, "https://x.test/a.mp4?token=secret")
        meta = json.loads(resume_validators.sidecar(self.part).read_text())
        self.assertEqual(meta["url"], "https://x.test/a.mp4")  # no token beside the download
        self.assertEqual(resume_validators.if_range(self.part), '"v1"')
        self.assertIn("now 300", resume_validators.mismatch(self.part, {"Content-Range": "bytes 100-299/300"}, 100))
        self.assertIn("ETag", resume_validators.mismatch(self.part, {"Content-Range": "bytes 100-199/200", "ETag": '"v2"'}, 100))
        self.assertEqual("", resume_validators.mismatch(self.part, {"Content-Range": "bytes 100-199/200", "ETag": '"v1"'}, 100))
        resume_validators.forget(self.part)
        self.assertFalse(resume_validators.sidecar(self.part).exists())

    def test_weak_etag_falls_back_to_last_modified(self):
        resume_validators.save(self.part, {"ETag": 'W/"v1"', "Last-Modified": "Mon, 26 Oct 2026 10:00:00 GMT"}, 200)
        self.assertEqual(resume_validators.if_range(self.part), "Mon, 26 Oct 2026 10:00:00 GMT")


class SinglePassHashing(unittest.TestCase):
    def test_all_hashes_from_one_read_match_hashlib(self):
        from cyberdrop_dl.hasher import HashAlgo, _compute_hashes
        import xxhash

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "f.bin"
            data = os.urandom(3 * 1024 * 1024 + 7)
            path.write_bytes(data)
            found = _compute_hashes(path, [HashAlgo.MD5, HashAlgo.SHA256, HashAlgo.XXH128])
        self.assertEqual(found[HashAlgo.MD5], hashlib.md5(data).hexdigest())
        self.assertEqual(found[HashAlgo.SHA256], hashlib.sha256(data).hexdigest())
        self.assertEqual(found[HashAlgo.XXH128], xxhash.xxh128(data).hexdigest())


class RetryDelay(unittest.TestCase):
    def test_retry_after_wins_and_backoff_grows(self):
        from cyberdrop_dl.downloader.http import RETRY_AFTER_CAP, RETRY_MAX_DELAY, retry_delay
        from cyberdrop_dl.exceptions import DownloadError

        error = DownloadError(503, retry=True)
        error.retry_after = 7
        self.assertEqual(retry_delay(error, 1), 7)
        error.retry_after = 10_000
        self.assertEqual(retry_delay(error, 1), RETRY_AFTER_CAP)
        plain = DownloadError(500, retry=True)
        self.assertLess(retry_delay(plain, 1), retry_delay(plain, 4))
        self.assertLessEqual(retry_delay(plain, 30), RETRY_MAX_DELAY * 1.25)

    def test_busy_answers_are_retryable_and_missing_files_are_not(self):
        from cyberdrop_dl.clients.http import RETRYABLE_STATUS, retry_after_seconds

        self.assertIn(503, RETRYABLE_STATUS)
        self.assertIn(429, RETRYABLE_STATUS)
        self.assertNotIn(404, RETRYABLE_STATUS)
        self.assertNotIn(403, RETRYABLE_STATUS)
        self.assertEqual(retry_after_seconds("12"), 12.0)
        self.assertIsNone(retry_after_seconds("soon"))
        self.assertEqual(retry_after_seconds("Mon, 26 Oct 2020 10:00:00 GMT"), 0.0)


class EngineDownloads(unittest.TestCase):
    """One engine run over every server behaviour; each test checks its own files."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.tmp.name)
        cls.server = LocalServer().__enter__()
        s = cls.server
        stamp = str(time.time_ns())
        cls.urls = {
            "ok": s.url("ok", f"ok{stamp}.mp4", size=300_000),
            "copy": s.url("ok", f"cp{stamp}.mp4", size=300_000, **{"as": f"ok{stamp}.mp4"}),
            "trunc": s.url("trunc", f"tr{stamp}.mp4", size=300_000),
            "drop": s.url("drop-once", f"dr{stamp}.mp4", size=300_000),
            "flaky": s.url("flaky", f"fl{stamp}.mp4", size=50_000, fail=1),
            "gone": s.url("gone", f"gn{stamp}.mp4"),
            "html": s.url("html", f"ht{stamp}.jpg"),
            "badrange": s.url("badrange", f"br{stamp}.mp4", size=300_000),
            "changed": s.url("changed", f"ch{stamp}.mp4", size=300_000),
            "ignore": s.url("changed-ignore", f"ci{stamp}.mp4", size=300_000),
        }
        cls.started = time.monotonic()
        cls.events = run_bridge(list(cls.urls.values()), cls.folder)
        cls.seconds = time.monotonic() - cls.started
        cls.saved = by_url(cls.events, "saved")
        cls.failed = by_url(cls.events, "failed")

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()
        cls.tmp.cleanup()

    def saved_bytes(self, key: str) -> bytes:
        event = self.saved.get(self.urls[key])
        self.assertIsNotNone(event, f"{key} was not saved; failed: {self.failed.get(self.urls[key])}")
        return Path(event["path"]).read_bytes()

    def name(self, key: str) -> str:
        return self.urls[key].split("/")[4].split("?")[0]

    def test_plain_file_is_saved_whole(self):
        self.assertEqual(self.saved_bytes("ok"), content(self.name("ok"), 300_000))

    def test_same_content_under_another_name_is_reported_not_deleted(self):
        copy = self.saved_bytes("copy")
        self.assertEqual(copy, self.saved_bytes("ok"))
        dupes = [e for e in self.events if e.get("type") == "duplicate"]
        self.assertEqual(len(dupes), 1, dupes)  # whichever of the two finished second
        self.assertIn(dupes[0]["url"], (self.urls["ok"], self.urls["copy"]))
        self.assertTrue(Path(dupes[0]["path"]).exists() and Path(dupes[0]["of"]).exists())

    def test_truncated_transfer_is_never_saved(self):
        self.assertNotIn(self.urls["trunc"], self.saved)
        self.assertIn(self.urls["trunc"], self.failed)
        self.assertFalse(list(self.folder.rglob(self.name("trunc"))), "a truncated file was left as complete")

    def test_dropped_connection_resumes_with_a_range(self):
        self.assertEqual(self.saved_bytes("drop"), content(self.name("drop"), 300_000))
        last = self.server.headers[f"drop-once/{self.name('drop')}"]
        self.assertTrue(last.get("Range", "").startswith("bytes="), last)
        self.assertEqual(last.get("If-Range"), '"d1"')

    def test_busy_server_is_retried_inside_the_run(self):
        self.assertEqual(self.saved_bytes("flaky"), content(self.name("flaky"), 50_000))
        self.assertEqual(self.server.hits[f"flaky/{self.name('flaky')}"], 2)

    def test_missing_file_is_not_retried(self):
        self.assertIn(self.urls["gone"], self.failed)
        self.assertEqual(self.server.hits[f"gone/{self.name('gone')}"], 1)

    def test_web_page_instead_of_image_is_not_saved(self):
        self.assertNotIn(self.urls["html"], self.saved)
        self.assertIn(self.urls["html"], self.failed)

    def test_wrong_range_start_restarts_instead_of_joining(self):
        self.assertEqual(self.saved_bytes("badrange"), content(self.name("badrange"), 300_000))

    def test_changed_file_with_if_range_is_downloaded_again_whole(self):
        self.assertEqual(self.saved_bytes("changed"), content(self.name("changed"), 300_000, 2))

    def test_changed_file_ignoring_if_range_is_not_joined(self):
        data = self.saved_bytes("ignore")
        self.assertEqual(data, content(self.name("ignore"), 300_000, 2))
        self.assertNotIn(content(self.name("ignore"), 300_000, 1)[:150_000], data)

    def test_no_sidecars_left_beside_finished_files(self):
        trunc = self.name("trunc").removesuffix(".mp4")  # that one stays resumable
        leftovers = [p.name for p in self.folder.rglob("*") if p.name.endswith(".nxmeta") and not p.name.startswith(trunc)]
        self.assertEqual(leftovers, [])

    def test_engine_start_has_no_fixed_pause(self):
        ready = next(e for e in self.events if e.get("type") == "ready")
        self.assertTrue(ready)
        self.assertLess(self.seconds, 90)


if __name__ == "__main__":
    unittest.main()
