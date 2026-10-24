"""Regression tests for Monotirou media resolution and cursor pagination."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "vendor"))
from cyberdrop_dl.crawlers.monotirou import MonotirouCrawler
from cyberdrop_dl.exceptions import ScrapeError
from cyberdrop_dl.url_objects import AbsoluteHttpURL


def row(number):
    return {"id": number, "public_id": f"public-{number}",
            "created_at": "2026-10-24T10:28:21.07476Z"}


def crawler(responses):
    tasks = []
    c = SimpleNamespace(
        API_USER_VIDEOS=MonotirouCrawler.API_USER_VIDEOS,
        API_VIDEO=MonotirouCrawler.API_VIDEO,
        API_SEARCH=MonotirouCrawler.API_SEARCH,
        PRIMARY_URL=MonotirouCrawler.PRIMARY_URL,
        request_json=AsyncMock(side_effect=responses),
        create_title=lambda value: value,
        run=AsyncMock(), direct_file=AsyncMock(),
        create_task=tasks.append, create_eager_task=tasks.append,
    )
    return c, tasks


class MonotirouTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_routes_real_urls(self):
        for path, method, identifier in [
            ("/users/nesa1", "profile", "nesa1"),
            ("/users/nesa1/", "profile", "nesa1"),
            ("/video/ExampleId?rank=1", "video", "ExampleId"),
            ("/video/ExampleId/", "video", "ExampleId"),
        ]:
            with self.subTest(path=path):
                c = SimpleNamespace(profile=AsyncMock(), video=AsyncMock())
                item = SimpleNamespace(url=AbsoluteHttpURL("https://monotirou.com" + path))
                await MonotirouCrawler.fetch(c, item)
                getattr(c, method).assert_awaited_once_with(item, identifier)

    async def test_fetch_routes_search_and_tag_urls(self):
        for url, query, title in [
            ("https://monotirou.com/?q=Melayu+Sexy", "Melayu Sexy", "Melayu Sexy [search]"),
            ("https://monotirou.com/search?q=abang", "abang", "abang [search]"),
            ("https://monotirou.com/tags/abang", "abang", "abang [tag]"),
            ("https://monotirou.com/tags/abang/", "abang", "abang [tag]"),
        ]:
            with self.subTest(url=url):
                c = SimpleNamespace(search=AsyncMock())
                item = SimpleNamespace(url=AbsoluteHttpURL(url))
                await MonotirouCrawler.fetch(c, item)
                c.search.assert_awaited_once_with(item, query, title)

    async def test_fetch_rejects_unsupported_paths(self):
        for path in ["/", "/?q=", "/users", "/video", "/tags", "/users/nesa1/extra"]:
            c = SimpleNamespace(profile=AsyncMock(), video=AsyncMock(), search=AsyncMock())
            item = SimpleNamespace(url=AbsoluteHttpURL("https://monotirou.com" + path))
            with self.assertRaises(ValueError):
                await MonotirouCrawler.fetch(c, item)
            c.profile.assert_not_awaited()
            c.video.assert_not_awaited()
            c.search.assert_not_awaited()

    async def finish(self, tasks):
        for task in tasks:
            await task

    async def test_cursor_advances_and_uses_public_ids(self):
        first = [row(i) for i in range(24)]
        c, tasks = crawler([{"data": first}, {"data": [row(24)]}])
        item = Mock()
        await MonotirouCrawler.profile.__wrapped__(c, item, "test")
        await self.finish(tasks)
        urls = [call.args[0] for call in c.request_json.call_args_list]
        self.assertNotIn("cursor", urls[0].query)
        self.assertNotIn("offset", urls[1].query)
        self.assertEqual(json.loads(urls[1].query["cursor"]),
                         {"v": first[-1]["created_at"], "id": 23})
        self.assertEqual(item.create_child.call_count, 25)
        self.assertEqual(str(item.create_child.call_args_list[0].args[0]),
                         "https://monotirou.com/video/public-0")

    async def test_repeated_page_stops_without_rescheduling(self):
        page = {"data": [row(i) for i in range(24)]}
        c, tasks = crawler([page, page])
        item = Mock()
        with self.assertRaises(ScrapeError):
            await MonotirouCrawler.profile.__wrapped__(c, item, "test")
        await self.finish(tasks)
        self.assertEqual(c.request_json.await_count, 2)
        self.assertEqual(item.create_child.call_count, 24)

    async def test_empty_and_invalid_listings(self):
        for response, error in [({"data": []}, False), ({"data": {}}, True),
                                ({"data": [{}]}, True)]:
            c, tasks = crawler([response])
            if error:
                with self.assertRaises(ScrapeError):
                    await MonotirouCrawler.profile.__wrapped__(c, Mock(), "test")
            else:
                await MonotirouCrawler.profile.__wrapped__(c, Mock(), "test")
            self.assertEqual(tasks, [])

    async def test_search_pages_by_offset_until_a_short_page(self):
        first = [row(i) for i in range(48)]
        c, tasks = crawler([{"data": first}, {"data": [row(48), row(49)]}])
        item = Mock()
        await MonotirouCrawler.search.__wrapped__(c, item, "Melayu Sexy", "Melayu Sexy [search]")
        await self.finish(tasks)
        urls = [call.args[0] for call in c.request_json.call_args_list]
        self.assertEqual([u.query["offset"] for u in urls], ["0", "48"])
        self.assertEqual(urls[0].query["q"], "Melayu Sexy")
        self.assertEqual(str(urls[0].with_query(None)), "https://api.monotirou.com/api/videos/search")
        self.assertEqual(item.create_child.call_count, 50)
        item.setup_as_profile.assert_called_once_with("Melayu Sexy [search]")

    async def test_search_repeated_or_invalid_page_stops(self):
        page = {"data": [row(i) for i in range(48)]}
        c, tasks = crawler([page, page])
        item = Mock()
        with self.assertRaises(ScrapeError):
            await MonotirouCrawler.search.__wrapped__(c, item, "abang", "abang [tag]")
        await self.finish(tasks)
        self.assertEqual(item.create_child.call_count, 48)
        for response in [{"data": {}}, {"data": [{}]}]:
            c, tasks = crawler([response])
            with self.assertRaises(ScrapeError):
                await MonotirouCrawler.search.__wrapped__(c, Mock(), "abang", "abang [tag]")
            self.assertEqual(tasks, [])

    async def test_media_url_comes_from_api(self):
        media = "https://cdn.example.test/actual-file.mp4?token=abc"
        c, tasks = crawler([{"data": {"public_id": "public-1",
                                      "title": "Example", "video_url": media}}])
        item = Mock()
        await MonotirouCrawler.video.__wrapped__(c, item, "1")
        await self.finish(tasks)
        self.assertEqual(str(c.direct_file.call_args.args[1]), media)
        item.setup_as_album.assert_called_once_with("Example", album_id="public-1")

    async def test_missing_media_or_api_failure_never_guesses_cdn(self):
        for response in [{"data": {"public_id": "public-1"}},
                         {"data": {"public_id": "public-1", "video_url": "/relative.mp4"}},
                         RuntimeError("API unavailable")]:
            c, tasks = crawler([response])
            with self.assertRaises((ScrapeError, RuntimeError)):
                await MonotirouCrawler.video.__wrapped__(c, Mock(), "1")
            self.assertEqual(tasks, [])
            c.direct_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
