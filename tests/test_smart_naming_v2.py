"""Smart naming V2: scoring, cross-domain context, per-file titles, the naming report, learned
names, titles inside files and the accuracy floor. Offline; files are built in temp folders."""

from __future__ import annotations

import importlib
import json
import logging
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "tests"))

from bs4 import BeautifulSoup  # noqa: E402

from cyberdrop_dl import media_tags, smart_naming  # noqa: E402
from cyberdrop_dl.smart_naming import NamingSettings, clean_title  # noqa: E402
from cyberdrop_dl import source_order  # noqa: E402
from test_smart_naming import URL, SmartCase, _crawler  # noqa: E402

AUTO = NamingSettings.from_payload({})


def explain(stem, naming, page_url="https://example.invalid/none", settings=AUTO):
    return smart_naming.explain_title(stem, naming, page_url, settings)


class Captions(unittest.TestCase):
    def test_semantic_cleaning(self):
        for text, want in (
            ("Here is my new Summer Beach Photoshoot! Check my profile for more updates. #summer #photography",
             "Summer Beach Photoshoot"),
            ("Just dropped: Pool Party Part 2 - link in bio", "Pool Party Part 2"),
            ("Check out my new Gym Set", "Gym Set"),
            ("Sunset vibes. Full video on my OF", "Sunset vibes"),
            ("My new Bikini set 💕 hope you like it", "Bikini set"),
            ("Behind the scenes 🎬 Full set available on my page", "Behind the scenes"),
            ("Birthday dinner 🎂 thank you all for the wishes", "Birthday dinner"),
            ("Garden Party - 45 pics", "Garden Party"),
            ("Red Dress Shoot (2024)", "Red Dress Shoot (2024)"),
            ("New York trip", "New York trip"),
        ):
            self.assertEqual(clean_title(text, 80), want, text)

    def test_text_that_names_nothing_is_dropped(self):
        for text in ("Follow me for more 😘", "Thanks for watching!", "Discussion thread", "Here you go", "Part 2",
                     "#fyp #foryou #viral #dance", "🔥🔥🔥"):
            self.assertEqual(clean_title(text, 80), "", text)


class Scoring(unittest.TestCase):
    def test_rejections_say_why(self):
        for text, why in (
            ("Bunkr", "website"), ("Free Image Hosting", "host"), ("Hot singles in your area", "advert"),
            ("Just a moment...", "error page"), ("404 Not Found", "error page"), ("New folder", "generic"),
            ("Mega link: password in spoiler", "download instructions"), ("a827fc8b", "hash"),
        ):
            quality, reason = smart_naming.assess(clean_title(text, 80, post_text=False) or text)
            self.assertEqual(quality, 0.0, text)
            self.assertIn(why, reason, text)

    def test_hash_file_takes_the_post_title_over_a_generic_host_page(self):
        soup = BeautifulSoup('<html><head><meta property="og:title" content="Download File">'
                             '<meta property="og:site_name" content="Bunkr"></head></html>', "html.parser")
        smart_naming.note_page("https://bunkr.cr/f/a82f72", soup)
        title, source, score, reason, cands = explain(
            "a82f72", {"album": "Summer Collection", "caption": "Summer Beach", "caption_source": "caption"},
            "https://bunkr.cr/f/a82f72")
        self.assertEqual((title, source), ("Summer Beach", "caption"))
        self.assertEqual(reason, "highest score")
        verdicts = {c.source: (c.verdict, c.reason) for c in cands}
        self.assertEqual(verdicts["original filename"][0], "rejected")
        self.assertIn("hash", verdicts["original filename"][1])
        self.assertEqual(verdicts["album title"][0], "lower")

    def test_specific_original_name_beats_the_post_title(self):
        title, source, *_ = explain("Sunset at Beach", {"caption": "Summer Beach Collection", "caption_source": "forum post"})
        self.assertEqual((title, source), ("Sunset at Beach", "original filename"))

    def test_per_file_titles_beat_the_album(self):
        for media in ("Beach Sunset", "Swimming Pool", "Night Photography"):
            title, source, *_ = explain("Xa1Yb2Z", {"album": "Summer Collection", "media_title": media})
            self.assertEqual((title, source), (media, "media title"))
        title, source, *_ = explain("Xa1Yb2Z", {"album": "Summer Collection"})
        self.assertEqual((title, source), ("Summer Collection", "album title"))

    def test_nothing_trustworthy_keeps_the_original(self):
        title, source, _score, reason, _ = explain("a9c82fa8", {"album": "gofile"})
        self.assertEqual((title, source), ("a9c82fa8", "original filename"))
        self.assertIn("original name stays", reason)

    def test_hosts_random_id_comes_off_a_real_name(self):
        title, source, *_ = explain("Beach Sunset-aB3dE9fG", {"album": "Summer"})
        self.assertEqual((title, source), ("Beach Sunset", "original filename"))

    def test_forum_replies_and_questions_are_not_titles(self):
        for caption in ("Thank you so much for posting this", "Anyone have more of her?", "Credit to the original uploader"):
            title, source, *_ = explain("c7d8e9f0", {"thread": "Rosa", "caption": caption, "caption_source": "forum post"})
            self.assertEqual((title, source), ("Rosa", "thread title"), caption)

    def test_strategy_original_never_renames(self):
        settings = NamingSettings.from_payload({"nameStrategy": "original"})
        self.assertEqual(explain("a9c82fa8", {"album": "Summer"}, settings=settings)[:2], ("a9c82fa8", "original filename"))


