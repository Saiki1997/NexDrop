"""GoonBox direct /images/ links and Pixeldrain captcha-locked files (from a real run log)."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "vendor"))
from cyberdrop_dl.crawlers.goonbox import GoonBoxCrawler
from cyberdrop_dl.crawlers.pixeldrain import File, PixelDrainCrawler
from cyberdrop_dl.exceptions import ScrapeError
from cyberdrop_dl.url_objects import AbsoluteHttpURL


class GoonBoxImages(unittest.IsolatedAsyncioTestCase):
    async def test_main_host_images_path_is_a_direct_file(self):
        url = AbsoluteHttpURL("https://goonbox.cr/images/10000255899b009913813dbc8095a3a96bd2da80ab.jpg")
        c = SimpleNamespace(is_subdomain=GoonBoxCrawler.is_subdomain, direct_file=AsyncMock())
        item = SimpleNamespace(url=url)
        await GoonBoxCrawler.fetch(c, item)
        c.direct_file.assert_awaited_once_with(item)

    async def test_images_folder_without_a_file_is_still_unknown(self):
        c = SimpleNamespace(is_subdomain=GoonBoxCrawler.is_subdomain, direct_file=AsyncMock())
        with self.assertRaises(ValueError):
            await GoonBoxCrawler.fetch(c, SimpleNamespace(url=AbsoluteHttpURL("https://goonbox.cr/images/2024")))
        c.direct_file.assert_not_awaited()


def pd_file(availability):
    return File(id="XTFsyKbm", name="1.jpg", date_upload="2025-01-13T22:21:00Z",
                mime_type="image/jpeg", hash_sha256="x", availability=availability)


class PixeldrainCaptcha(unittest.IsolatedAsyncioTestCase):
    def crawler(self, logged_in):
        api = SimpleNamespace(logged_in=logged_in,
                              download=lambda f: AbsoluteHttpURL(f"https://pixeldrain.com/api/file/{f.id}?download"))
        return SimpleNamespace(api=api, origin=AbsoluteHttpURL("https://pixeldrain.com"),
                               check_complete_by_hash=AsyncMock(return_value=True))

    async def test_captcha_file_fails_with_a_clear_reason(self):
        c = self.crawler(logged_in=False)
        with self.assertRaises(ScrapeError) as caught:
            await PixelDrainCrawler._file(c, SimpleNamespace(), pd_file("file_rate_limited_captcha_required"))
        self.assertIn("captcha", str(caught.exception))
        c.check_complete_by_hash.assert_not_awaited()

    async def test_api_key_or_normal_file_still_downloads(self):
        for logged_in, availability in [(True, "file_rate_limited_captcha_required"), (False, "")]:
            c = self.crawler(logged_in)
            await PixelDrainCrawler._file(c, SimpleNamespace(), pd_file(availability))
            c.check_complete_by_hash.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
