"""Smart naming (2.5): folder and file names from crawl metadata, checked offline with stubbed crawlers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "desktop"))

from bs4 import BeautifulSoup  # noqa: E402

from cyberdrop_dl import smart_naming, source_order  # noqa: E402
from cyberdrop_dl.smart_naming import NamingSettings, meaningfulness  # noqa: E402
from cyberdrop_dl.url_objects import AbsoluteHttpURL, ScrapeItem  # noqa: E402

URL = AbsoluteHttpURL


def _crawler(cls, root: Path, *, site_in_folder: bool = False, separate_posts: bool = False):
    crawler = object.__new__(cls)
    saved = []

    async def handle_media_item(media_item, m3u8=None):
        saved.append(media_item)

    crawler.handle_media_item = handle_media_item
    crawler._prepare_headers = lambda item: {}
    crawler.config = types.SimpleNamespace(
        download_folder=root,
        filters=types.SimpleNamespace(files=types.SimpleNamespace(thumbnails=False)),
        subfolders=types.SimpleNamespace(
            include=types.SimpleNamespace(album_id=False, domain=site_in_folder, thread_id=False),
            separate_posts=types.SimpleNamespace(enabled=separate_posts, format="{default}"),
        ),
    )
    return crawler, saved


class SmartCase(unittest.TestCase):
    """Smart naming on with the 2.5 defaults, in a temporary download folder."""

    settings = NamingSettings.from_payload({})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Registered first, so it runs last: on Windows an open job database or log can't be deleted.
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        source_order.reset()
        smart_naming.REGISTRY.load(None, self.root)
        self.tokens = [
            (smart_naming.SETTINGS, smart_naming.SETTINGS.set(self.settings)),
            (source_order.NUMBER_MODE, source_order.NUMBER_MODE.set(self.settings.numbering)),
        ]

    def tearDown(self):
        for var, token in reversed(self.tokens):
            var.reset(token)

    def item(self, url: str) -> ScrapeItem:
        return ScrapeItem(url=URL(url), download_folder=self.root)

    def save(self, crawler, item, cdn: str, name: str, **kw):
        asyncio.run(crawler.handle_file(URL(cdn), item, name, source_order.split_name(name)[1], **kw))

    def rel(self, media_item) -> str:
        return media_item.download_folder.relative_to(self.root).as_posix()


class Settings(unittest.TestCase):
    def test_defaults_follow_the_spec(self):
        s = NamingSettings.from_payload({})
        self.assertEqual(
            (s.smart, s.strategy, s.numbering, s.folders, s.site_in_folder, s.replace_generic, s.keep_meaningful, s.max_len),
            (True, "auto", "suffix", "auto", False, False, True, 120),
        )

    def test_old_numbering_off_stays_off_and_bad_values_fall_back(self):
        self.assertEqual(NamingSettings.from_payload({"sequencePrefix": False}).numbering, "off")
        s = NamingSettings.from_payload({"numbering": "sideways", "folderNaming": "x", "maxNameLength": 5})
        self.assertEqual((s.numbering, s.folders, s.max_len), ("suffix", "auto", 20))

    def test_engine_default_is_still_the_old_behaviour(self):
        self.assertFalse(smart_naming.SETTINGS.get().smart)
        self.assertEqual(smart_naming.SETTINGS.get().folders, "original")


class MeaningfulNames(unittest.TestCase):
    def test_hashes_ids_and_camera_names_are_not_titles(self):
        for stem in ("a9c82fa8", "ab8273fd", "f82a9391", "9af7283c", "video_183726", "IMG_8403", "DSC01234",
                     "GaB12xYW0AAbc9z", "1873747839563755753", "Xk9aB2qP", "20240512_123456",
                     "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "Screenshot_20240101-101010", "7351234567_img000"):
            self.assertLess(meaningfulness(stem), 0.6, stem)

    def test_real_titles_are_kept(self):
        for stem in ("Beach day", "Summer Photoshoot 2026", "allecakes summer", "Project Files v2", "my_vacation_video",
                     "Невеста", "夏の写真", "Chapter 3 - The Return"):
            self.assertGreaterEqual(meaningfulness(stem), 0.6, stem)

    def test_captions_are_cleaned(self):
        clean = smart_naming.clean_title
        self.assertEqual(clean("New Summer Photoshoot 🌞 https://t.co/abc123 #summer #beach #model"), "New Summer Photoshoot")
        self.assertEqual(clean("@friend @other New set is up! link in bio"), "New set is up")
        self.assertEqual(clean("Beach #day with #friends"), "Beach day with friends")
        self.assertEqual(clean("https://bunkr.cr/a/xyz"), "")
        self.assertEqual(clean("Pics"), "")
        long = clean("This is the first sentence of a much longer post. " * 6, 60)
        self.assertLessEqual(len(long), 60)
        self.assertTrue(long.startswith("first sentence of a much longer post"), long)  # V2 drops the lead-in

    def test_page_titles_lose_only_the_site_name(self):
        soup = BeautifulSoup('<html><head><meta property="og:title" content="Beach Day | Bunkr">'
                             '<meta property="og:site_name" content="Bunkr"></head></html>', "html.parser")
        smart_naming.note_page("https://bunkr.cr/f/abc", soup)
        self.assertEqual(smart_naming.page_title("https://bunkr.cr/f/abc")[0], "Beach Day")
        soup = BeautifulSoup("<html><head><title>GoFile Uploads Explained - gofile.io</title></head></html>", "html.parser")
        smart_naming.note_page("https://gofile.io/d/x", soup)
        self.assertEqual(smart_naming.page_title("https://gofile.io/d/x")[0], "GoFile Uploads Explained")

    def test_forum_caption_skips_quotes_links_and_noise(self):
        html = """<div class="bbWrapper"><blockquote>quoted reply</blockquote>
        https://bunkr.cr/a/xyz<br>Summer Photoshoot 2026<br><a href="x">more</a></div>"""
        content = BeautifulSoup(html, "html.parser")
        for quote in content.select("blockquote"):
            quote.decompose()
        self.assertEqual(smart_naming.forum_caption(content), "Summer Photoshoot 2026")
        self.assertEqual(smart_naming.forum_caption(BeautifulSoup("<div>thx</div>", "html.parser")), "")

    def test_forum_caption_ignores_link_labels(self):
        # Seen in a real SimpCity thread: the first line was a link to the discussion thread.
        html = '<div><a href="/threads/x-discussion-thread.1">Discussion thread \U0001f447</a><br><img src="a.jpg"></div>'
        content = BeautifulSoup(html, "html.parser")
        self.assertEqual(smart_naming.forum_caption(content), "")
        self.assertIsNotNone(content.select_one("a"))  # the page itself is not changed
        html = '<div><a href="https://bunkr.cr/f/x">[Photobook] Sweet PHOTOBOOK.7z</a><br>Summer set</div>'
        self.assertEqual(smart_naming.forum_caption(BeautifulSoup(html, "html.parser")), "Summer set")

    def test_download_pages_titled_with_a_hash_are_not_titles(self):
        for title in ("Download 7b72bde7c9c9a435aba7322424cbb9e0.mp4", "Download XMr9wJSCVuM.mp4"):
            soup = BeautifulSoup(f'<html><head><meta property="og:title" content="{title}"></head></html>', "html.parser")
            smart_naming.note_page("https://pixeldrain.com/u/abc", soup)
            self.assertEqual(smart_naming.page_title("https://pixeldrain.com/u/abc")[0], "", title)
        soup = BeautifulSoup('<html><head><meta property="og:title" content="Download Beach Day.mp4"></head></html>', "html.parser")
        smart_naming.note_page("https://pixeldrain.com/u/def", soup)
        self.assertEqual(smart_naming.page_title("https://pixeldrain.com/u/def")[0], "Beach Day")


