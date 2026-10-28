"""3.1.1: dates are taken out of file and folder names, and every file of a post or album gets that
post's or album's folder, also when it keeps its own name."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "tests"))

from cyberdrop_dl import smart_naming  # noqa: E402
from cyberdrop_dl.name_dates import strip_dates  # noqa: E402
from cyberdrop_dl.smart_naming import NamingSettings  # noqa: E402
from test_smart_naming import URL, SmartCase, _crawler  # noqa: E402


class StripDates(unittest.TestCase):
    def test_whole_dates_are_removed(self):
        cases = {
            "2021-06-10-PIYO-118-I-Want-My-Step-Dads-Baby": "PIYO-118-I-Want-My-Step-Dads-Baby",
            "2020 06 11 [EMOI-014] Shy, Emotional Girl": "[EMOI-014] Shy, Emotional Girl",
            "(2022-07-12) - (1080p)-841": "(1080p)-841",
            "Beach - 2021.07.02 - Day 2": "Beach - Day 2",
            "[2021.11.11] [SUN-035] Exhibitionist": "[SUN-035] Exhibitionist",
            "My Video 02.07.2021": "My Video",
            "2021_07_02 Pool": "Pool",
        }
        for before, after in cases.items():
            self.assertEqual(strip_dates(before), after, before)

    def test_other_numbers_stay(self):
        for name in ("IMG_20210702_1234", "2021-13-02 not a date", "PIYO-118 1080p", "Set 2021", "20210702"):
            self.assertEqual(strip_dates(name), name)

    def test_a_name_that_is_only_a_date_is_kept(self):
        self.assertEqual(strip_dates("2021-07-02"), "2021-07-02")
        self.assertEqual(strip_dates("(2021-07-02)"), "(2021-07-02)")


class InARun(SmartCase):
    def _post_files(self, caption: str, names: tuple[str, ...], **payload):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler
        from cyberdrop_dl.crawlers.xenforo.simpcity import SimpCityCrawler

        token = smart_naming.SETTINGS.set(NamingSettings.from_payload(payload))
        try:
            forum, _ = _crawler(SimpCityCrawler, self.root)
            host, saved = _crawler(BunkrCrawler, self.root)
            thread = self.item("https://simpcity.cr/threads/jav.123/")
            thread.setup_as_forum("")
            thread.append_folders(forum.create_title("JAV", thread_id=123))
            post = thread.create_new(thread.url, add_parent=URL("https://simpcity.cr/threads/jav.123/post-9"))
            post.setup_as_post("")
            post.append_folders(forum.create_separate_post_title(None, "9", None, caption=caption))
            for n, name in enumerate(names, 1):
                link = post.create_child(URL(f"https://bunkr.cr/f/{n}"))
                link.reset()  # handle_external_links
                self.save(host, link, f"https://cdn.bunkr.ru/{name}", name)
            return saved
        finally:
            smart_naming.SETTINGS.reset(token)

    def test_files_of_one_post_share_its_folder_and_lose_their_dates(self):
        saved = self._post_files("2021.06.10 [PIYO-118] I Want My Step Dads Baby",
                                 ("2021-06-10-PIYO-118-I-Want-My-Step-Dads-Baby.mp4", "0a9c82fa8.jpg"))
        folders = {m.download_folder.relative_to(self.root).as_posix() for m in saved}
        self.assertEqual(folders, {"JAV/[PIYO-118] I Want My Step Dads Baby"})
        names = [m.filename for m in saved]
        self.assertEqual(names[0], "PIYO-118-I-Want-My-Step-Dads-Baby-001.mp4")
        self.assertFalse(any("2021" in name for name in names), names)

    def test_both_settings_off_keep_the_old_behaviour(self):
        saved = self._post_files("2021.06.10 [PIYO-118] I Want My Step Dads Baby",
                                 ("2021-06-10-PIYO-118-I-Want-My-Step-Dads-Baby.mp4",),
                                 contextFolders=False, stripDates=False)
        self.assertEqual(saved[0].filename, "2021-06-10-PIYO-118-I-Want-My-Step-Dads-Baby-001.mp4")
        self.assertEqual(saved[0].download_folder.relative_to(self.root).as_posix(), "JAV")


    def test_a_post_without_a_usable_title_still_gets_its_own_folder(self):
        # 3.1.2: these files stayed loose in the thread folder ("Discussion Thread-003.jpg")
        for caption in ("", None, "Discussion Thread", "thanks!", "https://bunkr.cr/a/xyz"):
            saved = self._post_files(caption, ("IMG_1234.jpg", "a8f9c0d1e2.jpg", "video.mp4"))
            folders = {m.download_folder.relative_to(self.root).as_posix() for m in saved}
            self.assertEqual(folders, {"JAV/Post 9"}, caption)

    def test_download_prompts_sizes_and_extensions_leave_the_title(self):
        saved = self._post_files("Download @@PIYO-116 Some Title.mp4 (1.2 GB)", ("a8f9c0d1e2.jpg", "video.mp4"))
        self.assertEqual({m.download_folder.relative_to(self.root).as_posix() for m in saved}, {"JAV/PIYO-116 Some Title"})
        self.assertEqual(saved[0].filename, "PIYO-116 Some Title-001.jpg")

    def test_no_second_folder_when_posts_already_have_folders(self):
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler
        from cyberdrop_dl.crawlers.xenforo.simpcity import SimpCityCrawler

        forum, _ = _crawler(SimpCityCrawler, self.root, separate_posts=True)
        host, saved = _crawler(BunkrCrawler, self.root)
        thread = self.item("https://simpcity.cr/threads/jav.123/")
        thread.setup_as_forum("")
        thread.append_folders(forum.create_title("JAV", thread_id=123))
        post = thread.create_new(thread.url, add_parent=URL("https://simpcity.cr/threads/jav.123/post-9"))
        post.setup_as_post("")
        post.append_folders(forum.create_separate_post_title(None, "9", None, caption=""))
        link = post.create_child(URL("https://bunkr.cr/f/1"))
        link.reset()
        self.save(host, link, "https://cdn.bunkr.ru/a8f9c0d1e2.jpg", "a8f9c0d1e2.jpg")
        self.assertEqual(saved[0].download_folder.parent.relative_to(self.root).as_posix(), "JAV")

    def test_post_folder_fallback_follows_the_setting(self):
        saved = self._post_files("", ("IMG_1234.jpg",), contextFolders=False)
        self.assertEqual(saved[0].download_folder.relative_to(self.root).as_posix(), "JAV")


if __name__ == "__main__":
    unittest.main()
