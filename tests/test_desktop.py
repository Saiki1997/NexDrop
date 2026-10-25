"""Offline checks for the desktop adapter. Run: python -m pytest tests (or python tests/test_desktop.py)."""

from __future__ import annotations

import http.client
import http.server
import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
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


class HintsForOldLogFailures(unittest.TestCase):
    def test_bunkr_storage_link_refused(self):
        hints = session.Hints("parallel", flare_ready=True)
        hints.failed("https://pbc.scdn.st/storage/media/65cba869.MP4?n=a.MP4", "403 Forbidden")
        hints.failed("https://example.org/storage/x.mp4", "403 Forbidden")
        summary = " ".join(hints.summary())
        self.assertIn("1 Bunkr storage link(s) on pbc.scdn.st", summary)
        self.assertIn("bunkr.cr/f/", summary)

    def test_retry_keeps_the_sites_error_page_reason(self):
        hints = session.Hints("parallel", flare_ready=True)
        url = "https://simpcity.cr/attachments/x-jpeg.33171"
        hints.failed(url, "Invalid Content Type - Got a web page instead of the file: 'Oops! We ran into some problems. | SimpCity Forums'")
        hints.failed(url, "Invalid Content Type - Received 'text/html; charset=utf-8', was expecting binary payload")
        summary = " ".join(hints.summary())
        self.assertIn("1 file(s) from simpcity.cr returned the site's own error page", summary)
        self.assertNotIn("came back as a web page", summary)


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


class OneTimeTokens(unittest.TestCase):
    def test_file_events_drop_tokens_so_rows_match_numbering(self):
        sys.path.insert(0, str(ROOT / "engine"))
        import importlib
        import io

        bridge = importlib.import_module("bridge")
        from cyberdrop_dl.source_order import public_url

        url = "https://cdn4.telesco.pe/file/abc.mp4?token=SECRET&w=1"
        self.assertEqual(bridge.shown_url(url), public_url(url))
        out = io.StringIO()
        original = sys.stdout
        sys.stdout = out
        try:
            bridge.emit({"type": "progress", "files": [{"url": url, "read": 1}]})
            bridge.emit({"type": "skipped", "url": "https://t.me/a/5#2", "reason": "x"})
        finally:
            sys.stdout = original
        self.assertNotIn("SECRET", out.getvalue())
        self.assertIn("https://t.me/a/5#2", out.getvalue())


class TelegramPublic(unittest.TestCase):
    """The t.me/s crawler, fed a saved page instead of the network."""

    def _crawler(self, pages, done=()):
        from bs4 import BeautifulSoup
        from cyberdrop_dl.crawlers.telegram import TelegramCrawler

        crawler = object.__new__(TelegramCrawler)
        calls = {"files": [], "pages": [], "warnings": []}

        async def web_pager(url, selector):
            calls["pages"].append(str(url))
            for html in pages:
                soup = BeautifulSoup(html, "html.parser")
                yield soup

        async def request_soup(url):
            calls["pages"].append(str(url))
            return BeautifulSoup(pages[0], "html.parser")

        async def handle_file(url, item, name, ext, **kw):
            calls["files"].append((str(url), name, str(kw["referer"]), str(item.url)))

        async def complete(referer):
            return str(referer) in done

        crawler.web_pager = web_pager
        crawler.request_soup = request_soup
        crawler.handle_file = handle_file
        crawler.check_complete_from_referer = complete
        crawler.create_title = lambda title, album_id=None: f"{title} ({album_id})"
        return crawler, calls

    def _item(self, url):
        from cyberdrop_dl.url_objects import AbsoluteHttpURL, ScrapeItem

        return ScrapeItem(url=AbsoluteHttpURL(url))

    def test_channel_files_in_post_order_with_sign_in_hints(self):
        import asyncio
        import logging

        html = (ROOT / "tests" / "fixtures" / "telegram_channel.html").read_text(encoding="utf-8")
        crawler, calls = self._crawler([html])
        item = self._item("https://t.me/testchan")
        with self.assertLogs("cyberdrop_dl", level=logging.WARNING) as logs:
            asyncio.run(type(crawler).channel.__wrapped__(crawler, item, "testchan"))
        names = [name for _, name, _, _ in calls["files"]]
        self.assertEqual(names, ["testchan_10.jpg", "testchan_11.mp4", "testchan_14_1.jpg", "testchan_14_2.ogg", "testchan_14_3.mp4"])
        self.assertEqual(calls["files"][1][2], "https://t.me/testchan/11")
        self.assertEqual(item.folders, ["Test Channel (testchan)"])
        needs = [line for line in logs.output if "Telegram needs sign-in" in line]
        self.assertEqual(len(needs), 2)
        self.assertIn("pack.zip", " ".join(needs))

    def test_history_and_single_file(self):
        import asyncio

        html = (ROOT / "tests" / "fixtures" / "telegram_channel.html").read_text(encoding="utf-8")
        crawler, calls = self._crawler([html], done={"https://t.me/testchan/10"})
        asyncio.run(type(crawler).channel.__wrapped__(crawler, self._item("https://t.me/s/testchan"), "testchan"))
        self.assertNotIn("testchan_10.jpg", [name for _, name, _, _ in calls["files"]])

        crawler, calls = self._crawler([html])
        asyncio.run(type(crawler).post.__wrapped__(crawler, self._item("https://t.me/testchan/11?single"), "testchan", 11))
        self.assertEqual([name for _, name, _, _ in calls["files"]], ["testchan_11.mp4"])
        self.assertIn("embed=1", calls["pages"][0])

    def test_links_and_host_matching(self):
        from cyberdrop_dl.crawlers.telegram import TelegramCrawler, classify
        from cyberdrop_dl.url_objects import AbsoluteHttpURL

        cases = {
            "https://t.me/durov": "channel", "https://t.me/s/durov": "channel", "https://t.me/durov/42": "post",
            "https://t.me/c/1234/5": "private", "https://t.me/+AbCd": "private", "https://t.me/joinchat/x": "private",
            "https://t.me/share/url?url=x": "other", "https://t.me/addstickers/x": "other",
        }
        for url, kind in cases.items():
            self.assertEqual(classify(AbsoluteHttpURL(url))[0], kind, url)
        self.assertTrue(TelegramCrawler.check_host_match("telegram.me"))
        self.assertFalse(TelegramCrawler.check_host_match("art.media.com"))


