"""3.2.2: one download per content in a run (hosts that publish the hash before downloading)."""

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

import bridge  # noqa: E402
from cyberdrop_dl.crawlers.crawler import Crawler  # noqa: E402

SHA = "a" * 64


class SameContent(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.emit = patch.object(bridge, "emit", self.events.append)
        self.emit.start()
        bridge.SAME_CONTENT = bridge._SameContent()
        self.original = Crawler.check_complete_by_hash
        self.on_record = False

        async def fake_original(_self, url, algo, checksum):
            return self.on_record

        Crawler.check_complete_by_hash = fake_original
        bridge._install_same_content()
        stats = SimpleNamespace(prev_completed=0)
        self.crawler = SimpleNamespace(config=SimpleNamespace(ignore_hashes=False), tui=SimpleNamespace(files=SimpleNamespace(stats=stats)))

    def tearDown(self):
        Crawler.check_complete_by_hash = self.original
        self.emit.stop()

    def check(self, url, checksum=SHA):
        return asyncio.run(Crawler.check_complete_by_hash(self.crawler, url, "sha256", checksum))

    def test_second_link_with_same_hash_is_skipped(self):
        self.assertFalse(self.check("https://pixeldrain.com/api/file/AAAAAAAA"))
        self.assertTrue(self.check("https://gofile.io/d/other-name.mp4"))
        self.assertEqual(self.events[-1]["type"], "skipped")
        self.assertIn("same content", self.events[-1]["reason"])
        self.assertEqual(self.crawler.tui.files.stats.prev_completed, 1)

    def test_case_and_other_content_and_same_link(self):
        self.assertFalse(self.check("https://a.example/1", SHA.upper()))
        self.assertFalse(self.check("https://a.example/1"))  # the same link asked again is not a duplicate
        self.assertFalse(self.check("https://b.example/2", "b" * 64))
        self.assertTrue(self.check("https://c.example/3", SHA))

    def test_on_record_and_ignore_hashes(self):
        self.on_record = True
        self.assertTrue(self.check("https://a.example/1"))
        self.assertEqual(self.events, [])  # the engine's own skip, reported by its log line
        self.on_record = False
        self.crawler.config.ignore_hashes = True
        self.assertFalse(self.check("https://a.example/1"))
        self.assertFalse(self.check("https://b.example/2"))

    def test_held_links_fail_with_the_first(self):
        first = "https://pixeldrain.com/api/file/AAAAAAAA"
        self.check(first)
        self.check("https://b.example/2")
        self.events.clear()
        bridge.SAME_CONTENT.failed("Scrape Failed: https://pixeldrain.com/u/AAAAAAAA (509 limit)")
        self.assertEqual([e["type"] for e in self.events], ["failed"])
        self.assertEqual(self.events[0]["url"], "https://b.example/2")
        self.assertFalse(self.check("https://b.example/2"))  # free to download now

    def test_unrelated_failure_keeps_the_hold(self):
        self.check("https://a.example/file-one")
        self.check("https://b.example/2")
        self.events.clear()
        bridge.SAME_CONTENT.failed("Download Failed: https://z.example/x (404)")
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
