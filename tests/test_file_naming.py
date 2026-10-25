"""Album filenames and dated post folders, checked offline with stubbed crawlers."""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))

from cyberdrop_dl import source_order  # noqa: E402
from cyberdrop_dl.url_objects import AbsoluteHttpURL, ScrapeItem  # noqa: E402

DOWNLOADS = Path("downloads").resolve()


def _item(url: str) -> ScrapeItem:
    return ScrapeItem(url=AbsoluteHttpURL(url), download_folder=DOWNLOADS)


def _crawler(cls):
    crawler = object.__new__(cls)
    saved = []

    async def handle_media_item(media_item, m3u8=None):
        saved.append(media_item)

    crawler.handle_media_item = handle_media_item
    crawler._prepare_headers = lambda item: {}
    crawler.config = types.SimpleNamespace(
        download_folder=DOWNLOADS,
        filters=types.SimpleNamespace(files=types.SimpleNamespace(thumbnails=False)),
        subfolders=types.SimpleNamespace(
            include=types.SimpleNamespace(album_id=False, domain=True, thread_id=False),
            separate_posts=types.SimpleNamespace(enabled=False, format="{default}"),
        ),
    )
    return crawler, saved


class AlbumNames(unittest.TestCase):
    def setUp(self):
        source_order.reset()
        self.tokens = [source_order.ALBUM_NAMES.set(True), source_order.SEQUENCE_PREFIX.set(True)]

    def tearDown(self):
        source_order.SEQUENCE_PREFIX.reset(self.tokens[1])
        source_order.ALBUM_NAMES.reset(self.tokens[0])

    def _album(self, crawler, name="Peachuu Tae", album_id="8TwlNRmn"):
        item = _item(f"https://bunkr.cr/a/{album_id}")
        item.setup_as_album(crawler.create_title(name, album_id), album_id=album_id)
        return item

    def _save(self, crawler, item, cdn, name, **kw):
        asyncio.run(crawler.handle_file(AbsoluteHttpURL(cdn), item, name, Path(name).suffix, **kw))

    def test_album_files_take_the_album_name_in_page_order(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        crawler, saved = _crawler(BunkrCrawler)
        album = self._album(crawler)
        pages = list(album.create_children(AbsoluteHttpURL(f"https://bunkr.cr/f/{n}") for n in "abc"))
        # The file pages answer out of order; names still follow the album page.
        self._save(crawler, pages[2], "https://cdn.bunkr.ru/IMG_8403.png", "IMG_8403.png")
        self._save(crawler, pages[0], "https://cdn.bunkr.ru/copy_08EB.mp4", "copy_08EB.mp4")
        self._save(crawler, pages[1], "https://cdn.bunkr.ru/IMG_8916.png", "IMG_8916.png")
        self.assertEqual(
            [m.filename for m in saved], ["Peachuu Tae 003.png", "Peachuu Tae 001.mp4", "Peachuu Tae 002.png"]
        )
        self.assertEqual(saved[0].original_filename, "IMG_8403.png")
        self.assertEqual(saved[0].download_folder.name, "Peachuu Tae (Bunkr)")

    def test_two_files_from_one_page_and_bad_characters(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        crawler, saved = _crawler(BunkrCrawler)
        album = self._album(crawler, 'Set: "one" / two?')
        page = next(album.create_children([AbsoluteHttpURL("https://bunkr.cr/f/a")]))
        self._save(crawler, page, "https://cdn.bunkr.ru/1.jpg", "1.jpg")
        self._save(crawler, page, "https://cdn.bunkr.ru/2.jpg", "2.jpg")
        names = [m.filename for m in saved]
        self.assertEqual(len(set(names)), 2)
        for name in names:
            self.assertFalse(set(name) & set('<>:"/\\|?*'), name)
            self.assertTrue(name.endswith(".jpg"))

    def test_single_files_and_setting_off_keep_their_names(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        crawler, saved = _crawler(BunkrCrawler)
        loose = _item("https://bunkr.cr/f/x")
        self._save(crawler, loose, "https://cdn.bunkr.ru/IMG_1.png", "IMG_1.png")
        page = next(self._album(crawler).create_children([AbsoluteHttpURL("https://bunkr.cr/f/y")]))
        token = source_order.ALBUM_NAMES.set(False)
        try:
            self._save(crawler, page, "https://cdn.bunkr.ru/IMG_2.png", "IMG_2.png")
        finally:
            source_order.ALBUM_NAMES.reset(token)
        self.assertEqual([m.filename for m in saved], ["001_IMG_1.png", "002_IMG_2.png"])

    def test_nested_album_counts_on_its_own(self):
        parent = ScrapeItem(url=AbsoluteHttpURL("https://gofile.io/d/root"))
        parent.setup_as_album("Root")
        first, second = parent.create_children([AbsoluteHttpURL("https://x/1"), AbsoluteHttpURL("https://x/2")])
        self.assertEqual((first.album_index, second.album_index), (1, 2))
        second.setup_as_album("Sub")
        child = next(second.create_children([AbsoluteHttpURL("https://x/3")]))
        self.assertEqual((child.album_title, child.album_index), ("Sub", 1))


class PostFolders(unittest.TestCase):
    def _title(self, cls, on, *args):
        crawler, _ = _crawler(cls)
        token = source_order.POST_FOLDERS.set(on)
        try:
            return crawler.create_separate_post_title(*args)
        finally:
            source_order.POST_FOLDERS.reset(token)

    def test_dated_post_folder_is_optional(self):
        import datetime

        from cyberdrop_dl.crawlers.twitter.crawler import TwitterCrawler

        date = datetime.datetime(2024, 12, 30, tzinfo=datetime.UTC).timestamp()
        self.assertEqual(self._title(TwitterCrawler, True, None, "1873747839563755753", date), "2024-12-30 - 1873747839563755753")
        self.assertEqual(self._title(TwitterCrawler, False, None, "1873747839563755753", date), "")

    def test_gallery_sites_keep_the_set_name(self):
        import datetime

        from cyberdrop_dl.crawlers.girlsreleased import GirlsReleasedCrawler

        self.assertEqual(self._title(GirlsReleasedCrawler, False, "Beach Set", "42", datetime.date(2024, 1, 2)), "Beach Set")


if __name__ == "__main__":
    unittest.main()
