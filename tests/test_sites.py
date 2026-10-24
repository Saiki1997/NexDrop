"""Approved sites, the generic media handler and NEXDROP's site policy.

Everything runs against the pages in tests/fixtures/site served from 127.0.0.1;
no live website is contacted. Run: python tests/test_sites.py
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "tests" / "fixtures" / "site"
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine" / "vendor"))

import site_policy  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2000 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1500
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 6000


def _make_hls(folder: Path) -> bool:
    """A real two-segment HLS stream, so the engine's HLS path is exercised. Needs ffmpeg."""
    if not shutil.which("ffmpeg"):
        return False
    folder.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
         str(folder / "index.m3u8")],
        capture_output=True, check=False,
    )
    return result.returncode == 0 and (folder / "index.m3u8").is_file()


class FixtureSite:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.hls = _make_hls(self.tmp / "stream")
        self.requests: list[str] = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                site.requests.append(path)
                body, ctype, status = b"", "text/html; charset=utf-8", 200
                if path == "/private.html":
                    status = 403
                elif path.startswith("/media/stream/") and site.hls:
                    file = site.tmp / "stream" / path.rsplit("/", 1)[1]
                    if file.is_file():
                        body = file.read_bytes()
                        ctype = "application/vnd.apple.mpegurl" if file.suffix == ".m3u8" else "video/mp2t"
                    else:
                        status = 404
                elif path in {"/media/missing.mp4"} or path.startswith("/static/"):
                    status = 404
                elif path.startswith("/media/"):
                    body, ctype = {".jpg": (JPEG, "image/jpeg"), ".png": (PNG, "image/png")}.get(Path(path).suffix, (MP4, "video/mp4"))
                elif (SITE / path.lstrip("/")).is_file():
                    body = (SITE / path.lstrip("/")).read_bytes()
                else:
                    status = 404
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)


def run_bridge(urls: list[str], approved: list[dict], appdata: Path, out: Path) -> list[dict]:
    payload = {
        "urls": urls, "downloadFolder": str(out), "orderMode": "parallel", "attempts": 1,
        "approvedSites": approved, "blockedHosts": site_policy.blocked_hosts(),
    }
    env = {**os.environ, "CDL_APPDATA_FOLDER": str(appdata), "PYTHONPATH": str(ROOT / "engine" / "vendor"),
           "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-u", str(ROOT / "engine" / "bridge.py")], input=json.dumps(payload) + "\n",
                            capture_output=True, text=True, env=env, timeout=240, check=False, encoding="utf-8")
    events = []
    for line in result.stdout.splitlines():
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    if not any(e.get("type") == "done" for e in events):
        raise AssertionError(f"bridge did not finish:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}")
    return events


class Policy(unittest.TestCase):
    def _user(self, tmp, entries):
        path = Path(tmp) / "approved.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def test_documented_assessment_for_monotirou(self):
        entry = site_policy.assessment("https://www.monotirou.com/video/k5hmA95b")
        self.assertEqual(entry["state"], "unsupported")
        for key in ("reason", "assessed", "reassess_when", "doc"):
            self.assertTrue(entry.get(key), key)
        self.assertTrue((ROOT / entry["doc"]).is_file())
        self.assertTrue(site_policy.blocked_reason("https://cdn.monotirou.com/a.mp4").startswith("Unsupported"))
        self.assertEqual(site_policy.blocked_reason("https://notmonotirou.com/"), "")

    def test_unlisted_sites_keep_normal_behaviour(self):
        self.assertIsNone(site_policy.assessment("https://www.erome.com/a/abc"))
        self.assertEqual(site_policy.blocked_reason("https://bunkr.cr/a/x"), "")

    def test_user_site_review_then_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._user(tmp, [{"url": "https://media.example/", "kind": "media"}])
            self.assertEqual(site_policy.assessment("https://media.example/v/1", path)["state"], "review")
            self.assertIn("Review required", site_policy.blocked_reason("https://media.example/v/1", path))
            self.assertIn("media.example", site_policy.blocked_hosts(path))
            self.assertEqual(site_policy.approved_sites(path), [])

            path = self._user(tmp, [{"url": "https://media.example/", "kind": "media", "authorizedAt": "2026-10-24T22:00:00"}])
            self.assertEqual(site_policy.blocked_reason("https://media.example/v/1", path), "")
            self.assertEqual([s["host"] for s in site_policy.approved_sites(path)], ["media.example"])
            self.assertNotIn("media.example", site_policy.blocked_hosts(path))

    def test_user_entry_cannot_override_an_unsupported_assessment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._user(tmp, [{"url": "https://monotirou.com/", "kind": "media", "authorizedAt": "x"},
                                    {"url": "https://odd.example/", "kind": "torrent", "authorizedAt": "x"}])
            self.assertEqual(site_policy.assessment("https://monotirou.com/", path)["source"], "nexdrop")
            self.assertEqual(site_policy.approved_sites(path), [])

    def test_audit_log(self):
        original = site_policy.AUDIT_FILE
        with tempfile.TemporaryDirectory() as tmp:
            site_policy.AUDIT_FILE = Path(tmp) / "logs" / "blocked.jsonl"
            try:
                site_policy.audit("https://monotirou.com/x", "reason", "pasted")
                line = json.loads(site_policy.AUDIT_FILE.read_text(encoding="utf-8").splitlines()[0])
            finally:
                site_policy.AUDIT_FILE = original
        self.assertEqual((line["url"], line["source"]), ("https://monotirou.com/x", "pasted"))