class CrossDomain(SmartCase):
    def test_a_bunkr_album_from_a_simpcity_post_keeps_the_posts_title(self):
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
        album.reset()  # handle_external_links
        album.setup_as_album(host.create_title("a827fc8b", "xyz"), album_id="xyz")
        for n, name in enumerate(("a827fc8.jpg", "Sunset at Beach.jpg"), 1):
            child = album.create_child(URL(f"https://bunkr.cr/f/{n}"))
            self.save(host, child, f"https://cdn.bunkr.ru/{name}?n={name}&token=SECRET", name)
        self.assertEqual([m.filename for m in saved], ["Summer Beach Collection-001.jpg", "Sunset at Beach-002.jpg"])
        self.assertEqual(saved[0].download_folder.relative_to(self.root).as_posix(), "Lala Kudo/a827fc8b/Summer Beach Collection")
        # its own name, and with "Folder for each post or album" (3.1.1) the same post's folder
        self.assertEqual(saved[1].download_folder.relative_to(self.root).as_posix(), "Lala Kudo/a827fc8b/Summer Beach Collection")

    def test_a_post_without_text_does_not_inherit_the_previous_posts_caption(self):
        from cyberdrop_dl.crawlers.xenforo.simpcity import SimpCityCrawler

        forum, saved = _crawler(SimpCityCrawler, self.root)
        thread = self.item("https://simpcity.cr/threads/t.5/")
        thread.setup_as_forum("")
        thread.append_folders(forum.create_title("Kira", thread_id=5))
        for post_id, caption in (("201", "Beach Trip Part 1"), ("305", "")):
            post = thread.create_new(thread.url)
            post.setup_as_post("")
            post.append_folders(forum.create_separate_post_title(None, post_id, None, caption=caption))
            self.save(forum, post.create_child(URL(f"https://simpcity.cr/attachments/{post_id}")),
                      f"https://simpcity.cr/data/{post_id}.jpg", f"cc33dd{post_id}.jpg")
        self.assertEqual([m.filename for m in saved], ["Beach Trip Part 1-001.jpg", "Kira-002.jpg"])


