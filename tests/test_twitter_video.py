"""3.2.5: X profile runs (x.com/RoshyTV) where a few long videos answered 503 on every try.

From the run logs of 6-8 Nov 2026: the same eight video.twimg.com MP4s failed with 503 on every
run and retry, and their failures paused twimg.com so 850 other files failed as "Site Paused"."""
import contextlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "engine"))
from cyberdrop_dl.crawlers.twitter.crawler import TwitterCrawler, _extract_files  # noqa: E402
from cyberdrop_dl.crawlers.twitter.models import Video, VideoFormat  # noqa: E402
from cyberdrop_dl.url_objects import AbsoluteHttpURL  # noqa: E402

U = AbsoluteHttpURL
TWEET = U("https://x.com/RoshyTV/status/2116122948516053421")
BEST = U("https://video.twimg.com/amplify_video/2105906474526420992/vid/avc1/1920x1080/0BFhaxhYb-xQ2tsT.mp4?tag=29")
SMALL = U("https://video.twimg.com/amplify_video/2105906474526420992/vid/avc1/1280x720/abc.mp4?tag=29")
HLS = U("https://video.twimg.com/amplify_video/2105906474526420992/pl/xyz.m3u8?tag=29")


def video(*formats):
    return Video(type="video", url=BEST, width=1920, height=1080, duration=14400.0, formats=list(formats))


FORMATS = (
    VideoFormat(BEST, "mp4", "h264", 10_000_000),
    VideoFormat(SMALL, "mp4", "h264", 2_000_000),
    VideoFormat(HLS, "m3u8", "h264", None),
)


class Item(SimpleNamespace):
    def create_child(self, url):
        return Item(url=url)


def crawler(answers):
    """answers: {url: status or None}"""
    async def probe(url):
        return answers.get(url)

    return SimpleNamespace(
        _probe=probe,
        catch_errors=lambda url: contextlib.nullcontext(),
        handle_external_links=MagicMock(),
        log=MagicMock(),
        request_m3u8=AsyncMock(return_value=("RENDITION", None)),
        handle_file=AsyncMock(),
        create_custom_filename=lambda stem, ext, **kw: stem + ext,
    )


class XVideoFallback(unittest.IsolatedAsyncioTestCase):
    async def test_working_best_mp4_is_queued_as_before(self):
        c, item = crawler({}), Item(url=BEST)
        await TwitterCrawler._video(c, item, video(*FORMATS), TWEET)
        c.handle_external_links.assert_called_once_with(item, reset=False)
        c.handle_file.assert_not_awaited()

    async def test_refused_best_mp4_uses_the_next_one(self):
        c = crawler({BEST: 503})
        await TwitterCrawler._video(c, Item(url=BEST), video(*FORMATS), TWEET)
        queued = c.handle_external_links.call_args.args[0]
        self.assertEqual(queued.url, SMALL)

    async def test_no_working_mp4_downloads_the_hls_stream(self):
        c = crawler({BEST: 503, SMALL: 503})
        await TwitterCrawler._video(c, Item(url=BEST), video(*FORMATS), TWEET)
        c.handle_external_links.assert_not_called()
        c.request_m3u8.assert_awaited_once()
        self.assertEqual(c.request_m3u8.call_args.args[0], HLS)
        args, kwargs = c.handle_file.call_args
        self.assertEqual(kwargs["m3u8"], "RENDITION")
        self.assertEqual(kwargs["referer"], TWEET)
        self.assertEqual(args[3], ".mp4")

    def test_videos_carry_their_formats(self):
        tweet = SimpleNamespace(
            media=SimpleNamespace(photos=[], videos=[video(*FORMATS)], external=None),
            card=None, raw_text=SimpleNamespace(facets=[]), article=None,
        )
        files = list(_extract_files(tweet, SimpleNamespace()))
        self.assertEqual(files[0].url, BEST)
        self.assertEqual(len(files[0].video.formats), 3)


class SitePauseCountsFiles(unittest.TestCase):
    def setUp(self):
        import bridge

        self.breaker = bridge._SiteBreaker(threshold=3, cooldown=60, ceiling=600)

    def test_the_same_file_failing_again_does_not_pause_the_site(self):
        for _ in range(10):
            self.assertEqual(self.breaker.failed("twimg.com", "503 Service Unavailable", now=0, url=str(BEST)), 0)
        self.assertEqual(self.breaker.blocked_for("twimg.com", now=1), 0)

    def test_different_files_failing_still_pause_it(self):
        self.breaker.failed("twimg.com", "503", now=0, url="a")
        self.breaker.failed("twimg.com", "503", now=0, url="b")
        self.assertEqual(self.breaker.failed("twimg.com", "503", now=0, url="c"), 60)

    def test_a_known_bad_file_after_the_pause_does_not_reopen_it(self):
        for name in "abc":
            self.breaker.failed("twimg.com", "503", now=0, url=name)
        self.assertEqual(self.breaker.failed("twimg.com", "503", now=61, url="a"), 0)
        self.assertEqual(self.breaker.failed("twimg.com", "503", now=61, url="d"), 120)


if __name__ == "__main__":
    unittest.main()
