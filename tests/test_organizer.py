"""3.1 storage organizer: preview, conflicts, no overwrite, History and Library follow, undo."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))

import library  # noqa: E402
import organizer  # noqa: E402
from job_store import JobStore  # noqa: E402


class Organizer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "dl"
        (self.root / "Loose Files").mkdir(parents=True)
        (self.root / "keep-empty").mkdir()
        self.jobs = base / "jobs.sqlite"
        self.journal = base / "organize.jsonl"
        store = JobStore(self.jobs)
        job = store.start_job(["https://bunkr.cr/a/x"], str(self.root))
        self.files = {}
        for name, creator, album in (("a.jpg", "alice", "Beach"), ("b.jpg", "alice", "Beach"), ("c.mp4", "bob", ""), ("d.jpg", "", "")):
            path = self.root / "Loose Files" / name
            path.write_bytes(name.encode() * 100)
            self.files[name] = path
            store.note(job, {"url": f"https://cdn.bunkr.cr/{name}", "host": "bunkr.cr", "name": name, "destination": str(path),
                             "event": "success", "bytes_total": path.stat().st_size,
                             "naming": {"original": name, "identity": {"creator": creator, "album": album}}})
        store.finish_job(job, "completed")
        store.close()
        self.conn = library.connect(base / "library.sqlite")
        library.sync_history(self.conn, self.jobs)
        self.roots = [str(self.root)]

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def plan(self, **body):
        return organizer.plan(self.conn, {"root": str(self.root), "roots": self.roots, **body})

    def apply(self, planned):
        return organizer.apply(self.conn, {"plan": planned, "jobs": str(self.jobs), "journal": str(self.journal), "roots": self.roots})

    def history_paths(self):
        conn = sqlite3.connect(self.jobs)
        paths = {Path(r[0]).name: r[0] for r in conn.execute("SELECT destination FROM items")}
        conn.close()
        return paths

    def test_preview_changes_nothing(self):
        planned = self.plan(folders="{creator}/{album}")
        self.assertTrue(planned["ok"], planned)
        targets = {Path(m["from"]).name: str(Path(m["to"]).relative_to(self.root)) for m in planned["moves"]}
        self.assertEqual(targets["a.jpg"], str(Path("alice/Beach/a.jpg")))
        self.assertEqual(targets["c.mp4"], str(Path("bob/c.mp4")))  # no album: that level is left out
        self.assertEqual(targets["d.jpg"], "d.jpg")  # nothing known: straight under the root
        self.assertTrue(all(p.exists() for p in self.files.values()))

    def test_apply_moves_and_history_and_library_follow_then_undo(self):
        result = self.apply(self.plan(folders="{creator}/{album}"))
        self.assertEqual((result["moved"], result["skipped"]), (4, 0), result)
        moved = self.root / "alice" / "Beach" / "a.jpg"
        self.assertTrue(moved.is_file())
        self.assertEqual(self.history_paths()["a.jpg"], str(moved))
        self.assertEqual(self.conn.execute("SELECT path FROM files WHERE name = 'a.jpg'").fetchone()[0], str(moved))
        batch = organizer.batches(self.journal)[0]
        self.assertEqual(batch["count"], 4)
        undone = organizer.undo(self.conn, {"batch": batch["batch"], "jobs": str(self.jobs), "journal": str(self.journal)})
        self.assertEqual(undone["restored"], 4, undone)
        self.assertTrue(all(p.is_file() for p in self.files.values()))
        self.assertFalse((self.root / "alice").exists())  # folders the organize step made are gone again
        self.assertTrue((self.root / "keep-empty").is_dir())  # an empty folder that was there before stays
        self.assertEqual(self.history_paths()["a.jpg"], str(self.files["a.jpg"]))
        again = organizer.undo(self.conn, {"batch": batch["batch"], "jobs": str(self.jobs), "journal": str(self.journal)})
        self.assertFalse(again["ok"])

    def test_existing_files_are_never_overwritten(self):
        (self.root / "alice" / "Beach").mkdir(parents=True)
        (self.root / "alice" / "Beach" / "a.jpg").write_bytes(b"mine")
        planned = self.plan(folders="{creator}/{album}")
        conflict = next(m for m in planned["moves"] if Path(m["from"]).name == "a.jpg")
        self.assertIn("already there", conflict["conflict"])
        result = self.apply(planned)
        self.assertEqual(result["moved"], 3)
        self.assertEqual((self.root / "alice" / "Beach" / "a.jpg").read_bytes(), b"mine")
        self.assertTrue(self.files["a.jpg"].is_file())

    def test_rename_pattern_and_shared_names(self):
        planned = self.plan(names="{creator} {number}")
        names = {Path(m["from"]).name: Path(m["to"]).name for m in planned["moves"]}
        # numbered per folder in saved order; a missing creator drops out with its separator
        self.assertEqual(names, {"a.jpg": "alice 001.jpg", "b.jpg": "alice 002.jpg", "c.mp4": "bob 003.mp4", "d.jpg": "004.jpg"})
        clash = self.plan(names="{site}")
        self.assertTrue(any("same name" in (m.get("conflict") or "") for m in clash["moves"]))

    def test_file_changed_after_preview_is_skipped(self):
        planned = self.plan(folders="{creator}")
        self.files["a.jpg"].write_bytes(b"changed")
        result = self.apply(planned)
        self.assertEqual(result["moved"], 3)
        self.assertTrue(self.files["a.jpg"].is_file())

    def test_bad_patterns_and_folders_are_refused(self):
        self.assertFalse(self.plan(folders="{nope}")["ok"])
        self.assertFalse(self.plan(names="fixed")["ok"])
        self.assertFalse(self.plan(folders="../{creator}")["ok"])
        self.assertFalse(organizer.plan(self.conn, {"root": "/somewhere/else", "roots": self.roots, "folders": "{creator}"})["ok"])

    def test_command_line_batches(self):
        import subprocess

        self.journal.write_text(json.dumps({"batch": "b1", "start": 1, "label": "x"}) + "\n")
        run = subprocess.run([sys.executable, str(ROOT / "desktop" / "organizer.py"), "batches", str(Path(self._tmp.name) / "library.sqlite")],
                             input=json.dumps({"journal": str(self.journal)}) + "\n", capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(run.stdout)["batches"][0]["batch"], "b1")


if __name__ == "__main__":
    unittest.main()