class RunLogSecrets(unittest.TestCase):
    def test_telegram_session_is_not_written(self):
        old = session.APPDATA
        with tempfile.TemporaryDirectory() as tmp:
            session.APPDATA = Path(tmp)
            try:
                config = {"downloadsDir": tmp, "telegram": {"apiId": "123", "apiHash": "hash-secret",
                                                            "session": "session-secret"}}
                session.open_run_log(config, ["https://t.me/example/1"])
                session.RUN_LOG.close()
                text = session.RUN_LOG_PATH.read_text(encoding="utf-8")
            finally:
                session.RUN_LOG = None
                session.APPDATA = old
        self.assertNotIn("hash-secret", text)
        self.assertNotIn("session-secret", text)
        self.assertIn("site sign-ins: telegram", text)


class ResumedDownloadBytes(unittest.TestCase):
    def test_bytes_already_on_disk_are_progress_not_new_bytes(self):
        sys.path.insert(0, str(ROOT / "engine"))
        import importlib

        bridge = importlib.import_module("bridge")
        events = []
        old_emit = bridge.emit
        bridge.emit = events.append
        clock = iter([100.0, 101.0, 101.0])
        try:
            with unittest.mock.patch.object(bridge.time, "monotonic", lambda: next(clock)):
                tracker = bridge._ProgressTracker()
                tracker.start(1, "https://cdn.example.test/v.mp4", "v.mp4", "example", 200)
                tracker.resume(1, 150)
                tracker.advance(1, 10)
        finally:
            bridge.emit = old_emit
        progress = [e for e in events if e["type"] == "progress"][-1]
        self.assertEqual(progress["bytes"], 10)
        self.assertEqual(progress["files"][0]["read"], 160)
        self.assertEqual(progress["files"][0]["speed"], 10)  # 10 new bytes in 1 s


