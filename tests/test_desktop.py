"""Offline checks for the desktop adapter. Run: python -m pytest tests (or python tests/test_desktop.py)."""

from __future__ import annotations

import http.client
import http.server
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine" / "vendor"))

import auth_cookies  # noqa: E402
import cairn_session as session  # noqa: E402


class FileNames(unittest.TestCase):
    def test_encoded_traversal_stays_in_folder(self):
        for raw in ("/f/..%2F..%2F..%2FStartup%2Fevil.jpg", "/a/%2e%2e%5cwin.ini", "/../../x.png"):
            name = session.safe_filename(raw)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertFalse(name.startswith("."))

    def test_windows_reserved_and_invalid(self):
        self.assertEqual(session.safe_filename("/x/CON.txt"), "_CON.txt")
        self.assertEqual(session.safe_filename("/x/a%3Ab%3F.png"), "a_b_.png")
        self.assertEqual(session.safe_filename("/x/"), "download")
        self.assertLessEqual(len(session.safe_filename("/y/" + "a" * 400 + ".jpeg")), 160)

    def test_unique_path_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a.jpg").write_text("x")
            self.assertEqual(session.unique_path(folder, "a.jpg").name, "a (1).jpg")

    def test_content_disposition_name(self):
        name = session._pick_name("https://x.test/dl?id=1", "https://cdn.test/abc", 'attachment; filename="clip 01.mp4"')
        self.assertEqual(name, "clip 01.mp4")


class UrlSafety(unittest.TestCase):
    def test_private_and_credential_urls_blocked(self):
        for url in ("http://127.0.0.1/x", "http://localhost/x", "http://[::ffff:10.0.0.1]/y", "http://user:p@example.com/"):
            with self.assertRaises(ValueError):
                session.public_url(url)

    def test_redirect_to_private_address_is_refused(self):
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/secret")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        class Local(urllib.request.HTTPHandler):
            def http_open(self, req):
                req.host = f"127.0.0.1:{server.server_port}"
                return self.do_open(http.client.HTTPConnection, req)

        original_opener, original_check = session._OPENER, session.blocked_reason
        session._OPENER = urllib.request.build_opener(session._NoRedirect, Local)
        session.blocked_reason = lambda host: None if host == "public.test" else "Private and local addresses are blocked"
        try:
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError) as caught:
                session.http_save("http://public.test/a.png", Path(tmp))
            self.assertIn("Private", str(caught.exception))
        finally:
            session._OPENER, session.blocked_reason = original_opener, original_check
            server.shutdown()


class Cookies(unittest.TestCase):
    NETSCAPE = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.simpcity.cr\tTRUE\t/\tTRUE\t1900000000\togaddgmetaprof_user\t123\n"
        ".simpcity.cr\tTRUE\t/\tFALSE\t1900000000\tother\tx\n"
        ".elsewhere.com\tTRUE\t/\tFALSE\t1900000000\tz\t1\n"
    )

    def test_filter_keeps_httponly_login_cookie(self):
        out = auth_cookies.filter_netscape(self.NETSCAPE, ["simpcity.cr"])
        self.assertIn("ogaddgmetaprof_user", out)
        self.assertIn("other", out)
        self.assertNotIn("elsewhere", out)

    def test_storage_state_json_is_converted(self):
        state = json.dumps({"cookies": [{"name": "sid", "value": "v", "domain": ".x.com", "httpOnly": True, "expires": -1}]})
        out = auth_cookies.filter_netscape(state, ["x.com"])
        self.assertIn("#HttpOnly_.x.com", out)
        self.assertIn("\tsid\tv", out)

    def test_status_handles_odd_expiry(self):
        text = ".x.com\tTRUE\t/\tFALSE\t1.9e9\ta\tb\n"
        self.assertEqual(auth_cookies.cookie_status(text, ["x.com"])["state"], "present")

    def test_cli_probe_flag_exists(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, str(ROOT / "desktop" / "auth_cookies.py"), "--probe", "/does/not/exist"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "missing")


class FlareSolverr(unittest.TestCase):
    def test_blocked_binary_does_not_crash_the_run(self):
        original_alive, original_binary = session.flare_alive, session.flare_binary
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "flaresolverr.exe"
            blocked.write_text("not runnable")  # no execute permission
            session.flare_alive = lambda base: False
            session.flare_binary = lambda: blocked
            try:
                self.assertIsNone(session.ensure_flaresolverr({}))
            finally:
                session.flare_alive, session.flare_binary = original_alive, original_binary


class HintsFromRealLog(unittest.TestCase):
    """Messages copied from a real SimpCity run (allecakes.11587)."""

    def test_messages_from_the_field(self):
        hints = session.Hints("source", flare_ready=False)
        level, text = hints.scrape_failed(
            "https://simpcity.cr/threads/allecakes.1220429",
            "Scrape Failed: https://simpcity.cr/threads/allecakes.1220429 (Max Children Reached - Nested thread skipped because the depth limit was reached. Depth is 0.)",
        )
        self.assertEqual(level, "info")
        level, _ = hints.scrape_failed("https://www.tiktok.com/@a", "Scrape Failed: https://www.tiktok.com/@a (DDoS-Guard - CloudFlareTurnstile anti-bot protection detected)")
        self.assertEqual(level, "warning")
        _, text = hints.scrape_failed("https://cyberdrop.cr/f/x?video", "Scrape Failed: https://cyberdrop.cr/f/x?video ( (2 sub-exceptions))")
        self.assertIn("engine log", text)
        for i in range(10):
            hints.failed(f"https://simpcity.cr/attachments/x-jpeg.{i}", "Invalid Content Type")
        hints.failed("https://mega.nz/file/abc", "509 Bandwidth Limit Exceeded")
        self.assertIn("797 files", hints.forum("Forum result posts=104 files=797 nested=2 url=/threads/allecakes.11587"))
        summary = " ".join(hints.summary())
        for expected in ("10 file(s) from simpcity.cr", "MEGA", "Nested threads to follow", "tiktok.com", "Parallel"):
            self.assertIn(expected, summary)


