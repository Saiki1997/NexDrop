"""3.2.3: Bunkr's status page (servers that are down fail as Server busy and are retried later)."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "desktop"))

import bridge  # noqa: E402
import error_classes  # noqa: E402

ROW = '<div class="flex items-center gap-4 py-4 border-b border-soft last:border-b-0"><div><p>{name}</p></div><div><span class="badge">{status}</span></div></div>'
PAGE = "<html><body><h1>Status</h1>" + "".join(
    ROW.format(name=n, status=s) for n, s in (("c5bu-b", "Operational"), ("c2rm-b", "Non-operational"), ("Kebab", "Down"))
) + "</body></html>"


class BunkrStatus(unittest.TestCase):
    def test_parse_status_rows(self):
        self.assertEqual(bridge._BunkrStatus.parse(PAGE), {"c5bu-b": "Operational", "c2rm-b": "Non-operational", "kebab": "Down"})
        self.assertEqual(bridge._BunkrStatus.parse("<html>nothing here</html>"), {})

    def test_down_by_subdomain(self):
        status = bridge._BunkrStatus()
        status.servers = bridge._BunkrStatus.parse(PAGE)
        self.assertIsNone(status.down("c5bu-b.cdn.cr"))
        self.assertEqual(status.down("c2rm-b.cdn.cr"), "Non-operational")
        self.assertEqual(status.down("KEBAB.bunkr.ru"), "Down")
        self.assertIsNone(status.down("unknown.cdn.cr"))

    def test_unreadable_page_skips_nothing_and_is_not_asked_again_at_once(self):
        status = bridge._BunkrStatus()
        with patch("urllib.request.urlopen", side_effect=OSError("blocked")) as opened:
            status.refresh()
            status.refresh()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(status.servers, {})
        self.assertIsNone(status.down("c2rm-b.cdn.cr"))

    def test_file_on_a_down_server_fails_as_server_busy(self):
        from cyberdrop_dl.downloader.http import Downloader
        from cyberdrop_dl.exceptions import DownloadError
        from yarl import URL

        original = Downloader._check_skip_by_config
        calls = []

        async def fake(self, media_item):
            calls.append(media_item)

        Downloader._check_skip_by_config = fake
        old = bridge.BUNKR_STATUS
        try:
            bridge.BUNKR_STATUS = bridge._BunkrStatus()
            bridge.BUNKR_STATUS.servers = bridge._BunkrStatus.parse(PAGE)
            bridge.BUNKR_STATUS.read_at = float("inf")  # no network in the test
            bridge._install_bunkr_status()
            down = SimpleNamespace(domain="bunkr", is_segment=False, parents=[], url=URL("https://c2rm-b.cdn.cr/a.mp4"))
            up = SimpleNamespace(domain="bunkr", is_segment=False, parents=[], url=URL("https://c5bu-b.cdn.cr/b.mp4"))
            other = SimpleNamespace(domain="gofile", is_segment=False, parents=[], url=URL("https://c2rm-b.example/c.mp4"))
            with patch.object(bridge.BUNKR_STATUS, "refresh"), self.assertRaises(DownloadError) as caught:
                asyncio.run(Downloader._check_skip_by_config(None, down))
            asyncio.run(Downloader._check_skip_by_config(None, up))
            asyncio.run(Downloader._check_skip_by_config(None, other))
            self.assertEqual(calls, [up, other])
            self.assertFalse(caught.exception.retry)
            self.assertEqual(error_classes.classify(f"Server Down {caught.exception}"), "server_busy")
            self.assertIn("server_busy", error_classes.RETRY_LATER)
        finally:
            Downloader._check_skip_by_config = original
            bridge.BUNKR_STATUS = old


if __name__ == "__main__":
    unittest.main()