class TitleFolders(SmartCase):
    def folder(self, title, source, where, **payload):
        settings = NamingSettings.from_payload(payload)
        return smart_naming.title_folder(title, source, Path(where), settings)

    def test_shared_titles_get_a_folder(self):
        self.assertEqual(self.folder("Leaked set pics", "forum post", "/dl/Lala Kudo"), "Leaked set pics")
        self.assertEqual(self.folder("Summer Collection", "album title", "/dl/Lala Kudo"), "Summer Collection")

    def test_no_folder_for_own_names_repeats_or_when_off(self):
        self.assertEqual(self.folder("NHDTB-823 Saliva dripping", "original filename", "/dl/Lala Kudo"), "")
        self.assertEqual(self.folder("Beach Sunset", "media title", "/dl/Imgur"), "")
        self.assertEqual(self.folder("Lala Kudo", "thread title", "/dl/Lala Kudo (SimpCity)/Loose Files (Bunkr)"), "")
        self.assertEqual(self.folder("Pool Day", "album title", "/dl/Pool Day (2)"), "")
        self.assertEqual(self.folder("Leaked set pics", "forum post", "/dl/Lala Kudo", titleFolders=False), "")
        self.assertEqual(self.folder('a<b>:c', "caption", "/dl/x"), "a-b--c")

    def test_resumed_job_keeps_the_folder(self):
        from cyberdrop_dl.crawlers.xenforo.simpcity import SimpCityCrawler

        manifest = self.root / "m.jsonl"
        manifest.write_text(json.dumps({"url": "https://simpcity.cr/data/1.jpg", "sequence": 193, "name": "Leaked set pics-193.jpg",
                                        "title": "Leaked set pics", "source": "forum post", "naming": self.settings.fingerprint(),
                                        "job": "J"}) + "\n")
        source_order.reset(str(manifest), "J")
        forum, saved = _crawler(SimpCityCrawler, self.root)
        thread = self.item("https://simpcity.cr/threads/lala-kudo.1/")
        thread.setup_as_forum("")
        thread.append_folders(forum.create_title("Lala Kudo", thread_id=1))
        self.save(forum, thread.create_child(URL("https://simpcity.cr/attachments/1")), "https://simpcity.cr/data/1.jpg", "ab12cd.jpg")
        self.assertEqual(saved[0].filename, "Leaked set pics-193.jpg")
        self.assertEqual(self.rel(saved[0]), "Lala Kudo/Leaked set pics")


class IdentityAndReport(SmartCase):
    def test_identity_and_report_have_no_query_strings(self):
        from cyberdrop_dl.crawlers.imgur import ImgurCrawler

        crawler, saved = _crawler(ImgurCrawler, self.root)
        report = self.root / "run.naming.log"
        smart_naming.REPORT.open(report)
        self.addCleanup(smart_naming.REPORT.open, None)
        album = self.item("https://imgur.com/a/abc?token=SECRET")
        album.setup_as_album(crawler.create_title("Summer Collection", "abc"), album_id="abc")
        with self.assertLogs("cyberdrop_dl", level=logging.INFO) as logs:
            self.save(crawler, album.create_child(URL("https://imgur.com/Xa1Yb2Z")), "https://i.imgur.com/Xa1Yb2Z.jpg?sig=SECRET",
                      "Xa1Yb2Z.jpg", media_title="Beach Sunset")
        self.assertEqual(saved[0].filename, "Beach Sunset-001.jpg")
        line = next(r for r in logs.output if "Source order" in r)
        identity = json.loads(line.split("\t")[-1])
        self.assertEqual(identity["media_url"], "https://i.imgur.com/Xa1Yb2Z.jpg")
        self.assertEqual((identity["album"], identity["media_title"], identity["type"]), ("Summer Collection", "Beach Sunset", "image"))
        self.assertIn("album", identity["context"])
        text = report.read_text(encoding="utf-8")
        self.assertIn("Xa1Yb2Z.jpg  ->  Beach Sunset-001.jpg", text)
        self.assertIn("media title “Beach Sunset”", text)
        self.assertNotIn("SECRET", text + line)

    def test_bridge_passes_the_identity_on(self):
        bridge = importlib.import_module("bridge")
        events = []
        original = bridge.emit
        bridge.emit = events.append
        self.addCleanup(setattr, bridge, "emit", original)
        record = logging.LogRecord("cyberdrop_dl", logging.INFO, "", 0, "Source order\t%s", (
            "\t".join(["3", "https://x/y.jpg", "Beach-003.jpg", "A", "y.jpg", "caption", "0.8", "auto", "Beach", "A",
                       "1.auto.suffix.1.120", json.dumps({"post_id": "9", "context": {"post": "simpcity.cr|post:9"}})]),), None)
        bridge._LogBridge().emit(record)
        self.assertEqual(events[0]["identity"]["context"]["post"], "simpcity.cr|post:9")


