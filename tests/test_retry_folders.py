"""3.2.4: Retry failed saves each file into the folder it was meant for."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "desktop"))

import bridge  # noqa: E402
from cyberdrop_dl.downloader.http import Downloader  # noqa: E402
from yarl import URL  # noqa: E402


class RetryFolders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.thread = self.root / "SimpCity" / "Some thread"
        self.events = []
        self.emit = patch.object(bridge, "emit", self.events.append)
        self.emit.start()
        self.original = Downloader._download
        self.seen = []

        async def fake(_self, media_item):
            self.seen.append(Path(media_item.download_folder))
            return True

        Downloader._download = fake

    def tearDown(self):
        Downloader._download = self.original
        self.emit.stop()
        self.tmp.cleanup()

    def item(self, url, referer, folder):
        return SimpleNamespace(url=URL(url), referer=URL(referer) if referer else "", download_folder=folder, is_segment=False)

    def run_item(self, item):
        return asyncio.run(Downloader._download(None, item))

    def test_every_file_reports_its_folder_without_type_folder(self):
        bridge._install_retry_folders({}, self.root)
        self.run_item(self.item("https://c5bu-b.cdn.cr/a.mp4", "https://bunkr.cr/f/abc", self.thread / "Videos"))
        self.run_item(self.item("https://c5bu-b.cdn.cr/a.mp4", "https://bunkr.cr/f/abc", self.thread / "Videos"))
        origins = [e for e in self.events if e["type"] == "origin"]
        self.assertEqual(len(origins), 1)  # once per file
        self.assertEqual(origins[0]["folder"], str(self.thread))
        self.assertEqual(origins[0]["referer"], "https://bunkr.cr/f/abc")

    def test_retry_goes_to_the_recorded_folder_by_page_or_link(self):
        bridge._install_retry_folders({
            "https://bunkr.cr/f/abc": str(self.thread),
            "https://pixeldrain.com/api/file/XYZ?download": str(self.thread),
            "relative/bad": "x",
            "https://bunkr.cr/f/rel": "not/absolute",
        }, self.root)
        loose = self.root / "bunkr.cr" / "abc"
        # a fresh CDN link (new token) found from the same page
        self.run_item(self.item("https://c9xx-b.cdn.cr/a.mp4?token=new", "https://bunkr.cr/f/abc", loose))
        # the same file link without its query string
        self.run_item(self.item("https://pixeldrain.com/api/file/XYZ", "", self.root / "pixeldrain"))
        # not part of the retry: left where the engine put it
        self.run_item(self.item("https://c9xx-b.cdn.cr/b.mp4", "https://bunkr.cr/f/rel", self.root / "other"))
        self.assertEqual(self.seen, [self.thread, self.thread, self.root / "other"])

    def test_type_folder_inside_the_recorded_folder_is_kept(self):
        bridge._install_retry_folders({"https://bunkr.cr/f/abc": str(self.thread)}, self.root)
        self.run_item(self.item("https://c5bu-b.cdn.cr/a.mp4", "https://bunkr.cr/f/abc", self.thread / "Videos"))
        self.assertEqual(self.seen, [self.thread / "Videos"])

    def test_desktop_keeps_only_absolute_folders_for_web_links(self):
        import cairn_session

        folder = str(self.thread)
        clean = cairn_session.retry_folders({"https://a.example/x": folder, "ftp://a/b": folder, "https://b.example/y": "rel/x", 3: folder})
        self.assertEqual(clean, {"https://a.example/x": folder})
        self.assertEqual(cairn_session.retry_folders(None), {})

    def test_desktop_collects_folders_for_failed_files(self):
        import cairn_session

        files = cairn_session.FILES
        url, page = "https://c5bu-b.cdn.cr/zz-retry.mp4", "https://bunkr.cr/f/zz"
        files.update(url, folder=str(self.thread), referer=page)
        files.update("https://c5bu-b.cdn.cr/no-folder.mp4", state="failed")
        got = files.folders([url, "https://c5bu-b.cdn.cr/no-folder.mp4"])
        self.assertEqual(got, {url: str(self.thread), page: str(self.thread)})


if __name__ == "__main__":
    unittest.main()
