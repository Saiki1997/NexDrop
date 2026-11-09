"""SimpCity posts with unrendered XenForo media BB code (from the 2026-11-10 03:10 run log).

Links such as https://simpcity.cr/%5BMEDIA=saint_vid%5DiwbWxP92_6W%5B/MEDIA%5D were queued as forum pages
and failed with "No downloader for this kind of link". They now become Saint (turbo.cr) video links.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))

from bs4 import BeautifulSoup  # noqa: E402

from cyberdrop_dl.crawlers import _forum  # noqa: E402
from cyberdrop_dl.crawlers.turbovid import TurboVidCrawler  # noqa: E402
from cyberdrop_dl.crawlers.xenforo.simpcity import SimpCityCrawler  # noqa: E402
from cyberdrop_dl.url_objects import AbsoluteHttpURL  # noqa: E402


def _post(html: str) -> SimpleNamespace:
    soup = BeautifulSoup(
        f'<article class="message" id="js-post-1"><div class="message-userContent">{html}</div></article>',
        "html.parser",
    )
    return SimpleNamespace(id=1, content=soup.select_one(".message-userContent"), article=soup.select_one("article"))


def _simpcity() -> SimpCityCrawler:
    forum = object.__new__(SimpCityCrawler)
    forum.__dict__["log"] = logging.getLogger("test_forum_media_bbcode")
    return forum


class MediaBBCode(unittest.TestCase):
    def test_saint_vid_raw_and_encoded(self):
        for text in (
            "[MEDIA=saint_vid]iwbWxP92_6W[/MEDIA]",
            "%5BMEDIA=saint_vid%5DiwbWxP92_6W%5B/MEDIA%5D",
            "https://simpcity.cr/%5BMEDIA=saint_vid%5DiwbWxP92_6W%5B/MEDIA%5D",
        ):
            self.assertEqual(_forum.media_bbcode_urls(text), (["https://turbo.cr/embed/iwbWxP92_6W"], []), text)

    def test_ids_with_dash_and_underscore_and_old_13_char_ids(self):
        urls, _ = _forum.media_bbcode_urls(
            "[MEDIA=saint_vid]X-YiUVhcw7p[/MEDIA] [MEDIA=saint_vid]kLg9t9bAYn-[/MEDIA] "
            "[media=SAINT_VID]7WhhRkWG3lCIY[/media]"
        )
        self.assertEqual(
            urls,
            [
                "https://turbo.cr/embed/X-YiUVhcw7p",
                "https://turbo.cr/embed/kLg9t9bAYn-",
                "https://turbo.cr/embed/7WhhRkWG3lCIY",
            ],
        )

    def test_two_tags_in_one_link(self):
        urls, _ = _forum.media_bbcode_urls(
            "https://simpcity.cr/[MEDIA=saint_vid]rpPlVTmG4cEiR[/MEDIA]%20[MEDIA=saint_vid]utONxZg8ohNce[/MEDIA]"
        )
        self.assertEqual(urls, ["https://turbo.cr/embed/rpPlVTmG4cEiR", "https://turbo.cr/embed/utONxZg8ohNce"])

    def test_other_media_sites(self):
        cases = {
            "[MEDIA=youtube]dQw4w9WgXcQ[/MEDIA]": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "[MEDIA=redgifs]sillyhappycat[/MEDIA]": "https://www.redgifs.com/watch/sillyhappycat",
            "[MEDIA=twitter]1850000000000000000[/MEDIA]": "https://x.com/i/status/1850000000000000000",
            "[MEDIA=imgur]a:AbCdE[/MEDIA]": "https://imgur.com/a/AbCdE",
            "[MEDIA=streamable]abc12[/MEDIA]": "https://streamable.com/abc12",
        }
        for tag, url in cases.items():
            self.assertEqual(_forum.media_bbcode_urls(tag)[0], [url], tag)

    def test_unknown_site_is_reported_not_queued(self):
        self.assertEqual(_forum.media_bbcode_urls("[MEDIA=nosuchsite]abc[/MEDIA]"), ([], ["[MEDIA=nosuchsite]abc[/MEDIA]"]))
        self.assertIsNone(_forum.pre_process_child("[MEDIA=nosuchsite]abc[/MEDIA]"))

    def test_pre_process_child_never_returns_bbcode(self):
        self.assertEqual(
            _forum.pre_process_child("[MEDIA=saint_vid]hUuyCm7ghTv[/MEDIA]"), "https://turbo.cr/embed/hUuyCm7ghTv"
        )
        self.assertEqual(_forum.pre_process_child("https://turbo.cr/embed/abc"), "https://turbo.cr/embed/abc")

    def test_converted_link_goes_to_the_turbo_crawler(self):
        url = AbsoluteHttpURL(_forum.media_bbcode_urls("[MEDIA=saint_vid]6xgZJyJRzk6[/MEDIA]")[0][0])
        self.assertIn(url.host, (TurboVidCrawler.PRIMARY_URL.host, *TurboVidCrawler.OLD_DOMAINS))
        self.assertEqual(url.parts[1:], ("embed", "6xgZJyJRzk6"))


class PostCollection(unittest.TestCase):
    def test_bbcode_href_in_a_post_becomes_a_saint_link_in_page_order(self):
        post = _post(
            '<a href="https://bunkr.cr/f/first">one</a>'
            '<a href="[MEDIA=saint_vid]hUuyCm7ghTv[/MEDIA]">two</a>'
            '<a href="https://bunkr.cr/f/third">three</a>'
        )
        self.assertEqual(
            _simpcity()._collect_post_urls(post),
            ["https://bunkr.cr/f/first", "https://turbo.cr/embed/hUuyCm7ghTv", "https://bunkr.cr/f/third"],
        )

    def test_bbcode_left_as_plain_text_is_found(self):
        post = _post(
            "<p>new vids</p>[MEDIA=saint_vid]iwbWxP92_6W[/MEDIA]<br>[MEDIA=saint_vid]6T23BCGWzkH[/MEDIA]"
            '<iframe src="https://turbo.cr/embed/AXNaAy-dlk9"></iframe>'
        )
        urls = _simpcity()._collect_post_urls(post)
        self.assertEqual(
            urls,
            [
                "https://turbo.cr/embed/iwbWxP92_6W",
                "https://turbo.cr/embed/6T23BCGWzkH",
                "https://turbo.cr/embed/AXNaAy-dlk9",
            ],
        )
        self.assertFalse(any("MEDIA" in u for u in urls))

    def test_same_video_as_link_and_text_is_queued_once(self):
        post = _post('<a href="[MEDIA=saint_vid]nM952UelhQ8[/MEDIA]">[MEDIA=saint_vid]nM952UelhQ8[/MEDIA]</a>')
        self.assertEqual(_simpcity()._collect_post_urls(post), ["https://turbo.cr/embed/nM952UelhQ8"])


if __name__ == "__main__":
    unittest.main()
