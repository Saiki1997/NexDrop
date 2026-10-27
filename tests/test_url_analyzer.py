"""3.1 URL analyzer and link preview: link types from the engine's handlers, duplicate links,
and what one request shows (title, size, reasons), without guessing."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import url_analyzer  # noqa: E402
from local_server import LocalServer  # noqa: E402


class Normalize(unittest.TestCase):
    def test_tracking_parameters_slash_and_www_are_ignored(self):
        same = ["https://www.cyberdrop.cr/a/xyz/?utm_source=x", "https://cyberdrop.cr/a/xyz", "https://cyberdrop.cr/a/xyz#top"]
        self.assertEqual(len({url_analyzer.normalize(u) for u in same}), 1)

    def test_meaningful_parameters_stay(self):
        self.assertIn("page=2", url_analyzer.normalize("https://site.test/a?page=2&fbclid=1"))
        self.assertIn("s=cats", url_analyzer.normalize("https://site.test/search?s=cats"))  # a real search
        self.assertNotIn("s=20", url_analyzer.normalize("https://x.com/u/status/1?s=20"))  # X's share tracker
        self.assertIn("v=abc", url_analyzer.normalize("https://www.youtube.com/watch?v=abc&si=zz"))
        self.assertNotIn("si=", url_analyzer.normalize("https://www.youtube.com/watch?v=abc&si=zz"))


class Analyze(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = url_analyzer.Handlers()

    def kinds(self, urls):
        return [(i["handler"], i["kind"]) for i in url_analyzer.analyze(urls, self.handlers)]

    def test_engine_handlers_are_loaded(self):
        self.assertGreater(len(self.handlers.mapping), 300)

    def test_link_types_come_from_handler_formats(self):
        got = self.kinds([
            "https://bunkr.cr/a/abc123",
            "https://www.erome.com/someuser",
            "https://simpcity.cr/threads/some-model.12345/",
            "https://simpcity.cr/threads/some-model.12345/post-999",
            "https://gofile.io/d/AbC",
            "https://x.com/user/status/1",
            "https://coomer.st/onlyfans/user/name",
        ])
        self.assertEqual(got, [("builtin", "album"), ("builtin", "profile"), ("builtin", "forum thread"),
                               ("builtin", "forum post"), ("builtin", "cloud folder"), ("builtin", "post"),
                               ("builtin", "profile")])

    def test_files_torrents_and_unknown_sites(self):
        items = url_analyzer.analyze(["https://unknown.test/x/a.zip", "https://unknown.test/v.mp4", "magnet:?xt=urn:btih:x",
                                      "https://unknown.test/page"], self.handlers)
        self.assertEqual([(i["handler"], i["kind"], i["media"]) for i in items],
                         [("direct", "archive", "archive"), ("direct", "direct file", "video"), ("none", "torrent", ""),
                          ("none", "page", "")])

    def test_duplicates_point_at_the_first_copy(self):
        items = url_analyzer.analyze(["https://bunkr.cr/a/abc", "https://b.test/x", "https://www.bunkr.cr/a/abc/?utm_medium=1"],
                                     self.handlers)
        self.assertEqual([i["duplicateOf"] for i in items], [None, None, 0])

    def test_sign_in_from_the_catalog(self):
        items = url_analyzer.analyze(["https://t.me/channel/1", "https://gofile.io/d/x", "https://bunkr.cr/a/x"], self.handlers)
        self.assertEqual([i["signIn"] for i in items], ["optional", "optional", ""])

    def test_short_links_are_marked(self):
        self.assertTrue(url_analyzer.analyze(["https://bit.ly/abc"], self.handlers)[0]["shortened"])


class Preview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LocalServer().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()

    def preview(self, url, handler="none", kind="page"):
        return url_analyzer.preview_one({"url": url, "handler": handler, "kind": kind})

    def test_page_title_site_author_and_distinct_file_links(self):
        got = self.preview(self.server.url("page", "album"))
        self.assertEqual((got["title"], got["siteName"], got["author"], got["fileLinks"]), ("Summer & Sun", "Test Host", "Ana", 3))

    def test_direct_file_size_from_a_head_request(self):
        url = self.server.url("ok", "big.mp4", size=123456)
        got = self.preview(url, "direct", "direct file")
        self.assertEqual(got["size"], 123456)
        self.assertEqual(self.server.hits["ok/big.mp4"], 1)

    def test_missing_page_says_so_and_guesses_nothing(self):
        got = self.preview(self.server.url("gone", "x"))
        self.assertIn("404", got["note"])
        self.assertIsNone(got["fileLinks"])
        self.assertEqual(got["title"], "")

    def test_blocked_hosts_are_not_opened(self):
        got = url_analyzer.preview_one({"url": self.server.url("page", "y"), "handler": "none", "kind": "page"},
                                       safe=lambda host: "Private and local addresses are blocked")
        self.assertIn("Private", got["note"])
        self.assertEqual(self.server.hits["page/y"], 0)

    def test_short_link_is_followed_but_never_to_a_blocked_address(self):
        target = self.server.url("page", "z")
        short = self.server.url("redirect", "s1", to=target)
        allow_all = url_analyzer.safe_opener("", lambda host: None)
        self.assertEqual(url_analyzer.resolve_short(short, allow_all), target)
        refuse = url_analyzer.safe_opener("", lambda host: "blocked")
        self.assertEqual(url_analyzer.resolve_short(self.server.url("redirect", "s2", to=target), refuse), "")


class CommandLine(unittest.TestCase):
    def test_analyze_prints_one_json_object(self):
        proc = subprocess.run([sys.executable, str(ROOT / "desktop" / "url_analyzer.py"), "analyze"],
                              input=json.dumps({"urls": ["https://bunkr.cr/a/x", "https://bunkr.cr/a/x/"]}) + "\n",
                              capture_output=True, text=True, timeout=120)
        data = json.loads(proc.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual([i["duplicateOf"] for i in data["items"]], [None, 0])


if __name__ == "__main__":
    unittest.main()
