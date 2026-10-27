"""3.1 storage: destination rules send files from a site or of a type to their own folder, and a run
does not start when its folder can't be written."""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_server import LocalServer  # noqa: E402
from test_jobs_at_once import collect, start_session  # noqa: E402


class DestinationRules(unittest.TestCase):
    def test_videos_go_to_their_own_folder(self):
        with tempfile.TemporaryDirectory() as tmp, LocalServer() as server:
            base = Path(tmp)
            stamp = time.time_ns()
            urls = [server.url("ok", f"pic{stamp}.jpg", size=40_000), server.url("ok", f"clip{stamp}.mp4", size=60_000)]
            events = collect({0: start_session(base / "appdata", 0, urls, base / "dl",
                                               destinationRules=[{"type": "video", "folder": str(base / "videos")}])})[0]
            self.assertEqual(events[-1]["saved"], 2, events[-6:])
            self.assertEqual([p.name for p in (base / "dl").rglob("*.jpg")], [p.name for p in (base / "dl").rglob(f"pic{stamp}*.jpg")])
            self.assertEqual(len(list((base / "dl").rglob("*.mp4"))), 0)
            videos = list((base / "videos").rglob(f"clip{stamp}*.mp4"))
            self.assertEqual(len(videos), 1)
            self.assertEqual(videos[0].stat().st_size, 60_000)

    def test_rules_are_checked(self):
        import cairn_session

        rules = cairn_session.destination_rules([
            {"site": "www.Bunkr.cr", "folder": "/x"}, {"type": "video", "folder": "/v"}, {"type": "exe", "folder": "/e"},
            {"site": "not a site", "folder": "/n"}, {"site": "gofile.io"}, "junk",
        ])
        self.assertEqual(rules, [{"site": "bunkr.cr", "folder": "/x"}, {"type": "video", "folder": "/v"}])


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
                                               destinationRules=[{"type": "image", "folder": str(base / "a-file" / "pics")}])})[0]
            errors = [e["message"] for e in events if e.get("type") == "error"]
            self.assertTrue(errors and "Can't save files" in errors[0], events[-5:])
            self.assertEqual(server.hits, {})

if __name__ == "__main__":
    unittest.main()
