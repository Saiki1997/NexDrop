"""Smart Naming 3.0: filename templates and number width. Offline, with stubbed crawlers."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "tests"))

from cyberdrop_dl import smart_naming, source_order  # noqa: E402
from cyberdrop_dl.smart_naming import NamingSettings, clean_template, render_template, template_problem  # noqa: E402
from test_smart_naming import URL, SmartCase, _crawler  # noqa: E402

VALUES = {"title": "Summer Shoot", "original": "IMG_1", "album": "", "thread": "Lala Thread", "post": "",
          "creator": "lala", "date": "2026-10-27", "site": "bunkr", "type": "image"}


class Templates(unittest.TestCase):
    def test_spec_examples_render(self):
        self.assertEqual(render_template("{title}-{number}", VALUES, "007", 120), "Summer Shoot-007")
        self.assertEqual(render_template("{creator}-{title}-{number}", VALUES, "007", 120), "lala-Summer Shoot-007")
        self.assertEqual(render_template("{date}-{title}-{number}", VALUES, "007", 120), "2026-10-27-Summer Shoot-007")
        self.assertEqual(render_template("[{site}] {title}", VALUES, "", 120), "[bunkr] Summer Shoot")

    def test_empty_variables_drop_out_with_their_separator(self):
        self.assertEqual(render_template("{thread}-{post}-{number}", VALUES, "007", 120), "Lala Thread-007")
        self.assertEqual(render_template("{album} {creator}", VALUES, "", 120), "lala")

    def test_no_known_name_falls_back_to_the_automatic_name(self):
        self.assertEqual(render_template("{album}-{number}", VALUES, "007", 120), "")

    def test_long_titles_are_shortened_not_the_fixed_parts(self):
        out = render_template("{creator} - {title}-{number}", {**VALUES, "title": "word " * 60}, "007", 60)
        self.assertTrue(out.startswith("lala - word") and out.endswith("-007"), out)
        self.assertLessEqual(len(out), 60)

    def test_bad_templates_are_refused(self):
        self.assertEqual(template_problem("{creator}-{title}-{number}"), "")
        self.assertIn("unknown variable {artist}", template_problem("{artist}-{title}"))
        self.assertIn("no name variable", template_problem("{date}-{number}"))
        self.assertIn("Windows", template_problem("{title}/{number}"))
        self.assertIn("{ or }", template_problem("{title"))
        self.assertEqual(clean_template("{title}-{number}.{ext}"), "{title}-{number}")  # extension is added anyway
        self.assertEqual(clean_template("{bogus}"), "")

    def test_settings_and_fingerprint(self):
        old = NamingSettings.from_payload({})
        self.assertEqual(old.template, "")
        self.assertEqual(old.width, 3)
        # 3.1.1 takes dates out of names, so names an older version kept are made again...
        self.assertEqual(old.fingerprint(), "1.auto.suffix.1.120.nd")
        # ...unless that is turned off: then 2.5 names of a resumed job are kept
        self.assertEqual(NamingSettings.from_payload({"stripDates": False}).fingerprint(), "1.auto.suffix.1.120")
        new = NamingSettings.from_payload({"nameTemplate": "{creator}-{title}", "numberWidth": 4})
        self.assertNotEqual(new.fingerprint(), old.fingerprint())
        self.assertEqual(NamingSettings.from_payload({"numberWidth": 9}).width, 3)


class TemplatedNames(SmartCase):
    settings = NamingSettings.from_payload({"nameTemplate": "{creator}-{title}-{number}", "numberWidth": 4})

    def setUp(self):
        super().setUp()
        self.tokens.append((source_order.NUMBER_WIDTH, source_order.NUMBER_WIDTH.set(4)))

    def test_bunkr_album_from_a_simpcity_post_uses_the_template(self):
        token = smart_naming.SETTINGS.set(NamingSettings.from_payload({"nameTemplate": "{thread}-{title}-{number}"}))
        self.addCleanup(smart_naming.SETTINGS.reset, token)
        from cyberdrop_dl.crawlers.bunkr import BunkrCrawler
        from cyberdrop_dl.crawlers.xenforo.simpcity import SimpCityCrawler

        forum, _ = _crawler(SimpCityCrawler, self.root)
        host, saved = _crawler(BunkrCrawler, self.root)
        thread = self.item("https://simpcity.cr/threads/lala-kudo.123/")
        thread.setup_as_forum("")
        thread.append_folders(forum.create_title("Lala Kudo", thread_id=123))
        post = thread.create_new(thread.url, add_parent=URL("https://simpcity.cr/threads/lala-kudo.123/post-9"))
        post.setup_as_post("")
        post.append_folders(forum.create_separate_post_title(None, "9", None, caption="Summer Beach Collection"))
        album = post.create_child(URL("https://bunkr.cr/a/xyz"))
        album.reset()
        album.setup_as_album(host.create_title("a827fc8b", "xyz"), album_id="xyz")
        child = album.create_child(URL("https://bunkr.cr/f/1"))
        self.save(host, child, "https://cdn.bunkr.ru/a827fc8.jpg?token=SECRET", "a827fc8.jpg")
        name = saved[0].filename
        self.assertEqual(name, "Lala Kudo-Summer Beach Collection-0001.jpg")
        self.assertNotIn("SECRET", name)

    def test_numbers_have_four_digits_and_stay_stable_across_a_resume(self):
        first = smart_naming.name_file("https://c/1.jpg", "Beach day.jpg", ".jpg", {"profile": "Kira"}, "https://c/p",
                                       self.root, lambda n: n)
        self.assertEqual(first.filename, "Kira-Beach day-0001.jpg")
        manifest = self.root / "m.jsonl"
        row = {"url": "https://c/1.jpg", "sequence": 1, "name": first.filename, "job": "J",
               "naming": self.settings.fingerprint(), "title": first.title}
        manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
        source_order.reset(str(manifest), "J")
        again = smart_naming.name_file("https://c/1.jpg", "Beach day.jpg", ".jpg", {"profile": "Kira"}, "https://c/p",
                                       self.root, lambda n: n)
        self.assertEqual(again.filename, first.filename)
        nxt = smart_naming.name_file("https://c/2.jpg", "x.jpg", ".jpg", {"profile": "Kira"}, "https://c/p",
                                     self.root, lambda n: n)
        self.assertTrue(nxt.filename.endswith("-0002.jpg"), nxt.filename)

    def test_upload_date_fills_date(self):
        settings = NamingSettings.from_payload({"nameTemplate": "{date} {title}"})
        token = smart_naming.SETTINGS.set(settings)
        try:
            d = smart_naming.name_file("https://c/3.jpg", "Beach day.jpg", ".jpg", {}, "https://c/p", self.root,
                                       lambda n: n, date="2026-10-27")
        finally:
            smart_naming.SETTINGS.reset(token)
        self.assertEqual(d.filename, "2026-10-27 Beach day-0001.jpg")  # number by the Sequence numbering setting


if __name__ == "__main__":
    unittest.main()
