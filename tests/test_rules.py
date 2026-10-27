"""3.1 download rules: validation, matching, and real runs where rules skip, include, size-check and
redirect files. Also the destination check before a run."""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules  # noqa: E402
from local_server import LocalServer  # noqa: E402
from test_jobs_at_once import collect, start_session  # noqa: E402


def rule(then, *when, **extra):
    return {"enabled": True, "then": then, "when": [dict(zip(("field", "op", "value"), w)) for w in when], **extra}


class Logic(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(rules.problems(rule("skip", ("type", "is", "video"))), [])
        self.assertTrue(rules.problems(rule("skip")))
        self.assertTrue(rules.problems(rule("folder", ("type", "is", "video"))))  # no folder
        self.assertTrue(rules.problems(rule("folder", ("size", "over", "10"), folder="/x")))  # size can't pick a folder
        self.assertTrue(rules.problems(rule("skip", ("site", "is", "not a site"))))
        self.assertTrue(rules.problems(rule("skip", ("date", "before", "31/01/2024"))))
        self.assertTrue(rules.problems(rule("skip", ("size", "over", "-1"))))
        self.assertTrue(rules.problems(rule("explode", ("type", "is", "video"))))
        disabled = {**rule("skip", ("type", "is", "video")), "enabled": False}
        self.assertEqual(rules.clean([disabled, rule("skip"), rule("skip", ("type", "is", "image"))]), [rule("skip", ("type", "is", "image"))])

    def test_matching(self):
        fact = rules.facts("https://c3.bunkr.cr/v/clip.MP4", "clip.MP4", creator="Alice Smith", uploaded=1_700_000_000)
        self.assertEqual((fact["type"], fact["ext"], fact["site"]), ("video", "mp4", "c3.bunkr.cr"))
        self.assertTrue(rules.matches(rule("skip", ("site", "is", "bunkr.cr")), fact))
        self.assertFalse(rules.matches(rule("skip", ("site", "is", "unkr.cr")), fact))
        self.assertTrue(rules.matches(rule("skip", ("creator", "contains", "alice")), fact))
        self.assertTrue(rules.matches(rule("skip", ("name", "matches", "clip*")), fact))
        self.assertTrue(rules.matches(rule("skip", ("ext", "in", "mkv, .mp4")), fact))
        self.assertTrue(rules.matches(rule("skip", ("date", "before", "2024-01-01")), fact))
        self.assertIsNone(rules.matches(rule("skip", ("size", "over", "5")), fact))  # size not known yet

    def test_decisions(self):
        active = rules.clean([rule("only", ("type", "is", "video")), rule("skip", ("name", "matches", "*preview*")),
                              rule("folder", ("site", "is", "bunkr.cr"), folder="/videos"), rule("single", ("type", "is", "video"))])
        clip = rules.decide(active, rules.facts("https://bunkr.cr/clip.mp4", "clip.mp4"))
        self.assertEqual((clip["skip"], clip["folder"], clip["single"]), ("", "/videos", True))
        self.assertIn("only download", rules.decide(active, rules.facts("https://bunkr.cr/a.jpg", "a.jpg"))["skip"])
        self.assertIn("rule:", rules.decide(active, rules.facts("https://bunkr.cr/preview.mp4", "preview.mp4"))["skip"])
        sized = rules.clean([rule("skip", ("size", "over", "1"))])
        self.assertTrue(rules.decide(sized, rules.facts("https://x.test/a.mp4", "a.mp4"))["pending"])
        self.assertTrue(rules.decide(sized, rules.facts("https://x.test/a.mp4", "a.mp4", size=2 * 1024 * 1024))["skip"])


class LibraryPreview(unittest.TestCase):
    def test_what_rules_would_do_to_library_files(self):
        import library

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "library.sqlite"
            conn = library.connect(db)
            rows = [("a.jpg", "https://bunkr.cr/f/a", 1000, 0), ("b.mp4", "https://bunkr.cr/f/b", 5_000_000, 0),
                    ("preview-c.jpg", "https://x.test/c", 10, 0), ("gone.mp4", "https://bunkr.cr/g", 10, 1)]
            for name, url, size, missing in rows:
                conn.execute("INSERT INTO files (path, folder, name, ext, kind, size, mtime, url, missing) VALUES (?,?,?,?,?,?,?,?,?)",
                             (f"{tmp}/{name}", tmp, name, name.rsplit(".", 1)[1], "", size, 0, url, missing))
            conn.commit()
            conn.close()
            active = rules.clean([rule("skip", ("name", "matches", "preview*"), name="no previews"),
                                  rule("folder", ("site", "is", "bunkr.cr"), ("type", "is", "video"), folder="/v"),
                                  rule("single", ("size", "over", "1"))])
            self.assertTrue(all(not rules.problems(r) for r in active[:2]))
            out = rules.preview(active[:2], str(db))
            self.assertEqual((out["total"], out["skipped"], out["folder"]), (3, 1, 1))  # the missing file isn't counted
            self.assertEqual(out["examples"]["skipped"], ["preview-c.jpg"])
            self.assertEqual(out["byRule"]["no previews"], 1)
            self.assertEqual([p for p in Path(tmp).iterdir() if not p.name.startswith("library.sqlite")], [])  # read only


class InARun(unittest.TestCase):
    def test_skip_only_size_and_folder_rules(self):
        stamp = time.time_ns()
        with tempfile.TemporaryDirectory() as tmp, LocalServer() as server:
            base = Path(tmp)
            urls = {
                "keep": server.url("ok", f"keep{stamp}.jpg", size=20_000),
                "preview": server.url("ok", f"preview{stamp}.jpg", size=20_000),
                "big": server.url("ok", f"big{stamp}.jpg", size=300_000),
                "clip": server.url("ok", f"clip{stamp}.mp4", size=40_000),
                "doc": server.url("ok", f"doc{stamp}.zip", size=1_000),
            }
            active = [rule("only", ("type", "is", "image")), rule("only", ("type", "is", "video")),
                      rule("skip", ("name", "matches", "preview*"), name="no previews"), rule("skip", ("size", "over", "0.2")),
                      rule("folder", ("type", "is", "video"), folder=str(base / "videos"))]
            events = collect({0: start_session(base / "appdata", 0, list(urls.values()), base / "dl", rules=active)})[0]
            rows = {}
            for event in events:
                if event.get("type") == "files":
                    for row in event.get("items") or []:
                        rows[row.get("url")] = row
            finished = events[-1]
            self.assertEqual((finished["type"], finished["saved"]), ("finished", 2), events[-6:])
            self.assertEqual(len(list((base / "dl").rglob(f"keep{stamp}*.jpg"))), 1)
            self.assertEqual(len(list((base / "videos").rglob(f"clip{stamp}*.mp4"))), 1)  # the folder rule
            self.assertEqual(list((base / "dl").rglob("*.mp4")), [])
            for key in ("preview", "big", "doc"):
                self.assertEqual(list(base.rglob(f"{key}{stamp}*")), [], key)
            reasons = {key: (rows.get(url) or {}).get("reason", "") for key, url in urls.items()}
            self.assertEqual(reasons["preview"], "rule: no previews")
            self.assertIn("only download", reasons["doc"])
            self.assertIn("over 0.2 MB", reasons["big"])  # checked once the server gave the size
            self.assertEqual(server.hits.get(f"/ok/preview{stamp}.jpg", 0), 0)  # skipped before any request


class Destination(unittest.TestCase):
    def test_folder_that_cannot_be_made_stops_the_run(self):
        import cairn_session

        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "a-file"
            blocker.write_text("x")
            self.assertIn("Can't save files", cairn_session.check_destinations([blocker / "downloads"]))

    def test_writable_folder_is_fine(self):
        import cairn_session

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cairn_session.check_destinations([Path(tmp) / "new"]), "")
            self.assertEqual(list((Path(tmp) / "new").iterdir()), [])  # the write test leaves nothing behind

    def test_run_stops_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp, LocalServer() as server:
            base = Path(tmp)
            (base / "a-file").write_text("x")
            events = collect({0: start_session(base / "appdata", 0, [server.url("ok", "x.jpg", size=10)], base / "dl",
                                               rules=[rule("folder", ("type", "is", "image"), folder=str(base / "a-file" / "pics"))])})[0]
            errors = [e["message"] for e in events if e.get("type") == "error"]
            self.assertTrue(errors and "Can't save files" in errors[0], events[-5:])
            self.assertEqual(server.hits, {})


if __name__ == "__main__":
    unittest.main()