class LearnedNames(SmartCase):
    def test_a_correction_applies_only_in_its_own_post(self):
        path = self.root / "corrections.jsonl"
        path.write_text(json.dumps({"context": "simpcity.cr|post:9", "title": "Summer Beach", "use": "Beach Day 2026"}) + "\n")
        smart_naming.CORRECTIONS.load(path)
        self.addCleanup(smart_naming.CORRECTIONS.load, None)
        naming = {"caption": "Summer Beach", "caption_source": "caption", "post_key": "simpcity.cr|post:9"}
        self.assertEqual(explain("a8f7", naming)[:2], ("Beach Day 2026", "your correction"))
        other = {**naming, "post_key": "simpcity.cr|post:10"}
        self.assertEqual(explain("a8f7", other)[:2], ("Summer Beach", "caption"))
        path.write_text(path.read_text() + json.dumps({"context": "simpcity.cr|post:9", "title": "Summer Beach", "removed": True}) + "\n")
        smart_naming.CORRECTIONS.load(path)
        self.assertEqual(explain("a8f7", naming)[:2], ("Summer Beach", "caption"))

    def test_renaming_in_history_learns_for_the_post(self):
        from job_store import JobStore, read_corrections

        downloads = self.root / "dl"
        (downloads / "Lala Kudo").mkdir(parents=True)
        file = downloads / "Lala Kudo" / "Summer Beach-004.jpg"
        file.write_bytes(b"x")
        store = JobStore(self.root / "jobs.sqlite")
        self.addCleanup(store.close)
        job = store.start_job(["https://simpcity.cr/threads/x.1/"], str(downloads))
        store.note(job, {"url": "https://cdn/x.jpg", "event": "success", "sequence": 4, "name": file.name,
                         "destination": str(file), "naming": {"original": "a8f7.jpg", "title": "Summer Beach", "source": "caption",
                                                              "identity": {"context": {"post": "simpcity.cr|post:9"}}}})
        item = store.conn.execute("SELECT id FROM items").fetchone()[0]
        corrections = self.root / "corrections.jsonl"
        escaped = store.rename(item, "../evil", None)  # a path in the name stays a name in the same folder
        self.assertTrue(escaped["ok"])
        self.assertTrue((downloads / "Lala Kudo" / escaped["name"]).is_file())
        self.assertFalse((downloads / "evil.jpg").exists())
        self.assertFalse(store.rename(item, file.name, corrections)["learned"])  # back to the title it had
        result = store.rename(item, "Beach Day 2026-004", corrections)
        self.assertEqual((result["ok"], result["name"], result["learned"]), (True, "Beach Day 2026-004.jpg", True))
        self.assertTrue((downloads / "Lala Kudo" / "Beach Day 2026-004.jpg").is_file())
        self.assertEqual(read_corrections(corrections),
                         [{"context": "simpcity.cr|post:9", "title": "Summer Beach", "use": "Beach Day 2026"}])
        (downloads / "Lala Kudo" / "taken.jpg").write_bytes(b"y")
        self.assertFalse(store.rename(item, "taken.jpg", corrections)["ok"])

    def test_names_are_windows_safe(self):
        from job_store import clean_filename, strip_number

        self.assertEqual(clean_filename('a<b>:c?.jpg', ".jpg"), "a-b--c-.jpg")
        self.assertEqual(clean_filename("CON", ".jpg"), "_CON.jpg")
        self.assertEqual(clean_filename("Beach", ".jpg"), "Beach.jpg")
        self.assertEqual(strip_number("Beach Day-004", 4), "Beach Day")
        self.assertEqual(strip_number("004_Beach Day", 4), "Beach Day")
        self.assertEqual(strip_number("Beach 2004", 4), "Beach 2004")


def _box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(body), kind) + body


