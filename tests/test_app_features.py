"""Offline checks for history search, the proxy setting and the upstream engine report."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine" / "vendor"))

import cairn_session as session  # noqa: E402
from job_store import JobStore  # noqa: E402


class HistorySearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.sqlite"
        store = JobStore(self.db)
        job = store.start_job(["https://bunkr.cr/a/trip-2024"], "D:/downloads")
        store.note(job, {"url": "https://cdn.test/1.jpg", "host": "bunkr.cr", "event": "success",
                         "name": "Holiday 001.jpg", "bytes_total": 2048, "destination": "D:/downloads/Holiday/Holiday 001.jpg"})
        store.note(job, {"url": "https://cdn.test/2.jpg", "host": "bunkr.cr", "event": "failure", "name": "2.jpg", "status": "404"})
        store.note(job, {"url": "https://cdn.test/100%_real.jpg", "host": "gofile.io", "event": "success", "name": "100%_real.jpg"})
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_words_status_and_host(self):
        store = JobStore(self.db)
        self.assertEqual([i["name"] for i in store.search("holiday")["items"]], ["Holiday 001.jpg"])
        self.assertEqual(store.search("", status="failed")["total"], 1)
        self.assertEqual(store.search("", status="all")["total"], 3)
        self.assertEqual(store.search("", host="www.gofile.io")["total"], 1)
        # % and _ are matched literally
        self.assertEqual([i["name"] for i in store.search("100%_")["items"]], ["100%_real.jpg"])
        self.assertEqual(store.search("0%")["total"], 1)
        # The link that was pasted finds every file of that run.
        self.assertEqual(store.search("trip-2024", status="all")["total"], 3)
        store.close()

    def test_destination_is_kept_and_located(self):
        store = JobStore(self.db)
        item = store.search("holiday")["items"][0]
        self.assertTrue(item["hasPath"])
        # A later note without a path keeps the saved one.
        job_id = store.conn.execute("SELECT job_id FROM items WHERE id = ?", (item["id"],)).fetchone()[0]
        store.note(job_id, {"url": "https://cdn.test/1.jpg", "event": "success"})
        self.assertEqual(store.locate(item["id"])["destination"], "D:/downloads/Holiday/Holiday 001.jpg")
        self.assertIsNone(store.locate(999))
        store.close()

    def test_command_line_reads_query_from_stdin(self):
        result = subprocess.run([sys.executable, str(ROOT / "desktop" / "job_store.py"), "search", str(self.db)],
                                input=json.dumps({"text": "holiday", "status": "all"}) + "\n",
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["total"], 1)


class ProxySetting(unittest.TestCase):
    def test_only_http_proxies_with_a_host(self):
        self.assertEqual(session.proxy_url(" http://127.0.0.1:8080/ "), "http://127.0.0.1:8080")
        self.assertEqual(session.proxy_url("https://user:pw@proxy.example.com:3128"), "https://user:pw@proxy.example.com:3128")
        for bad in ("", "socks5://127.0.0.1:1080", "127.0.0.1:8080", "http://", "http://a b:1", "http://x/path"):
            self.assertEqual(session.proxy_url(bad), "", bad)

    def test_opener_uses_the_proxy(self):
        import urllib.request

        session.use_proxy("http://127.0.0.1:8080")
        handlers = [h for h in session._OPENER.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(handlers and handlers[0].proxies["https"] == "http://127.0.0.1:8080")
        session.use_proxy("")
        handlers = [h for h in session._OPENER.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(all("https" not in h.proxies or h.proxies["https"] != "http://127.0.0.1:8080" for h in handlers))


class UpstreamReport(unittest.TestCase):
    def test_classify(self):
        spec = importlib.util.spec_from_file_location("upstream_report", ROOT / "tools" / "engine-upstream-report.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        base = {"a.py": b"1", "b.py": b"1", "c.py": b"1", "gone.py": b"1", "same.py": b"1"}
        target = {"a.py": b"2", "b.py": b"2", "c.py": b"1", "new.py": b"1", "same.py": b"1\r\n"[:1]}
        local = {"a.py": b"1", "b.py": b"mine", "c.py": b"mine", "gone.py": b"1", "same.py": b"1", "ours.py": b"x"}
        groups = module.classify(base, target, local)
        self.assertEqual(groups["upstream"], ["a.py"])
        self.assertEqual(groups["both"], ["b.py"])
        self.assertEqual(groups["nexdrop"], ["c.py"])
        self.assertEqual(groups["added"], ["new.py"])
        self.assertEqual(groups["removed"], ["gone.py"])
        self.assertEqual(groups["nexdrop_new"], ["ours.py"])


if __name__ == "__main__":
    unittest.main()