class CookieFiltering(unittest.TestCase):
    def test_whole_browser_export_is_cut_to_supported_sites(self):
        sys.path.insert(0, str(ROOT / "engine"))
        import importlib

        bridge = importlib.import_module("bridge")
        keys = ["simpcity.cr", "bunkr", "mega.nz"]
        self.assertTrue(bridge._cookie_domain_ok(".simpcity.cr", keys, set()))
        self.assertTrue(bridge._cookie_domain_ok("#HttpOnly_.bunkr.cr", keys, set()))
        self.assertTrue(bridge._cookie_domain_ok(".example.org", keys, {"example.org"}))
        for private in (".google.com", ".visa.com", "accounts.google.com", ".tngdigital.com.my"):
            self.assertFalse(bridge._cookie_domain_ok(private, keys, set()))


class SecondRunHints(unittest.TestCase):
    """Messages from the 2026-10-24 12:27 run (4,457 files)."""

    def test_site_error_page_and_busy_server(self):
        hints = session.Hints("parallel", flare_ready=True)
        for i in range(10):
            hints.failed(f"https://simpcity.cr/attachments/x-jpeg.{i}",
                         "Invalid Content Type - Got a web page instead of the file: 'Oops! We ran into some problems. | SimpCity Forums'")
        hints.failed("https://pbc.scdn.st/storage/media/a.MP4", "503 Service Unavailable")
        text = " ".join(hints.summary())
        self.assertIn("site's own error page", text)
        self.assertNotIn("bot check isn't solved", text)
        self.assertIn("temporary server error", text)


class ServerSlots(unittest.TestCase):
    def test_three_per_server_then_back_off(self):
        import asyncio
        import types

        sys.path.insert(0, str(ROOT / "engine"))
        import importlib

        bridge = importlib.import_module("bridge")
        bridge.emit = lambda payload: None
        bridge._install_server_slots(3)
        from cyberdrop_dl.downloader.http import Downloader

        async def run(host, count):
            fake = types.SimpleNamespace(
                use_server_lock=True, _semaphore=asyncio.Semaphore(16),
                manager=types.SimpleNamespace(http_client=types.SimpleNamespace(limiter=types.SimpleNamespace(downloads=asyncio.Semaphore(16)))),
            )
            state = {"cur": 0, "peak": 0}

            async def one():
                async with Downloader.lock(fake, types.SimpleNamespace(host=host)):
                    state["cur"] += 1
                    state["peak"] = max(state["peak"], state["cur"])
                    await asyncio.sleep(0.02)
                    state["cur"] -= 1

            await asyncio.gather(*(one() for _ in range(count)))
            return state["peak"]

        self.assertEqual(asyncio.run(run("bunkr-cdn.test", 9)), 3)
        bridge.SERVER_SLOTS.back_off("bunkr-cdn.test")
        self.assertEqual(asyncio.run(run("bunkr-cdn.test", 5)), 1)
        self.assertEqual(asyncio.run(run("other-cdn.test", 9)), 3)


class RetryAndFolders(unittest.TestCase):
    def test_transient_errors(self):
        for reason in ("503 Service Unavailable", "429 Too Many Requests", "999 Timeout - Download timeout reached", "HTTP 502"):
            self.assertTrue(session.is_transient(reason), reason)
        for reason in ("404 Not Found", "403 Forbidden", "509 Bandwidth Limit Exceeded", "Invalid Content Type"):
            self.assertFalse(session.is_transient(reason), reason)

    def test_type_folder(self):
        self.assertEqual(session.type_folder("/a/b.JPG"), "Images")
        self.assertEqual(session.type_folder("/a/b.mp4"), "Videos")
        self.assertEqual(session.type_folder("/a/b.mp3"), "Audio")
        self.assertEqual(session.type_folder("/a/b.zip"), "Other")

    def test_failure_counted_once_and_forgotten_after_retry(self):
        hints = session.Hints("parallel", flare_ready=True)
        hints.failed("https://pbc.scdn.st/a.mp4", "503 Service Unavailable")
        hints.failed("https://pbc.scdn.st/a.mp4", "503 Service Unavailable")
        self.assertEqual(hints.busy, {"pbc.scdn.st": 1})
        hints.forget_saved(lambda url: True)
        self.assertEqual(hints.busy, {})


class FileTableStates(unittest.TestCase):
    def test_finished_row_never_returns_to_active(self):
        table = session.FileTable()
        table.update("u", state="active", read=10)
        table.update("u", state="done", read=100, total=100)
        table.update("u", state="active", read=5, speed=3)
        self.assertEqual(table.state("u"), "done")
        self.assertEqual(table.counts["done"], 1)

    def test_counts_follow_corrections(self):
        table = session.FileTable()
        table.update("u", state="done")
        table.update("u", state="failed")
        self.assertEqual(table.counts["done"], 0)
        self.assertEqual(table.counts["failed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