def _ebml(ident: int, body: bytes) -> bytes:
    raw = ident.to_bytes((ident.bit_length() + 7) // 8, "big")
    return raw + bytes([0x80 | len(body)]) + body if len(body) < 127 else raw + b"\x40" + bytes([len(body)]) + body


class TitlesInsideFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_each_format(self):
        title = "Beach Sunset Clip".encode()
        mp4 = _box(b"ftyp", b"isom\x00\x00\x02\x00") + _box(b"moov", _box(b"udta", _box(b"meta", b"\x00" * 4 + _box(
            b"ilst", _box(b"\xa9nam", _box(b"data", b"\x00\x00\x00\x01\x00\x00\x00\x00" + title))))))
        mkv = b"\x1a\x45\xdf\xa3\x80" + bytes.fromhex("18538067") + b"\x01\xff\xff\xff\xff\xff\xff\xff" + \
            _ebml(0x1549A966, _ebml(0x7BA9, b"Night Drive"))
        frame = b"\x03Ocean Waves"
        mp3 = b"ID3\x03\x00\x00" + bytes([0, 0, 0, 10 + len(frame)]) + b"TIT2" + struct.pack(">I", len(frame)) + b"\x00\x00" + frame
        comment = b"TITLE=Ocean Waves Flac"
        vorbis = struct.pack("<I", 3) + b"lib" + struct.pack("<I", 1) + struct.pack("<I", len(comment)) + comment
        flac = b"fLaC" + bytes([0x84]) + len(vorbis).to_bytes(3, "big") + vorbis
        png_text = b"Title\x00Garden Walk"
        png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4s", len(png_text), b"tEXt") + png_text + b"\x00" * 4 + \
            struct.pack(">I4s", 0, b"IDAT") + struct.pack(">I", zlib.crc32(b"IDAT"))
        xp = "Pool Party".encode("utf-16-le") + b"\x00\x00"
        ifd = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1) + struct.pack("<HHII", 0x9C9B, 1, len(xp), 26) + \
            struct.pack("<I", 0) + xp
        app1 = b"Exif\x00\x00" + ifd
        jpeg = b"\xff\xd8\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xda"
        archive = self.dir / "a.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Summer Set/01.jpg", "x")
            z.writestr("Summer Set/02.jpg", "y")
        for path, want in ((self.write("a.mp4", mp4), "Beach Sunset Clip"), (self.write("b.mkv", mkv), "Night Drive"),
                           (self.write("c.mp3", mp3), "Ocean Waves"), (self.write("d.flac", flac), "Ocean Waves Flac"),
                           (self.write("e.png", png), "Garden Walk"), (self.write("f.jpg", jpeg), "Pool Party"),
                           (archive, "Summer Set")):
            self.assertEqual(media_tags.read_title(path)[0], want, path.name)

    def test_encoder_names_and_broken_files_give_nothing(self):
        tool = _box(b"ftyp", b"isom") + _box(b"moov", _box(b"udta", _box(b"\xa9nam", struct.pack(">HH", 13, 0) + b"Lavf58.29.100")))
        self.assertEqual(media_tags.read_title(self.write("t.mp4", tool)), ("", ""))
        self.assertEqual(media_tags.read_title(self.write("x.mp4", b"\x00\x00\x00\xffmoov" + b"\x01" * 20)), ("", ""))
        self.assertEqual(media_tags.read_title(self.write("y.jpg", b"\xff\xd8\xff\xe1\x00")), ("", ""))
        self.assertEqual(media_tags.read_title(self.dir / "missing.mp3"), ("", ""))

    def test_only_unnamed_files_are_renamed_from_tags(self):
        mp3 = self.write("a9c82fa8-007.mp3", b"ID3\x03\x00\x00\x00\x00\x00\x16TIT2\x00\x00\x00\x0c\x00\x00\x03Ocean Waves")
        smart_naming.UNNAMED.clear()
        self.assertEqual(smart_naming.embedded_name("https://x/a.mp3", mp3, AUTO), ("", ""))
        smart_naming.UNNAMED["https://x/a.mp3"] = 7
        self.assertEqual(smart_naming.embedded_name("https://x/a.mp3", mp3, AUTO), ("Ocean Waves-007.mp3", "audio tag"))


class Accuracy(unittest.TestCase):
    """The labelled sets (see tests/naming_accuracy.py). A floor, so a change that loses accuracy fails."""

    def test_accuracy_floor(self):
        accuracy = importlib.import_module("naming_accuracy")
        import naming_dataset
        import naming_holdout
        import naming_holdout2

        for cases, floor in ((naming_dataset.CASES, 0.95), (naming_holdout.CASES, 0.95), (naming_holdout2.CASES, 0.9)):
            accuracy.CASES = cases
            v1 = accuracy.accuracy(accuracy.v1)
            v2 = accuracy.accuracy(accuracy.v2)
            self.assertGreaterEqual(v2["correct"] / len(cases), floor)
            self.assertEqual(v2["wrong_association"], 0)
            self.assertGreater(v2["correct"], v1["correct"])

    def test_resume_keeps_every_name(self):
        accuracy = importlib.import_module("naming_accuracy")
        jobs = accuracy.collisions_and_resume(accuracy.v2)
        self.assertEqual(jobs["suffix"]["collisions"], 0)
        self.assertEqual(jobs["suffix"]["resume_same"], jobs["suffix"]["files"])


if __name__ == "__main__":
    unittest.main()
