"""Offline tests: run with python -m unittest discover -s tests -p test_profile_metadata.py."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from profile_metadata import parse_listing, profile_url_parts


class ProfileMetadataTests(unittest.TestCase):
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