class Numbering(unittest.TestCase):
    def setUp(self):
        source_order.reset()

    def test_suffix_goes_before_the_extension_and_grows_past_999(self):
        fmt = source_order.format_number
        self.assertEqual(fmt(1, "Photoshoot.jpg", "suffix"), "Photoshoot-001.jpg")
        self.assertEqual(fmt(1000, "Photoshoot.jpg", "suffix"), "Photoshoot-1000.jpg")
        self.assertEqual(fmt(3, "backup.tar.gz", "suffix"), "backup-003.tar.gz")
        self.assertEqual(fmt(3, "Video.mp4", "prefix"), "003_Video.mp4")
        self.assertEqual(fmt(3, "Video.mp4", "off"), "Video.mp4")
        self.assertEqual(fmt(3, "Video-003.mp4", "suffix"), "Video-003.mp4")

    def test_same_url_keeps_its_number_for_retries(self):
        first = source_order.assign("https://x/1")
        source_order.assign("https://x/2")
        self.assertEqual(source_order.assign("https://x/1"), first)

    def test_each_job_starts_at_one_and_a_resumed_job_keeps_its_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "m.jsonl"
            rows = [
                {"url": "https://old/legacy", "sequence": 1500, "name": "1500_x.jpg"},
                {"url": "https://a/1", "sequence": 1, "name": "Summer-001.jpg", "job": "A", "naming": "x"},
                {"url": "https://a/2", "sequence": 2, "name": "Summer-002.jpg", "job": "A", "naming": "x"},
            ]
            manifest.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            source_order.reset(str(manifest), "A")
            self.assertEqual(source_order.assign("https://a/2"), 2)
            self.assertEqual(source_order.assign("https://a/3"), 3)
            self.assertEqual(source_order.stored("https://a/1")["name"], "Summer-001.jpg")
            source_order.reset(str(manifest), "B")
            self.assertEqual(source_order.assign("https://b/1"), 1)  # not 1501