class TelegramAccount(unittest.TestCase):
    """Account downloads with a stand-in for Telethon's client."""

    @staticmethod
    def _message(mid, ext, size, *, grouped=None, photo=False, video=False, name=None, sticker=False):
        import types

        return types.SimpleNamespace(
            id=mid, grouped_id=grouped, photo=object() if photo else None,
            document=None if photo else object(), video=object() if video else None, voice=None, video_note=None,
            gif=None, sticker=object() if sticker else None, web_preview=None,
            file=types.SimpleNamespace(ext=ext, size=size, name=name),
        )

    def _client(self, messages, fail_ids=()):
        import types

        chat = types.SimpleNamespace(id=777, username="chan", title="My Channel")
        calls = {"downloads": []}

        class Client:
            async def connect(self):
                pass

            async def disconnect(self):
                pass

            async def is_user_authorized(self):
                return True

            async def get_entity(self, peer):
                return chat

            async def get_messages(self, entity, ids):
                by_id = {m.id: m for m in messages}
                if isinstance(ids, list):
                    return [by_id.get(i) for i in ids]
                return by_id.get(ids)

            async def iter_messages(self, entity, reverse=False):
                for m in sorted(messages, key=lambda m: m.id, reverse=not reverse):
                    yield m

            async def download_media(self, message, file, progress_callback=None):
                calls["downloads"].append(message.id)
                if message.id in fail_ids:
                    raise ValueError("boom")
                data = b"x" * message.file.size
                with open(file, "wb") as handle:
                    handle.write(data)
                if progress_callback:
                    progress_callback(len(data), len(data))
                return file

        return Client(), calls

    def _hooks(self, history=()):
        import types

        rows, events = {}, {"saved": [], "failed": [], "skipped": [], "log": []}

        def update(url, **fields):
            rows.setdefault(url, {}).update(fields)

        return types.SimpleNamespace(
            stop=threading.Event(), update=update, add_bytes=lambda n: None,
            number=lambda url, name: (0, name),
            skip=lambda url: "already downloaded" if url in history else "",
            saved=lambda url, path: events["saved"].append((url, path.name)),
            failed=lambda url, reason: events["failed"].append((url, reason)),
            skipped=lambda url, reason: events["skipped"].append((url, reason)),
            log=lambda level, text: events["log"].append(text),
        ), rows, events

    def test_links(self):
        import telegram_account as tg

        self.assertEqual(tg.parse_link("https://t.me/c/1234/56").kind, "private_post")
        whole = tg.parse_link("https://t.me/c/1988700745")
        self.assertEqual((whole.kind, whole.channel_id), ("private_channel", 1988700745))
        self.assertEqual(tg.parse_link("https://t.me/c/1234/9/56").message_id, 56)
        self.assertEqual(tg.parse_link("https://t.me/+AbC_1").invite, "AbC_1")
        self.assertEqual(tg.parse_link("https://t.me/joinchat/xyz").kind, "invite")
        self.assertEqual(tg.parse_link("https://t.me/s/chan").kind, "channel")
        self.assertTrue(tg.parse_link("https://t.me/chan/5?single").single)
        self.assertIsNone(tg.parse_link("https://t.me/share/url?x=1"))
        self.assertIsNone(tg.parse_link("https://example.com/chan/5"))
        self.assertTrue(tg.is_telegram("https://telegram.me/x"))

    def test_channel_oldest_first_with_filters_history_and_failures(self):
        import telegram_account as tg

        messages = [
            self._message(1, ".jpg", 50, photo=True),
            self._message(2, ".mp4", 400, video=True),
            self._message(3, ".zip", 900, name="pack.zip"),
            self._message(4, ".webp", 10, sticker=True),
            self._message(5, ".mp4", 300, video=True),
            self._message(6, ".jpg", 5000, photo=True),
        ]
        client, calls = self._client(messages, fail_ids={5})
        hooks, rows, events = self._hooks(history={"https://t.me/chan/2"})
        with tempfile.TemporaryDirectory() as tmp:
            tg.TelegramDownloads({"session": "", "apiId": 1, "apiHash": "x"}, Path(tmp), hooks, parallel=2,
                                 min_image_kb=1, client_factory=lambda account: client).run(["https://t.me/chan"])
            saved = sorted(name for _, name in events["saved"])
            self.assertEqual(saved, ["chan_3_pack.zip", "chan_6.jpg"])
            self.assertTrue((Path(tmp) / "My Channel (Telegram)" / "chan_6.jpg").is_file())
            self.assertFalse(list(Path(tmp).rglob("*.part")))
        self.assertIn(("https://t.me/chan/1", "smaller than the minimum size"), events["skipped"])
        self.assertIn(("https://t.me/chan/2", "already downloaded"), events["skipped"])
        self.assertEqual(events["failed"][0][0], "https://t.me/chan/5")
        self.assertNotIn(4, calls["downloads"])  # stickers are not files
        self.assertEqual(rows["https://t.me/chan/6"]["state"], "done")

    def test_whole_private_chat(self):
        import types

        import telegram_account as tg

        messages = [self._message(i, ".jpg", 20, photo=True) for i in (3, 1, 2)]
        client, _ = self._client(messages)
        private = types.SimpleNamespace(id=1988700745, username=None, title="Sharing is Caring")

        async def get_entity(peer):
            return private

        client.get_entity = get_entity
        hooks, rows, events = self._hooks()
        with tempfile.TemporaryDirectory() as tmp:
            tg.TelegramDownloads({"session": "", "apiId": 1, "apiHash": "x"}, Path(tmp), hooks,
                                 client_factory=lambda account: client).run(["https://t.me/c/1988700745"])
            self.assertTrue((Path(tmp) / "Sharing is Caring (Telegram)" / "c1988700745_1.jpg").is_file())
        self.assertEqual(sorted(url for url, _ in events["saved"]),
                         [f"https://t.me/c/1988700745/{i}" for i in (1, 2, 3)])

    def test_album_post_and_single(self):
        import telegram_account as tg

        messages = [self._message(i, ".jpg", 20, grouped=99 if i in (10, 11, 12) else None, photo=True) for i in range(8, 15)]
        for url, expected in (("https://t.me/chan/11", ["chan_10.jpg", "chan_11.jpg", "chan_12.jpg"]),
                              ("https://t.me/chan/11?single", ["chan_11.jpg"])):
            client, _ = self._client(messages)
            hooks, _, events = self._hooks()
            with tempfile.TemporaryDirectory() as tmp:
                tg.TelegramDownloads({"session": "", "apiId": 1, "apiHash": "x"}, Path(tmp), hooks,
                                     client_factory=lambda account: client).run([url])
            self.assertEqual(sorted(name for _, name in events["saved"]), expected, url)

    def test_stop_leaves_no_partial_files(self):
        import telegram_account as tg

        client, _ = self._client([self._message(1, ".mp4", 100, video=True)])
        hooks, rows, events = self._hooks()
        hooks.stop.set()
        with tempfile.TemporaryDirectory() as tmp:
            tg.TelegramDownloads({"session": "", "apiId": 1, "apiHash": "x"}, Path(tmp), hooks,
                                 client_factory=lambda account: client).run(["https://t.me/chan/1"])
            self.assertFalse([p for p in Path(tmp).rglob("*") if p.is_file()])
        self.assertFalse(events["saved"])

    def test_login_steps(self):
        import asyncio

        import telegram_account as tg
        import telethon
        from telethon import errors

        sent = []

        class FakeClient:
            def __init__(self, session, api_id, api_hash, **kw):
                self.session = type("S", (), {"save": lambda self: "SESSION-STRING"})()

            async def connect(self):
                pass

            async def disconnect(self):
                pass

            async def send_code_request(self, phone):
                sent.append(phone)
                return type("Sent", (), {"type": type("SentCodeTypeApp", (), {})()})()

            async def sign_in(self, phone=None, code=None, password=None):
                if code == "11111":
                    raise errors.PhoneCodeInvalidError(request=None)
                if code:
                    raise errors.SessionPasswordNeededError(request=None)
                if password != "pw":
                    raise errors.PasswordHashInvalidError(request=None)

            async def get_me(self):
                return type("Me", (), {"first_name": "Saiful", "last_name": None, "username": "saiful"})()

        lines = iter([
            json.dumps({"cmd": "send_code", "apiId": "12", "apiHash": "0" * 31, "phone": "+60 12"}),
            json.dumps({"cmd": "send_code", "apiId": "12", "apiHash": "a" * 32, "phone": "+60 12-345 6789"}),
            json.dumps({"cmd": "code", "code": "1 1 1 1 1"}),
            json.dumps({"cmd": "code", "code": "22222"}),
            json.dumps({"cmd": "password", "password": "nope"}),
            json.dumps({"cmd": "password", "password": "pw"}),
            "",
        ])
        out = []
        original_client, original_out = telethon.TelegramClient, tg._out
        telethon.TelegramClient = FakeClient
        tg._out = out.append
        try:
            asyncio.run(tg.login_loop(lambda: next(lines)))
        finally:
            telethon.TelegramClient, tg._out = original_client, original_out
        steps = [(o["step"], o.get("field", "")) for o in out]
        self.assertEqual(steps, [("error", "api"), ("code", ""), ("error", "code"), ("password", ""), ("error", "password"), ("done", "")])
        self.assertEqual(sent, ["+60123456789"])
        self.assertEqual(out[-1]["session"], "SESSION-STRING")
        self.assertEqual(out[-1]["username"], "saiful")



class RunLogHints20261025(unittest.TestCase):
    """Messages from the 2026-10-25 19:15 run."""

    def test_dead_host_and_password_links_get_a_next_step(self):
        hints = session.Hints("parallel", flare_ready=True)
        hints.failed("https://c6.coomer.party/data/7a/10/x.jpg",
                     "ClientConnectorDNSError - Cannot connect to host c6.coomer.party:443 ssl:default [getaddrinfo failed]")
        level, text = hints.scrape_failed("https://cyberfile.me/b8rp", "Scrape Failed: https://cyberfile.me/b8rp (Password Protected - File/Folder is password protected)")
        self.assertEqual(level, "warning")
        self.assertIn("?password=", text)
        summary = " ".join(hints.summary())
        self.assertIn("c6.coomer.party failed because that server name no longer exists", summary)
        self.assertIn("1 link(s) need a password", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
