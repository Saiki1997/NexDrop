"""Page order (3.1.4): file numbers follow the post's layout, and Page order finishes one post before the next.

Checked offline: a stubbed SimpCity post whose images sit on another site that answers in reverse order.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))

from bs4 import BeautifulSoup  # noqa: E402

from cyberdrop_dl import smart_naming, source_order  # noqa: E402
from cyberdrop_dl.aio import TaskManager  # noqa: E402
from cyberdrop_dl.smart_naming import NamingSettings  # noqa: E402
from cyberdrop_dl.url_objects import AbsoluteHttpURL, ScrapeItem  # noqa: E402

URL = AbsoluteHttpURL


class Slots(unittest.TestCase):
    def setUp(self):
        source_order.reset()

    def test_a_late_file_takes_the_number_its_link_had_on_the_page(self):
        slots = [source_order.reserve(f"https://host/i/{n}") for n in range(3)]
        self.assertEqual(slots, [1, 2, 3])
        # The third link answers first.
        self.assertEqual(source_order.assign("https://cdn/2.jpg", slots[2]), 3)
        self.assertEqual(source_order.assign("https://cdn/0.jpg", slots[0]), 1)
        self.assertEqual(source_order.assign("https://cdn/1.jpg", slots[1]), 2)

    def test_more_files_under_one_link_count_on_from_the_end(self):
        album = source_order.reserve("https://host/a/album")
        single = source_order.reserve("https://host/i/one")
        self.assertEqual(source_order.assign("https://cdn/a1.jpg", album), 1)
        self.assertEqual(source_order.assign("https://cdn/a2.jpg", album), 3)
        self.assertEqual(source_order.assign("https://cdn/one.jpg", single), 2)

    def test_same_link_and_same_file_keep_their_numbers(self):
        self.assertEqual(source_order.reserve("https://host/i/x"), source_order.reserve("https://host/i/x"))
        first = source_order.assign("https://cdn/x.jpg", 1)
        self.assertEqual(source_order.assign("https://cdn/x.jpg", 7), first)

    def test_a_file_known_from_an_earlier_run_keeps_its_number(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "order.jsonl"
            manifest.write_text('{"url": "https://cdn/b.jpg", "sequence": 1, "job": "j"}\n', encoding="utf-8")
            source_order.reset(str(manifest), "j")
            slot = source_order.reserve("https://host/i/a")
            self.assertEqual(source_order.assign("https://cdn/a.jpg", slot), 2)
            # Slot 1 is taken by b.jpg, so a new file that was reserved 1 elsewhere moves to the end.
            self.assertEqual(source_order.assign("https://cdn/c.jpg", 1), 3)


class _Post:
    def __init__(self, post_id: int) -> None:
        self.id = post_id
        self.date = None
        self.content = BeautifulSoup("<div></div>", "html.parser").div
        self.article = self.content


def _stub(cls, root: Path, saved: list, task_mngr: TaskManager):
    crawler = object.__new__(cls)

    async def handle_media_item(media_item, m3u8=None):
        saved.append(media_item)

    crawler.handle_media_item = handle_media_item
    crawler._prepare_headers = lambda item: {}
    crawler._task_mngr = task_mngr
    crawler.config = types.SimpleNamespace(
        download_folder=root,
        filters=types.SimpleNamespace(files=types.SimpleNamespace(thumbnails=False)),
        subfolders=types.SimpleNamespace(
            include=types.SimpleNamespace(album_id=False, domain=False, thread_id=False),
            separate_posts=types.SimpleNamespace(enabled=False, format="{default}"),
        ),
    )
    return crawler


class ForumPosts(unittest.TestCase):
    """Two posts. Each has a forum attachment, then three images on another site.

    The other site answers the last image first, and each download takes a moment, so without
    reserved numbers and the post gate the names and the finish order come out shuffled."""

    settings = NamingSettings.from_payload({"smartNames": False})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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

    def run_posts(self, mode: str) -> tuple[list[str], list[str], list[str]]:
        from cyberdrop_dl.crawlers.cyberdrop import CyberdropCrawler
        from cyberdrop_dl.crawlers.xenforo.simpcity import SimpCityCrawler

        saved: list = []
        finished: list[str] = []
        errors: list = []

        async def main() -> None:
            tasks = TaskManager()
            async with tasks.scrape, tasks.downloads:
                forum = _stub(SimpCityCrawler, self.root, saved, tasks)
                host = _stub(CyberdropCrawler, self.root, saved, tasks)

                async def download(media_item) -> None:
                    await asyncio.sleep(0.01)
                    finished.append(media_item.filename)

                async def queue(media_item, m3u8=None):
                    saved.append(media_item)
                    tasks.downloads.create_task(download(media_item))

                forum.handle_media_item = host.handle_media_item = queue

                async def send_to_crawler(item: ScrapeItem) -> None:
                    name = item.url.name
                    await asyncio.sleep(0.005 * (4 - int(name[-1])))  # image 3 answers first
                    item.reset()
                    tasks.scrape.create_task(host.handle_file(URL(f"https://k1.cyberdrop.cr/{name}.jpg"), item, f"{name}.jpg", ".jpg"))

                logs = types.SimpleNamespace(write_scrape_error=lambda *error: errors.append(error))
                manager = types.SimpleNamespace(scrape_mapper=types.SimpleNamespace(send_to_crawler=send_to_crawler, logs=logs))
                forum.manager = host.manager = manager
                thread = ScrapeItem(url=URL("https://simpcity.cr/threads/t.5/"), download_folder=self.root)
                thread.setup_as_forum("")
                thread.append_folders(forum.create_title("Thread", thread_id=5))
                token = source_order.ORDER_MODE.set(mode)
                try:
                    for post_id in (1, 2):
                        links = [f"https://simpcity.cr/data/attachments/p{post_id}-0.jpg"]
                        links += [f"https://cyberdrop.cr/f/p{post_id}-{n}" for n in (1, 2, 3)]
                        forum._collect_post_urls = lambda post, links=links: links
                        forum._embedded_urls = lambda post: set()
                        await forum.post(thread.create_new(thread.url), _Post(post_id))
                finally:
                    source_order.ORDER_MODE.reset(token)

        asyncio.run(main())
        self.assertEqual(errors, [])
        names = sorted(m.filename for m in saved)
        stems = [name.rsplit("-", 1)[0] for name in sorted(names, key=lambda name: name.rsplit("-", 1)[1])]
        return stems, names, finished

    PAGE = ["p1-0", "p1-1", "p1-2", "p1-3", "p2-0", "p2-1", "p2-2", "p2-3"]

    def test_numbers_follow_the_post_layout_in_parallel(self):
        stems, names, _ = self.run_posts("parallel")
        self.assertEqual(stems, self.PAGE)
        self.assertEqual(sorted(name.rsplit("-", 1)[1] for name in names), [f"00{n}.jpg" for n in range(1, 9)])

    def test_page_order_finishes_each_post_before_the_next(self):
        stems, _, finished = self.run_posts("source")
        self.assertEqual(stems, self.PAGE)
        self.assertEqual(len(finished), 8)
        self.assertTrue(all(name.startswith("p1-") for name in finished[:4]), finished)
        self.assertTrue(all(name.startswith("p2-") for name in finished[4:]), finished)


if __name__ == "__main__":
    unittest.main()