class FolderNames(SmartCase):
    def test_site_suffix_is_gone_and_equal_titles_stay_apart(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler
        from cyberdrop_dl.crawlers.turbovid import TurboVidCrawler

        bunkr, saved = _crawler(BunkrCrawler, self.root)
        turbo, saved_t = _crawler(TurboVidCrawler, self.root)
        album = self.item("https://bunkr.cr/a/AAA")
        album.setup_as_album(bunkr.create_title("allecakes", "AAA"), album_id="AAA")
        other = self.item("https://bunkr.cr/a/BBB")
        other.setup_as_album(bunkr.create_title("All3C@kes", "BBB"), album_id="BBB")
        video = self.item("https://turbovid.cr/embed/x")
        video.setup_as_album(turbo.create_title("allecakes"))
        self.save(bunkr, next(album.create_children([URL("https://bunkr.cr/f/1")])), "https://cdn.bunkr.ru/a9c82fa8.jpg", "a9c82fa8.jpg")
        self.save(bunkr, next(other.create_children([URL("https://bunkr.cr/f/2")])), "https://cdn.bunkr.ru/ab8273fd.jpg", "ab8273fd.jpg")
        self.save(turbo, video, "https://turbovid.cr/v/f82a9391.mp4", "f82a9391.mp4")
        self.save(bunkr, next(album.create_children([URL("https://bunkr.cr/f/3")])), "https://cdn.bunkr.ru/9af7283c.jpg", "9af7283c.jpg")
        self.assertEqual([self.rel(m) for m in saved], ["allecakes", "All3C@kes", "allecakes"])
        self.assertEqual(self.rel(saved_t[0]), "allecakes (2)")  # same title, different collection

    def test_folder_assignments_survive_a_restart(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        registry = self.root / "registry.jsonl"
        smart_naming.REGISTRY.load(registry, self.root)
        crawler, saved = _crawler(BunkrCrawler, self.root)
        for album_id in ("B2", "B1"):
            album = self.item(f"https://bunkr.cr/a/{album_id}")
            album.setup_as_album(crawler.create_title("cake", album_id), album_id=album_id)
            self.save(crawler, next(album.create_children([URL(f"https://bunkr.cr/f/{album_id}")])), f"https://c/{album_id}.jpg", f"{album_id}x9f8e7d6.jpg")
        self.assertEqual([self.rel(m) for m in saved], ["cake", "cake (2)"])
        smart_naming.REGISTRY.load(registry, self.root)  # NexDrop restarted
        album = self.item("https://bunkr.cr/a/B1")
        album.setup_as_album(crawler.create_title("cake", "B1"), album_id="B1")
        self.save(crawler, next(album.create_children([URL("https://bunkr.cr/f/again")])), "https://c/again.jpg", "a1b2c3d4.jpg")
        self.assertEqual(self.rel(saved[-1]), "cake (2)")

    def test_a_folder_already_on_disk_is_not_merged_into(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        (self.root / "cake").mkdir()
        crawler, saved = _crawler(BunkrCrawler, self.root)
        album = self.item("https://bunkr.cr/a/C1")
        album.setup_as_album(crawler.create_title("cake", "C1"), album_id="C1")
        self.save(crawler, next(album.create_children([URL("https://bunkr.cr/f/c")])), "https://c/c.jpg", "a1b2c3d4.jpg")
        self.assertEqual(self.rel(saved[0]), "cake (2)")

    def _linked_file(self, **payload):
        token = smart_naming.SETTINGS.set(NamingSettings.from_payload(payload))
        try:
            return self._save_linked_file()
        finally:
            smart_naming.SETTINGS.reset(token)

    def _save_linked_file(self):
        from cyberdrop_dl.crawlers.cyberdrop import CyberdropCrawler
        from cyberdrop_dl.crawlers.xenforo.simpcity import SimpCityCrawler

        forum, _ = _crawler(SimpCityCrawler, self.root)
        host, saved = _crawler(CyberdropCrawler, self.root)
        thread = self.item("https://simpcity.cr/threads/model-abc.123/")
        thread.setup_as_forum("")
        thread.append_folders(forum.create_title("Model ABC - Photoshoot Collection", thread_id=123))
        post = thread.create_new(thread.url, add_parent=URL("https://simpcity.cr/threads/model-abc.123/post-9"))
        post.setup_as_post("")
        post.append_folders(forum.create_separate_post_title(None, "9", None, caption="Summer Photoshoot 2026"))
        link = post.create_child(URL("https://cyberdrop.cr/f/abc"))
        link.reset()  # handle_external_links
        self.save(host, link, "https://k1.cyberdrop.cr/ab8293.jpg", "ab8293.jpg")
        self.assertEqual(saved[0].filename, "Summer Photoshoot 2026-001.jpg")
        return self.rel(saved[0])

    def test_files_linked_from_a_thread_go_to_their_posts_title_folder(self):
        self.assertEqual(self._linked_file(), "Model ABC - Photoshoot Collection/Summer Photoshoot 2026")

    def test_files_linked_from_a_thread_never_get_a_loose_files_folder(self):
        self.assertEqual(self._linked_file(titleFolders=False, contextFolders=False), "Model ABC - Photoshoot Collection")

    def test_replace_generic_folders_puts_linked_files_in_the_thread_folder(self):
        self.assertEqual(self._linked_file(replaceGenericFolders=True, titleFolders=False, contextFolders=False),
                         "Model ABC - Photoshoot Collection")

    def test_folder_for_each_post_works_without_title_folders(self):
        self.assertEqual(self._linked_file(titleFolders=False), "Model ABC - Photoshoot Collection/Summer Photoshoot 2026")

    def test_a_file_with_no_title_anywhere_goes_to_the_download_folder(self):
        from cyberdrop_dl.crawlers.gofile import GoFileCrawler

        crawler, saved = _crawler(GoFileCrawler, self.root)
        self.save(crawler, self.item("https://gofile.io/d/zzz"), "https://store1.gofile.io/9af7283c.zip", "9af7283c.zip")
        self.assertEqual(self.rel(saved[0]), ".")
        self.assertEqual(saved[0].filename, "9af7283c-001.zip")

    def test_site_name_and_original_structure_are_options(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        token = smart_naming.SETTINGS.set(NamingSettings.from_payload({"siteInFolder": True, "folderNaming": "original"}))
        try:
            crawler, saved = _crawler(BunkrCrawler, self.root, site_in_folder=True)
            album = self.item("https://bunkr.cr/a/S")
            album.setup_as_album(crawler.create_title("cake", "S"), album_id="S")
            self.save(crawler, next(album.create_children([URL("https://bunkr.cr/f/s")])), "https://c/s.jpg", "a1b2c3d4.jpg")
            self.save(crawler, self.item("https://bunkr.cr/f/loose"), "https://c/l.jpg", "e5f6a7b8.jpg")
        finally:
            smart_naming.SETTINGS.reset(token)
        self.assertEqual([self.rel(m) for m in saved], ["cake (Bunkr)", "."])

    def test_windows_unsafe_folder_names(self):
        self.assertEqual(smart_naming.windows_safe("CON"), "CON_")
        self.assertEqual(smart_naming.windows_safe("aux.txt"), "aux.txt_")
        self.assertEqual(smart_naming.windows_safe('a<b>:c"d|e?*. '), "a-b--c-d-e--")
        self.assertEqual(smart_naming.windows_safe("..."), "Untitled")


class FileNames(SmartCase):
    def test_album_title_names_hash_files_and_keeps_real_names(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        crawler, saved = _crawler(BunkrCrawler, self.root)
        album = self.item("https://bunkr.cr/a/S1")
        album.setup_as_album(crawler.create_title("Summer Vacation Photoshoot", "S1"), album_id="S1")
        pages = list(album.create_children(URL(f"https://bunkr.cr/f/{n}") for n in "abcd"))
        self.save(crawler, pages[0], "https://c/ab8273fa.jpg", "ab8273fa.jpg")
        self.save(crawler, pages[1], "https://c/9c827aa1.jpg", "9c827aa1.jpg")
        self.save(crawler, pages[2], "https://c/7a8291cc.mp4", "7a8291cc.mp4")
        self.save(crawler, pages[3], "https://c/Beach day.jpg", "Beach day.jpg")
        self.assertEqual(
            [m.filename for m in saved],
            ["Summer Vacation Photoshoot-001.jpg", "Summer Vacation Photoshoot-002.jpg",
             "Summer Vacation Photoshoot-003.mp4", "Beach day-004.jpg"],
        )
        self.assertEqual(saved[0].original_filename, "ab8273fa.jpg")

    def test_social_posts_use_their_caption_and_number_across_posts(self):
        from cyberdrop_dl.crawlers.twitter.crawler import TwitterCrawler

        crawler, saved = _crawler(TwitterCrawler, self.root)
        token = source_order.POST_FOLDERS.set(False)  # "Folder for each post" off
        self.addCleanup(source_order.POST_FOLDERS.reset, token)
        profile = self.item("https://x.com/allecakes")
        for post_id, caption, files in (
            ("1", "New Summer Photoshoot https://t.co/x #summer", ["GaB12xYW0AAbc9z.jpg", "GaB12xYW0AAbc8y.jpg"]),
            ("2", "@fan Beach day 🌊", ["GbC34zXV1BBcd0a.mp4"]),
            ("3", "", ["GcD56aYW2CCde1b.jpg"]),
        ):
            tweet = profile.create_child(URL(f"https://x.com/allecakes/status/{post_id}"))
            tweet.setup_as_album(crawler.create_title("@allecakes"))
            tweet.append_folders(crawler.create_separate_post_title(None, post_id, None, caption=caption))
            for name in files:
                media = tweet.create_child(URL(f"https://pbs.twimg.com/media/{name}"))
                self.save(crawler, media, f"https://pbs.twimg.com/media/{name}", name)
        self.assertEqual(
            [m.filename for m in saved],
            ["New Summer Photoshoot-001.jpg", "New Summer Photoshoot-002.jpg", "Beach day-003.mp4", "@allecakes-004.jpg"],
        )
        # "Folder for each title": each caption gets its folder; the account's own name stays in the account folder
        self.assertEqual([self.rel(m) for m in saved], ["@allecakes/New Summer Photoshoot", "@allecakes/New Summer Photoshoot",
                                                        "@allecakes/Beach day", "@allecakes"])

    def test_a_post_without_text_does_not_borrow_the_previous_caption(self):
        from cyberdrop_dl.crawlers.xenforo.simpcity import SimpCityCrawler

        crawler, saved = _crawler(SimpCityCrawler, self.root)
        thread = self.item("https://simpcity.cr/threads/t.5/")
        thread.setup_as_forum("")
        thread.append_folders(crawler.create_title("Thread Title Here", thread_id=5))
        for post_id, caption in (("1", "Summer Photoshoot 2026"), ("2", "")):
            post = thread.create_new(thread.url)
            post.setup_as_post("")
            post.append_folders(crawler.create_separate_post_title(None, post_id, None, caption=caption))
            self.save(crawler, post.create_child(URL(f"https://simpcity.cr/attachments/{post_id}")),
                      f"https://simpcity.cr/data/{post_id}.jpg", f"ab82{post_id}3.jpg")
        self.assertEqual([m.filename for m in saved], ["Summer Photoshoot 2026-001.jpg", "Thread Title Here-002.jpg"])

    def test_numbers_follow_source_order_not_finish_order(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        crawler, saved = _crawler(BunkrCrawler, self.root)
        album = self.item("https://bunkr.cr/a/O")
        album.setup_as_album(crawler.create_title("Order", "O"), album_id="O")
        pages = list(album.create_children(URL(f"https://bunkr.cr/f/{n}") for n in range(5)))
        for n in range(5):  # numbers are reserved when the page lists the file
            source_order.assign(f"https://c/{n}.jpg")

        async def finish_backwards():
            for n in reversed(range(5)):
                await crawler.handle_file(URL(f"https://c/{n}.jpg"), pages[n], f"a{n}b2c3d4.jpg", ".jpg")

        asyncio.run(finish_backwards())
        self.assertEqual(sorted(m.filename for m in saved), [f"Order-00{n}.jpg" for n in range(1, 6)])
        self.assertEqual(saved[0].filename, "Order-005.jpg")
        # A retry of the same URL keeps its name.
        asyncio.run(crawler.handle_file(URL("https://c/0.jpg"), pages[0], "a0b2c3d4.jpg", ".jpg"))
        self.assertEqual(saved[-1].filename, "Order-001.jpg")

    def test_archives_keep_real_names_compound_extensions_and_parts(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        crawler, saved = _crawler(BunkrCrawler, self.root)
        album = self.item("https://bunkr.cr/a/Z")
        album.setup_as_album(crawler.create_title("Collection", "Z"), album_id="Z")
        for name in ("9af7283c.tar.gz", "Project Files v2.zip", "x9f8a7b6.part1.rar", "x9f8a7b6.part2.rar"):
            self.save(crawler, next(album.create_children([URL(f"https://bunkr.cr/f/{name}")])), f"https://c/{name}", name)
        self.assertEqual(
            [m.filename for m in saved],
            ["Collection-001.tar.gz", "Project Files v2-002.zip", "x9f8a7b6.part1.rar", "x9f8a7b6.part2.rar"],
        )

    def test_every_file_type_and_unicode(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        crawler, saved = _crawler(BunkrCrawler, self.root)
        album = self.item("https://bunkr.cr/a/U")
        album.setup_as_album(crawler.create_title("夏の写真 Collection", "U"), album_id="U")
        exts = ("jpg", "png", "webp", "gif", "avif", "mkv", "mov", "webm", "7z", "mp3", "flac", "m4a", "pdf", "epub", "txt")
        for ext in exts:
            self.save(crawler, next(album.create_children([URL(f"https://bunkr.cr/f/{ext}")])), f"https://c/a1b2c3d4.{ext}", f"a1b2c3d4.{ext}")
        self.assertEqual([m.filename for m in saved], [f"夏の写真 Collection-{i:03d}.{e}" for i, e in enumerate(exts, 1)])

    def test_long_titles_and_unsafe_characters(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        crawler, saved = _crawler(BunkrCrawler, self.root)
        album = self.item("https://bunkr.cr/a/L")
        album.setup_as_album(crawler.create_title('CON: "one" / two? ' + "word " * 60, "L"), album_id="L")
        self.save(crawler, next(album.create_children([URL("https://bunkr.cr/f/l")])), "https://c/l.jpg", "a1b2c3d4.jpg")
        name = saved[0].filename
        self.assertLessEqual(len(Path(name).stem), 120)
        self.assertTrue(name.endswith("-001.jpg"))
        self.assertFalse(set(name) & set('<>:"/\\|?*'), name)

    def test_strategies_and_switches(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        def run(**payload):
            source_order.reset()
            token = smart_naming.SETTINGS.set(NamingSettings.from_payload(payload))
            try:
                crawler, saved = _crawler(BunkrCrawler, self.root)
                album = self.item("https://bunkr.cr/a/Q")
                album.setup_as_album(crawler.create_title("Album Title", "Q"), album_id="Q")
                self.save(crawler, next(album.create_children([URL("https://bunkr.cr/f/q")])), "https://c/q.jpg", "Beach day.jpg")
                self.save(crawler, next(album.create_children([URL("https://bunkr.cr/f/r")])), "https://c/r.jpg", "a1b2c3d4.jpg")
                return [m.filename for m in saved]
            finally:
                smart_naming.SETTINGS.reset(token)

        self.assertEqual(run(), ["Beach day-001.jpg", "Album Title-002.jpg"])
        self.assertEqual(run(nameStrategy="original"), ["Beach day-001.jpg", "a1b2c3d4-002.jpg"])
        self.assertEqual(run(nameStrategy="album"), ["Album Title-001.jpg", "Album Title-002.jpg"])
        self.assertEqual(run(keepMeaningfulNames=False), ["Album Title-001.jpg", "Album Title-002.jpg"])
        self.assertEqual(run(numbering="prefix"), ["001_Beach day.jpg", "002_Album Title.jpg"])
        self.assertEqual(run(numbering="off"), ["Beach day.jpg", "Album Title.jpg"])
        self.assertEqual(run(smartNames=False), ["Beach day-001.jpg", "a1b2c3d4-002.jpg"])

    def test_resumed_job_restores_names_even_if_metadata_changed(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        manifest = self.root / "manifest.jsonl"
        fingerprint = self.settings.fingerprint()
        row = {"url": "https://c/x.jpg", "sequence": 7, "name": "Old Title-007.jpg", "job": "J", "naming": fingerprint}
        manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
        source_order.reset(str(manifest), "J")
        crawler, saved = _crawler(BunkrCrawler, self.root)
        album = self.item("https://bunkr.cr/a/N")
        album.setup_as_album(crawler.create_title("New Title", "N"), album_id="N")
        self.save(crawler, next(album.create_children([URL("https://bunkr.cr/f/x")])), "https://c/x.jpg", "a1b2c3d4.jpg")
        self.save(crawler, next(album.create_children([URL("https://bunkr.cr/f/y")])), "https://c/y.jpg", "e5f6a7b8.jpg")
        self.assertEqual([m.filename for m in saved], ["Old Title-007.jpg", "New Title-008.jpg"])

    def test_changed_settings_make_new_names(self):
        manifest = self.root / "manifest.jsonl"
        row = {"url": "https://c/x.jpg", "sequence": 1, "name": "001_old.jpg", "job": "J", "naming": "0.auto.prefix.1.120"}
        manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
        source_order.reset(str(manifest), "J")
        decision = smart_naming.name_file("https://c/x.jpg", "Beach day.jpg", ".jpg", {}, "https://c/p", self.root, lambda n: n)
        self.assertEqual(decision.filename, "Beach day-001.jpg")

    def test_large_job_is_fast(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler

        crawler, saved = _crawler(BunkrCrawler, self.root)
        album = self.item("https://bunkr.cr/a/BIG")
        album.setup_as_album(crawler.create_title("Big Album", "BIG"), album_id="BIG")
        pages = list(album.create_children(URL(f"https://bunkr.cr/f/{n}") for n in range(3000)))

        async def many():
            for n, page in enumerate(pages):
                await crawler.handle_file(URL(f"https://c/{n}.jpg"), page, f"{n:08x}.jpg", ".jpg")

        started = time.perf_counter()
        asyncio.run(many())
        seconds = time.perf_counter() - started
        self.assertEqual(len({m.filename for m in saved}), 3000)
        self.assertEqual(saved[-1].filename, "Big Album-3000.jpg")
        self.assertLess(seconds, 10, f"{seconds:.2f}s for 3000 files")


class UnfinishedDownloads(unittest.TestCase):
    def test_a_partial_file_keeps_its_old_folder_and_name(self):
        from cyberdrop_dl.clients.downloads import DownloadClient
        from cyberdrop_dl.url_objects import MediaItem

        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "allecakes (Bunkr)"
            old.mkdir()
            (old / "001_a9c82fa8.jpg.part").write_bytes(b"x" * 10)

            async def location(domain, media_item):
                return str(old), "001_a9c82fa8.jpg"

            client = object.__new__(DownloadClient)
            client.manager = types.SimpleNamespace(
                database=types.SimpleNamespace(history=types.SimpleNamespace(get_unfinished_location=location))
            )
            item = MediaItem(url=URL("https://c/a9c82fa8.jpg"), domain="bunkr", referer=URL("https://bunkr.cr/f/a"),
                             download_folder=Path(tmp) / "allecakes", db_path="/a9c82fa8.jpg", filename="allecakes-001.jpg")
            asyncio.run(client._keep_unfinished_location(item, "bunkr"))
            self.assertEqual(item.download_folder / item.filename, old / "001_a9c82fa8.jpg")
            # No partial file: the new name is used.
            (old / "001_a9c82fa8.jpg.part").unlink()
            item.download_folder, item.filename = Path(tmp) / "allecakes", "allecakes-001.jpg"
            asyncio.run(client._keep_unfinished_location(item, "bunkr"))
            self.assertEqual(item.filename, "allecakes-001.jpg")


class DesktopSession(unittest.TestCase):
    def test_job_key_and_numbering_setting(self):
        import cairn_session as session

        a = session.job_key(["https://x.com/a", "https://bunkr.cr/a/1"])
        self.assertEqual(a, session.job_key(["https://bunkr.cr/a/1/", "https://x.com/a"]))
        self.assertNotEqual(a, session.job_key(["https://x.com/a"]))
        self.assertEqual(session._numbering({}), "suffix")
        self.assertEqual(session._numbering({"sequencePrefix": False}), "off")
        self.assertEqual(session._numbering({"numbering": "prefix", "sequencePrefix": False}), "prefix")

    def test_old_job_database_gets_naming_columns(self):
        from job_store import JobStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.sqlite"
            old = sqlite3.connect(path)
            old.executescript("""CREATE TABLE jobs (id INTEGER PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                status TEXT NOT NULL, source_urls TEXT NOT NULL, download_dir TEXT NOT NULL, stop_reason TEXT NOT NULL DEFAULT '');
                CREATE TABLE items (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, url TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
                sequence INTEGER NOT NULL DEFAULT 0, filename TEXT NOT NULL DEFAULT '', destination TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued', bytes_read INTEGER NOT NULL DEFAULT 0, bytes_total INTEGER NOT NULL DEFAULT 0,
                retries INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL, UNIQUE (job_id, url));
                INSERT INTO jobs VALUES (1, 0, 0, 'completed', 'https://old', 'd', '');
                INSERT INTO items(job_id, url, filename, status, updated_at) VALUES (1, 'https://old/f', '001_f.jpg', 'completed', 0);""")
            old.commit()
            old.close()
            store = JobStore(path)
            job = store.start_job(["https://x"], "d")
            store.note(job, {"url": "https://x/1", "event": "success", "sequence": 1, "name": "Beach-001.jpg",
                             "naming": {"original": "a9c82fa8.jpg", "folder": "@x", "source": "caption", "confidence": "0.75",
                                        "strategy": "auto", "title": "Beach", "parent": "@x"}})
            row = store.conn.execute("SELECT original_name, folder, name_source, name_confidence FROM items WHERE url = 'https://x/1'").fetchone()
            self.assertEqual(tuple(row), ("a9c82fa8.jpg", "@x", "caption", 0.75))
            legacy = store.conn.execute("SELECT filename, original_name FROM items WHERE url = 'https://old/f'").fetchone()
            self.assertEqual(tuple(legacy), ("001_f.jpg", ""))
            store.close()


if __name__ == "__main__":
    unittest.main()
