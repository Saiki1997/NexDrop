"""Kemono/Coomer file servers that never answer, and Dropbox .blend files (2026-10-25 23:18 and 23:21 runs)."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "desktop"))

from cyberdrop_dl.clients import downloads as dl  # noqa: E402
from cyberdrop_dl.exceptions import DownloadError, InvalidExtensionError  # noqa: E402
from cyberdrop_dl.filepath import get_filename_and_ext  # noqa: E402
from cyberdrop_dl.url_objects import AbsoluteHttpURL  # noqa: E402

bridge = importlib.import_module("bridge")
session = importlib.import_module("cairn_session")

TIMEOUT = "Connection timeout to host https://n3.coomer.st/data/5e/da/5eda.jpg?f=a.jpg"


def item(url):
    async def resolve():
        return AbsoluteHttpURL(url)
    return SimpleNamespace(resolve=resolve)


class UnreachableFileServers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = dl.DownloadClient._download
        self.messages = []
        bridge.emit = self.messages.append
        bridge.UNREACHABLE = bridge._UnreachableSites(limit=3)
        self.calls = []
        self.outcomes = []
        test = self

        async def fake(client, domain, media_item):
            test.calls.append(str(await media_item.resolve()))
            outcome = test.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        dl.DownloadClient._download = fake
        bridge._install_download_clearance()
        self.download = dl.DownloadClient._download

    def tearDown(self):
        dl.DownloadClient._download = self.original

    async def attempt(self, url="https://coomer.st/data/5e/da/5eda.jpg?f=a.jpg"):
        try:
            return await self.download(SimpleNamespace(), "coomer", item(url))
        except Exception as error:  # noqa: BLE001
            return error

    async def test_site_is_skipped_after_timeouts_in_a_row(self):
        self.outcomes = [TimeoutError(TIMEOUT)] * 3
        for _ in range(3):
            await self.attempt()
        self.assertEqual(len(self.messages), 1)
        self.assertIn("Can't connect to coomer.st's file servers", self.messages[0]["message"])
        error = await self.attempt("https://n1.coomer.st/data/aa/bb/x.mp4")
        self.assertIsInstance(error, DownloadError)
        self.assertIn("Server unreachable", str(error))
        self.assertEqual(len(self.calls), 3)  # the skipped file never touched the network
        self.outcomes = [True]
        self.assertTrue(await self.attempt("https://kemono.cr/data/aa/bb/x.png"))  # other sites still download

    async def test_a_working_download_resets_the_count(self):
        self.outcomes = [TimeoutError(TIMEOUT), TimeoutError(TIMEOUT), True, TimeoutError(TIMEOUT), TimeoutError(TIMEOUT), True]
        for _ in range(6):
            await self.attempt()
        self.assertEqual(self.messages, [])
        self.assertEqual(len(self.calls), 6)

    async def test_other_errors_do_not_count(self):
        self.outcomes = [TimeoutError(TIMEOUT), TimeoutError(TIMEOUT), ValueError("404 Not Found"), TimeoutError(TIMEOUT), True]
        for _ in range(5):
            await self.attempt()
        self.assertEqual(self.messages, [])


class Hints(unittest.TestCase):
    def test_timeouts_get_a_vpn_or_proxy_hint(self):
        hints = session.Hints("parallel", flare_ready=True)
        for name in "abc":
            hints.failed(f"https://kemono.cr/data/1f/03/{name}.jpg", f"ConnectionTimeoutError - {TIMEOUT}")
        hints.failed("https://kemono.cr/data/1f/03/d.jpg", "Server unreachable")
        summary = " ".join(hints.summary())
        self.assertIn("4 file(s) from kemono.cr failed because its file servers never answered", summary)
        self.assertIn("VPN or set a proxy", summary)


class LongExtensions(unittest.TestCase):
    def test_blender_files_are_accepted(self):
        self.assertEqual(get_filename_and_ext("Io - code vein -nude.blend"), ("Io - code vein -nude.blend", ".blend"))
        self.assertEqual(get_filename_and_ext("scene.blend1")[1], ".blend1")

    def test_other_long_suffixes_are_still_rejected(self):
        with self.assertRaises(InvalidExtensionError):
            get_filename_and_ext("Episode.Finale")


if __name__ == "__main__":
    unittest.main()
