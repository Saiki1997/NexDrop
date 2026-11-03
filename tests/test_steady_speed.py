"""Steady download speed (3.2): a connection that goes silent or slows right down reconnects and
resumes, without using up the file's attempts. Pixeldrain's free daily transfer limit is checked
before a download starts.

From real run logs: Bunkr (cdn.cr) and Turbo files sat on a silent connection until the 60 s read
timeout ("999 Timeout"), then used up their 2 attempts.
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_server import LocalServer, content  # noqa: E402
from test_engine_integrity import by_url, run_bridge  # noqa: E402

from cyberdrop_dl.clients import steady  # noqa: E402
from cyberdrop_dl.crawlers.pixeldrain import File, PixelDrainAPI, PixelDrainCrawler  # noqa: E402
from cyberdrop_dl.exceptions import DownloadError, ScrapeError  # noqa: E402
from cyberdrop_dl.url_objects import AbsoluteHttpURL  # noqa: E402

MB = 1024 * 1024


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def run(watch: steady.SpeedWatch, clock: Clock, seconds: float, rate: float, step: float = 0.25) -> None:
    end = clock.now + seconds
    while clock.now < end:
        clock.now += step
        watch.feed(int(rate * step))


class SpeedWatchTests(unittest.TestCase):
    def test_steady_connection_is_left_alone(self):
        clock = Clock()
        watch = steady.SpeedWatch(clock=clock)
        run(watch, clock, 120, 10 * MB)
        self.assertGreater(watch.best, 9 * MB)

    def test_speed_that_falls_to_a_tenth_reconnects(self):
        clock = Clock()
        watch = steady.SpeedWatch(clock=clock)
        run(watch, clock, 40, 10 * MB)
        with self.assertRaises(steady.SpeedDropped) as caught:
            run(watch, clock, 60, 1 * MB)
        self.assertIn("Reconnecting", str(caught.exception))
        self.assertLess(clock.now, 40 + 2 * steady.WINDOW + 1)  # within two windows of the drop

    def test_short_dip_is_not_a_drop(self):
        clock = Clock()
        watch = steady.SpeedWatch(clock=clock)
        run(watch, clock, 40, 10 * MB)
        run(watch, clock, 5, 0.5 * MB)
        run(watch, clock, 60, 10 * MB)

    def test_a_connection_that_was_always_slow_is_not_dropping(self):
        clock = Clock()
        watch = steady.SpeedWatch(clock=clock)
        run(watch, clock, 30, 300 * 1024)
        run(watch, clock, 60, 20 * 1024)

    def test_off_with_a_speed_cap_or_without_ranges(self):
        clock = Clock()
        watch = steady.SpeedWatch(enabled=False, clock=clock)
        run(watch, clock, 40, 10 * MB)
        run(watch, clock, 60, 100 * 1024)
        self.assertFalse(steady.can_resume(200, {}))
        self.assertTrue(steady.can_resume(200, {"Accept-Ranges": "bytes"}))
        self.assertTrue(steady.can_resume(206, {}))


class SilentResponse:
    """Sends `chunks` chunks, then goes silent."""

    def __init__(self, chunks: int) -> None:
        self.left = chunks

    def iter_chunked(self, _size):
        async def gen():
            while self.left:
                self.left -= 1
                yield b"x" * 10
            await asyncio.sleep(3600)
            yield b""

        return gen()


class StallTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_connection_raises_timeout_quickly(self):
        got = []
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            async for chunk in steady.chunks(SilentResponse(3), 1024, stall=0.2):
                got.append(chunk)
        self.assertEqual(len(got), 3)
        self.assertLess(time.monotonic() - started, 2)

    async def test_finished_body_ends_normally(self):
        class Done:
            def iter_chunked(self, _size):
                async def gen():
                    yield b"a"
                    yield b"b"

                return gen()

        self.assertEqual([c async for c in steady.chunks(Done(), 1, stall=0.2)], [b"a", b"b"])


class FreeReconnects(unittest.TestCase):
    def test_progress_gives_the_attempt_back_up_to_the_budget(self):
        item = SimpleNamespace(attempts=1, extra_info={})
        moved = DownloadError(999, "Download timeout reached, retrying", retry=True)
        for _ in range(steady.MAX_RECONNECTS):
            item.attempts += 1
            self.assertTrue(steady.free_reconnect(item, moved))
        item.attempts += 1
        self.assertFalse(steady.free_reconnect(item, moved))
        self.assertEqual(item.attempts, 2)

    def test_other_errors_count_as_attempts(self):
        item = SimpleNamespace(attempts=1, extra_info={})
        self.assertFalse(steady.free_reconnect(item, DownloadError(503, "busy", retry=True)))
        self.assertEqual(item.attempts, 1)


class EngineRun(unittest.TestCase):
    """The real engine against the local server: one try per file, a 2 s stall wait."""

    SIZE = 3 * MB + 321

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.tmp.name)
        cls.server = LocalServer().__enter__()
        stamp = str(time.time_ns())
        cls.urls = {
            "stall": cls.server.url("stall", f"st{stamp}.mp4", size=cls.SIZE),
            "seg-stall": cls.server.url("seg-stall", f"ss{stamp}.mp4", size=cls.SIZE),
            "seg-busy": cls.server.url("seg-busy", f"sb{stamp}.mp4", size=cls.SIZE),
        }
        cls.started = time.monotonic()
        cls.events = run_bridge(list(cls.urls.values()), cls.folder, attempts=1, stallSeconds=2,
                                segments=4, segmentMinMB=1)
        cls.took = time.monotonic() - cls.started
        cls.saved = by_url(cls.events, "saved")
        cls.failed = by_url(cls.events, "failed")

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()
        cls.tmp.cleanup()

    def name(self, key):
        return self.urls[key].split("/")[4].split("?")[0]

    def saved_bytes(self, key):
        event = self.saved.get(self.urls[key])
        self.assertIsNotNone(event, f"{key} not saved; failed: {self.failed.get(self.urls[key])}")
        return Path(event["path"]).read_bytes()

    def test_silent_connection_resumes_on_a_new_one_without_using_an_attempt(self):
        self.assertEqual(self.saved_bytes("stall"), content(self.name("stall"), self.SIZE))
        self.assertGreaterEqual(self.server.hits[f"stall/{self.name('stall')}"], 2)
        resumed = self.server.headers[f"stall/{self.name('stall')}"].get("Range", "")
        self.assertTrue(resumed.startswith("bytes=") and not resumed.startswith("bytes=0-"), resumed)

    def test_silent_part_reconnects_on_its_own(self):
        self.assertEqual(self.saved_bytes("seg-stall"), content(self.name("seg-stall"), self.SIZE))
        resumed = self.server.ranges.get(f"seg-stall/{self.name('seg-stall')}", [])
        self.assertTrue(resumed, "the stalled part was not asked again")

    def test_part_refused_mid_way_keeps_part_one_and_continues_as_one_stream(self):
        self.assertEqual(self.saved_bytes("seg-busy"), content(self.name("seg-busy"), self.SIZE))
        resumed = self.server.ranges.get(f"seg-busy/{self.name('seg-busy')}", [])
        self.assertTrue(resumed, "the one-stream download started over from byte 0")

    def test_reconnects_are_not_logged_as_failures(self):
        errors = [e.get("message", "") for e in self.events if e.get("type") == "log" and e.get("level") == "error"]
        self.assertFalse([m for m in errors if "Speed dropped" in m or "No data for" in m], errors)

    def test_well_before_the_read_timeout(self):
        self.assertLess(self.took, 45)


class ConnectionCap(unittest.TestCase):
    MESSAGE = ("403 Forbidden - You have reached the maximum number of open download connections for "
               "free/anonymous accounts. If you wish to download more please wait for previous downloads to "
               "finish. (max_concurrent)")

    def test_pixeldrain_connection_limit_waits_without_using_attempts(self):
        item = SimpleNamespace(attempts=1, extra_info={})
        error = DownloadError(403, self.MESSAGE)
        for _ in range(steady.MAX_CAP_WAITS):
            item.attempts += 1
            self.assertTrue(steady.connection_cap_wait(item, error))
        self.assertFalse(steady.connection_cap_wait(item, error))
        self.assertFalse(steady.connection_cap_wait(item, DownloadError(403, "Forbidden")))

    def test_classified_as_rate_limited_not_sign_in(self):
        from error_classes import classify

        self.assertEqual(classify(self.MESSAGE, "https://pixeldrain.com/api/file/x?download"), "rate_limited")

    def test_reconnect_waits_grow_to_thirty_seconds(self):
        item = SimpleNamespace(extra_info={})
        waits = []
        for used in range(1, 9):
            item.extra_info["steady_reconnects"] = used
            waits.append(steady.reconnect_delay(item))
        self.assertEqual(waits, [1, 2, 4, 8, 16, 30, 30, 30])


class BridgeRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "engine"))
        import bridge

        cls.bridge = bridge

    def test_only_real_bot_checks_go_to_flaresolverr(self):
        b = self.bridge
        self.assertTrue(b._looks_like_bot_check("<title>DDoS-Guard</title>"))
        self.assertTrue(b._looks_like_bot_check("<title>Just a moment...</title><script src=/cdn-cgi/challenge-platform/>"))
        self.assertFalse(b._looks_like_bot_check("<html><body>You need to be signed in to view original images</body></html>"))
        self.assertFalse(b._looks_like_bot_check(""))

    def test_no_split_on_one_connection_servers_or_pixeldrain(self):
        from cyberdrop_dl.clients import segments

        b = self.bridge
        b._install_segments(4, 64)
        slots = b.SERVER_SLOTS
        old = slots.per_server
        try:
            slots.get("c5bu-b.cdn.cr")
            slots.per_server = 3
            self.assertTrue(segments.host_ok("c5bu-b.cdn.cr"))
            slots.per_server = 1
            self.assertFalse(segments.host_ok("c5bu-b.cdn.cr"))
            self.assertTrue(segments.host_ok("other.example"))
            self.assertFalse(segments.host_ok("pixeldrain.com"))
        finally:
            slots.per_server = old
            slots.hosts.pop("c5bu-b.cdn.cr", None)


def pd_file(size):
    return File(id="Ar8XUD9B", name="v.mp4", date_upload="2025-10-23T13:17:04Z", mime_type="video/mp4",
                hash_sha256="x", size=size)


class PixeldrainQuota(unittest.IsolatedAsyncioTestCase):
    def crawler(self, left, logged_in=False):
        api = SimpleNamespace(logged_in=logged_in, transfer_left=AsyncMock(return_value=left), reserve=lambda n: None,
                              download=lambda f: AbsoluteHttpURL(f"https://pixeldrain.com/api/file/{f.id}?download"))
        return SimpleNamespace(api=api, origin=AbsoluteHttpURL("https://pixeldrain.com"),
                               check_complete_by_hash=AsyncMock(return_value=False),
                               get_filename_and_ext=lambda name, mime_type=None: (name, ".mp4"),
                               parse_iso_date=lambda d: 0, handle_file=AsyncMock())

    async def test_file_bigger_than_what_is_left_fails_up_front(self):
        c = self.crawler(left=100 * MB)
        with self.assertRaises(ScrapeError) as caught:
            await PixelDrainCrawler._file(c, SimpleNamespace(), pd_file(900 * MB))
        self.assertIn("bandwidth limit", str(caught.exception))
        c.handle_file.assert_not_awaited()
        from error_classes import classify

        self.assertEqual(classify(str(caught.exception)), "quota")

    async def test_file_that_fits_or_api_key_or_unknown_limit_downloads(self):
        for left, logged_in in ((2000 * MB, False), (10 * MB, True), (None, False)):
            c = self.crawler(left, logged_in)
            await PixelDrainCrawler._file(c, SimpleNamespace(), pd_file(900 * MB))
            c.handle_file.assert_awaited_once()

    async def test_limit_is_read_once_a_minute_and_counts_started_files(self):
        api = SimpleNamespace(_quota=None, origin=AbsoluteHttpURL("https://pixeldrain.com"),
                              text=AsyncMock(return_value='{"transfer_limit":6000000000,"transfer_limit_used":1000000000}'))
        self.assertEqual(await PixelDrainAPI.transfer_left(api), 5_000_000_000)
        PixelDrainAPI.reserve(api, 2_000_000_000)
        self.assertEqual(await PixelDrainAPI.transfer_left(api), 3_000_000_000)
        api.text.assert_awaited_once()
        api._quota = None
        api.text = AsyncMock(side_effect=OSError("offline"))
        self.assertIsNone(await PixelDrainAPI.transfer_left(api))


if __name__ == "__main__":
    unittest.main()