class PageTest(unittest.TestCase):
    """The no-download page test used to move a site from review to supported."""

    @classmethod
    def setUpClass(cls):
        cls.site = FixtureSite()
        import cairn_session

        cls.session = cairn_session
        cls.original = cairn_session.blocked_reason
        cairn_session.blocked_reason = lambda host: None  # the fixture site is on 127.0.0.1

    @classmethod
    def tearDownClass(cls):
        cls.session.blocked_reason = cls.original
        cls.site.close()

    def test_gallery_listing_and_restricted(self):
        import site_check

        before = len([p for p in self.site.requests if p.startswith("/media/")])
        result = site_check.check(f"{self.site.base}/gallery1.html")
        self.assertTrue(result["ok"])
        self.assertEqual(result["counts"], {"image": 4})
        self.assertTrue(result["nextPage"].endswith("/gallery2.html"))
        self.assertEqual(len([p for p in self.site.requests if p.startswith("/media/")]), before)  # nothing downloaded
        listing = site_check.check(f"{self.site.base}/list.html", items="a.item")
        self.assertEqual(listing["itemLinks"], 3)
        private = site_check.check(f"{self.site.base}/private.html")
        self.assertFalse(private["ok"])
        self.assertIn("403", private["message"])
        self.assertIn("unsupported", site_check.check("https://monotirou.com/")["state"])


class Extraction(unittest.TestCase):
    def _media(self, name):
        from bs4 import BeautifulSoup
        from cyberdrop_dl.crawlers._generic_media import find_media

        html = (SITE / name).read_text(encoding="utf-8")
        return find_media(BeautifulSoup(html, "html.parser"), f"http://site.test/{name}")

    def test_video_page_one_file_from_three_sources(self):
        media, notes = self._media("video.html")
        self.assertEqual([(m.url, m.kind, m.origin) for m in media], [("http://site.test/media/clip.mp4", "video", "jsonld")])
        self.assertEqual(notes, [])

    def test_gallery_links_and_lazy_images_not_thumbnails_or_logos(self):
        media, _ = self._media("gallery1.html")
        urls = [m.url for m in media]
        self.assertEqual(urls[:3], ["http://site.test/media/a1.jpg", "http://site.test/media/a2.jpg", "https://monotirou.com/media/z.jpg"])
        self.assertIn("http://site.test/media/a3.png", urls)
        self.assertFalse([u for u in urls if "thumb" in u or "logo" in u or "placeholder" in u])

    def test_hls_dash_and_blob(self):
        media, _ = self._media("hls.html")
        self.assertEqual([(m.kind, m.url) for m in media], [("hls", "http://site.test/media/stream/index.m3u8")])
        media, notes = self._media("drm.html")
        self.assertEqual(media, [])
        self.assertEqual(len(notes), 2)

    def test_page_without_media(self):
        media, _ = self._media("empty.html")
        self.assertEqual(media, [])


