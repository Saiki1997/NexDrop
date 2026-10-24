"""Offline tests: run with python -m unittest discover -s tests -p test_profile_metadata.py."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from profile_metadata import classify_monotirou_url, parse_listing, profile_url_parts


class ProfileMetadataTests(unittest.TestCase):
    def test_monotirou_page_types(self):
        cases = [
            ("https://monotirou.com/", "home", None, None),
            ("https://monotirou.com/users/nesa1", "profile", "username", "nesa1"),
            ("https://monotirou.com/video/SdEyIPSy?rank=1&surface=home_feed_popular",
             "video", "video_id", "SdEyIPSy"),
        ]
        for url, kind, key, value in cases:
            with self.subTest(url=url):
                info = classify_monotirou_url(url)
                self.assertEqual(info["type"], kind)
                self.assertEqual(info["canonical"], url.split("?")[0])
                if key:
                    self.assertEqual(info[key], value)

    def test_monotirou_canonicalization(self):
        for path in ("", "/", "/users/nesa1/", "/video/SdEyIPSy/"):
            with self.subTest(path=path):
                info = classify_monotirou_url(
                    f"http://WWW.MONOTIROU.COM:80{path}?surface=home&rank=1&rank=2#top")
                self.assertEqual(info["canonical"],
                                 "https://monotirou.com" + (path.rstrip("/") or "/"))
        info = classify_monotirou_url(
            "https://monotirou.com:443/users/nesa1?page=2&rank=1&tag=a&tag=b&empty=")
        self.assertEqual(info["canonical"],
                         "https://monotirou.com/users/nesa1?page=2&tag=a&tag=b&empty=")
        plain = classify_monotirou_url("https://monotirou.com/video/SdEyIPSy")
        tracked = classify_monotirou_url(
            plain["canonical"] + "?surface=home_feed_popular&rank=1")
        self.assertEqual(plain, tracked)

    def test_monotirou_rejects_invalid_urls(self):
        for url in (
            "https://monotirou.com.evil.test/", "https://evil.test/video/SdEyIPSy",
            "ftp://monotirou.com/", "//monotirou.com/", "/users/nesa1",
            "https://user@monotirou.com/", "https://@monotirou.com/",
            "https://monotirou.com:8080/", "https://monotirou.com:80/",
            "https://monotirou.com:bad/", "https://monotirou.com:99999/",
            "https://[monotirou.com/", "https://monotirou.com/users/",
            "https://monotirou.com/video/", "https://monotirou.com/privacy",
            "https://monotirou.com/video/id/extra", "https://monotirou.com/users/a%2Fb",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                classify_monotirou_url(url)

    def test_profile_url(self):
        info = profile_url_parts("https://monotirou.com/users/nesa1", domain="monotirou.com")
        self.assertEqual(info["username"], "nesa1")
        with self.assertRaises(ValueError):
            profile_url_parts("http://localhost/users/nesa1", domain="monotirou.com")
        with self.assertRaises(ValueError):
            profile_url_parts("https://monotirou.com.evil.test/users/a", domain="monotirou.com")

    def test_listing_order_dedup_and_pagination(self):
        html = """<html><head><link rel="next" href="/users/test?page=2"></head>
        <body><a href="/posts/one"><img src="/thumb/one.jpg" alt="First"></a>
        <a href="/posts/one">Duplicate</a>
        <a href="/posts/two">Second</a>
        <a href="https://evil.test/posts/third">External</a>
        <a href="/image.jpg">Binary</a>
        <a href="/privacy">Privacy</a></body></html>"""
        result = parse_listing(html, "https://example.test/users/test",
                               item_path_pattern=r"/posts/[a-z]+")
        self.assertEqual([r["title"] for r in result["items"]], ["First", "Second"])
        self.assertEqual(result["next_page"], "https://example.test/users/test?page=2")

    def test_explicit_pattern_required(self):
        with self.assertRaises(ValueError):
            parse_listing("<html></html>", "https://example.test/", item_path_pattern="")


if __name__ == "__main__":
    unittest.main()
