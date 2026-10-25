"""Kemono and Coomer profile links (2026-10-25 22:43 and 22:45 runs: "Unsupported URL")."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("CDL_APPDATA_FOLDER", tempfile.mkdtemp(prefix="cdl-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "vendor"))
from cyberdrop_dl.crawlers.kemono.api import CreatorEndpoint, KemonoAPI
from cyberdrop_dl.crawlers.kemono.kemono import KemonoBaseCrawler
from cyberdrop_dl.crawlers.kemono.kemono_coomer import (
    CoomerAPI, CoomerCrawler, KemonoCrawler, KemonoPartyAPI,
)
from cyberdrop_dl.crawlers.kemono.models import File, UserPostModel
from cyberdrop_dl.scrape_mapper import _best_match, get_crawlers_mapping
from cyberdrop_dl.url_objects import AbsoluteHttpURL

COOMER_PROFILE = "https://coomer.st/onlyfans/user/bigtittygothegg"
KEMONO_PROFILE = "https://kemono.cr/patreon/user/8693043"

POST = {
    "id": "1001", "user": "bigtittygothegg", "service": "onlyfans", "title": "Hello",
    "content": "", "published": "2026-10-01T12:00:00",
    "file": {"name": "a.jpg", "path": "/7a/10/7a10.jpg"},
    "attachments": [{"name": "b.mp4", "path": "/aa/bb/aabb.mp4"}],
}


class KemonoCoomerTests(unittest.IsolatedAsyncioTestCase):
    def test_profile_links_have_a_handler(self):
        crawlers = get_crawlers_mapping()
        for url, cls in [
            (COOMER_PROFILE, CoomerCrawler),
            (KEMONO_PROFILE, KemonoCrawler),
            ("https://coomer.su/fansly/user/123/post/456", CoomerCrawler),
            ("https://kemono.su/fanbox/user/1", KemonoCrawler),
            ("https://n4.coomer.st/data/7a/10/7a10.jpg", CoomerCrawler),
        ]:
            with self.subTest(url=url):
                self.assertIs(_best_match(crawlers, AbsoluteHttpURL(url).host), cls)

    def test_old_hosts_move_to_the_main_domain(self):
        for url, new in [
            ("https://c6.coomer.party/data/7a/10/7a10.jpg?f=a.jpg", "https://coomer.st/data/7a/10/7a10.jpg?f=a.jpg"),
            ("https://coomer.su/onlyfans/user/x", "https://coomer.st/onlyfans/user/x"),
            ("https://kemono.party/patreon/user/1/post/2", "https://kemono.cr/patreon/user/1/post/2"),
            ("https://n4.coomer.st/data/7a/10/7a10.jpg", "https://n4.coomer.st/data/7a/10/7a10.jpg"),
            (COOMER_PROFILE, COOMER_PROFILE),
        ]:
            with self.subTest(url=url):
                crawler = CoomerCrawler if "coomer" in url else KemonoCrawler
                self.assertEqual(str(crawler.transform_url(AbsoluteHttpURL(url))), new)

    async def test_profile_link_goes_to_creator(self):
        for url, service, creator in [
            (COOMER_PROFILE, "onlyfans", "bigtittygothegg"),
            (KEMONO_PROFILE, "patreon", "8693043"),
        ]:
            with self.subTest(url=url):
                c = SimpleNamespace(creator=AsyncMock())
                item = SimpleNamespace(url=AbsoluteHttpURL(url))
                await KemonoBaseCrawler.fetch(c, item)
                c.creator.assert_awaited_once_with(item, service, creator)

    def test_api_asks_for_json_the_way_the_sites_require(self):
        for crawler, api, entry in [
            (CoomerCrawler, CoomerAPI, "https://coomer.st/api/v1"),
            (KemonoCrawler, KemonoPartyAPI, "https://kemono.cr/api/v1"),
        ]:
            with self.subTest(site=crawler.DOMAIN):
                config = crawler.__http_config__ | api.__http_config__
                self.assertEqual(config.headers["Accept"], "text/css")
                self.assertEqual(config.rate_limit, (2, 1))
                self.assertEqual(str(api.ENTRYPOINT), entry)

    async def test_creator_posts_endpoint_and_parsing(self):
        seen = []

        async def pager(url):
            seen.append(str(url))
            yield [dict(POST)]

        api = SimpleNamespace(ENTRYPOINT=CoomerAPI.ENTRYPOINT, VALID_QUERY_PARAMS=KemonoAPI.VALID_QUERY_PARAMS,
                              pager=pager, __post__=UserPostModel)
        endpoint = CreatorEndpoint.__new__(CreatorEndpoint)
        endpoint.api = api
        posts = [p async for page in endpoint.posts("onlyfans", "bigtittygothegg") for p in page]
        self.assertEqual(seen, ["https://coomer.st/api/v1/onlyfans/user/bigtittygothegg/posts"])
        self.assertEqual(posts[0].user_id, "bigtittygothegg")
        self.assertEqual([f.name for f in (posts[0].file, *posts[0].attachments)], ["a.jpg", "b.mp4"])

    def test_file_links_point_at_the_site(self):
        c = SimpleNamespace(api=SimpleNamespace(CDN=CoomerAPI.CDN), parse_url=CoomerCrawler.parse_url)
        url = KemonoBaseCrawler._compose_file_url(c, File(path="/7a/10/7a10.jpg", name="a.jpg"))
        self.assertEqual(str(url), "https://coomer.st/data/7a/10/7a10.jpg?f=a.jpg")

    async def test_no_creator_list_download_at_start(self):
        c = SimpleNamespace(api=SimpleNamespace(creators=AsyncMock()))
        await CoomerCrawler.__async_post_init__(c)
        c.api.creators.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