class EngineRun(unittest.TestCase):
    """The real engine, started through bridge.py like a NEXDROP run."""

    @classmethod
    def setUpClass(cls):
        cls.site = FixtureSite()
        cls.tmp = Path(tempfile.mkdtemp())
        cls.approved = [{"url": cls.site.base, "kind": "media", "items": "a.item", "next": ""}]
        b = cls.site.base
        cls.urls = [f"{b}/gallery1.html", f"{b}/list.html", f"{b}/drm.html", f"{b}/empty.html"]
        if cls.site.hls:
            cls.urls.append(f"{b}/hls.html")
        cls.first = run_bridge(cls.urls, cls.approved, cls.tmp / "appdata", cls.tmp / "out")
        cls.second = run_bridge(cls.urls, cls.approved, cls.tmp / "appdata", cls.tmp / "out")

    @classmethod
    def tearDownClass(cls):
        cls.site.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _of(self, events, kind):
        return [e for e in events if e.get("type") == kind]

    def test_files_saved_once_in_page_order(self):
        saved = sorted(Path(e["url"]).name for e in self._of(self.first, "saved"))
        expected = ["a1.jpg", "a2.jpg", "a3.png", "a4.jpg", "clip.mp4"] + (["index.m3u8"] if self.site.hls else [])
        self.assertEqual(saved, sorted(expected))
        files = sorted(p.name for p in (self.tmp / "out").rglob("*") if p.is_file())
        self.assertEqual(len(files), len(expected), files)
        self.assertTrue(all(name[:3].isdigit() for name in files), files)  # source-order numbering

    def test_blocked_host_is_never_requested(self):
        self.assertIn("https://monotirou.com/media/z.jpg", [e["url"] for e in self._of(self.first, "blocked")])

    def test_handler_is_reported(self):
        handlers = {e["url"]: e["handler"] for e in self._of(self.first, "handler")}
        self.assertTrue(all("approved site" in h for h in handlers.values()), handlers)

    def test_broken_restricted_and_empty_pages_are_reported(self):
        failed = {e["url"]: e["reason"] for e in self._of(self.first, "failed")}
        self.assertTrue(any(u.endswith("/media/missing.mp4") and "404" in r for u, r in failed.items()), failed)
        scrape = " ".join(e["message"] for e in self._of(self.first, "scrape_failed"))
        self.assertIn("403", scrape)
        self.assertIn("No downloadable media", scrape)
        logs = " ".join(e.get("message", "") for e in self._of(self.first, "log"))
        self.assertIn("DASH", logs)
        self.assertIn("blob:", logs)

    def test_only_one_site_and_no_wandering(self):
        paths = set(self.site.requests)
        self.assertNotIn("/private.html?x", paths)
        self.assertIn("/gallery2.html", paths)  # followed rel=next once
        self.assertEqual(self.site.requests.count("/gallery1.html"), 2)  # once per run, not again via gallery2's link

    def test_second_run_skips_everything_already_saved(self):
        self.assertEqual(self._of(self.second, "saved"), [])
        skipped = [e for e in self._of(self.second, "skipped") if e.get("reason") == "already downloaded"]
        self.assertGreaterEqual(len(skipped), 5)


class SessionRun(unittest.TestCase):
    """A whole NEXDROP run (cairn_session) with the policy applied to pasted links."""

    def setUp(self):
        import cairn_session

        self.session = cairn_session
        self.tmp = Path(tempfile.mkdtemp())
        self.site = FixtureSite()
        self.saved = (site_policy.APPROVED_FILE, site_policy.AUDIT_FILE, cairn_session.blocked_reason,
                      cairn_session.ensure_flaresolverr, cairn_session.APPDATA)
        site_policy.APPROVED_FILE = self.tmp / "approved-sites.json"
        site_policy.AUDIT_FILE = self.tmp / "blocked.jsonl"
        cairn_session.blocked_reason = lambda host: None
        cairn_session.ensure_flaresolverr = lambda config: None
        self.events = []
        self.original_emit = cairn_session.emit
        cairn_session.emit = lambda payload: self.events.append(payload)
        cairn_session.STOP.clear()
        cairn_session.FINISHED.clear()

    def tearDown(self):
        (site_policy.APPROVED_FILE, site_policy.AUDIT_FILE, self.session.blocked_reason,
         self.session.ensure_flaresolverr, self.session.APPDATA) = self.saved
        self.session.emit = self.original_emit
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(ROOT / "engine" / "appdata", ignore_errors=True)

    def _approve(self, authorized: bool):
        entry = {"url": self.site.base, "kind": "media", "testedAt": "t"}
        if authorized:
            entry["authorizedAt"] = "t"
        site_policy.APPROVED_FILE.write_text(json.dumps([entry]), encoding="utf-8")

    def test_review_and_unsupported_links_are_refused_and_audited(self):
        self._approve(False)
        self.session.run({"urls": [f"{self.site.base}/video.html", "https://monotirou.com/video/x"], "downloadsDir": str(self.tmp / "out")})
        finished = [e for e in self.events if e.get("type") == "finished"][0]
        self.assertEqual(finished["saved"], 0)
        logs = " ".join(e.get("message", "") for e in self.events if e.get("type") == "log")
        self.assertIn("Review required", logs)
        self.assertIn("Unsupported site", logs)
        self.assertEqual(len(site_policy.AUDIT_FILE.read_text(encoding="utf-8").splitlines()), 2)
        self.assertNotIn("/video.html", self.site.requests)  # nothing was fetched

    def test_supported_site_downloads_through_the_normal_pipeline(self):
        self._approve(True)
        self.session.run({"urls": [f"{self.site.base}/gallery1.html"], "downloadsDir": str(self.tmp / "out"),
                          "orderMode": "parallel", "autoRetry": False})
        finished = [e for e in self.events if e.get("type") == "finished"][0]
        self.assertEqual(finished["saved"], 4, finished)
        rows = {}
        for event in self.events:
            for row in event.get("items", []) if event.get("type") == "files" else []:
                rows[row["url"]] = row
        skipped = [r for r in rows.values() if r["state"] == "skipped"]
        self.assertTrue(any("monotirou.com" in r["url"] and r["reason"].startswith("blocked") for r in skipped), skipped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
