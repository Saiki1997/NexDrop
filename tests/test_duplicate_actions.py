"""3.1 duplicate actions: what happens to a new download with the same content as an earlier file.
Nothing is ever deleted; History follows every move or rename."""

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dup_actions  # noqa: E402
from local_server import LocalServer  # noqa: E402
from test_jobs_at_once import collect, start_session  # noqa: E402


class Moves(unittest.TestCase):
    def test_set_aside_and_rename_never_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "album").mkdir()
            new = root / "album" / "pic.jpg"
            new.write_bytes(b"new")
            (root / "_duplicates" / "album").mkdir(parents=True)
            (root / "_duplicates" / "album" / "pic.jpg").write_bytes(b"older aside")
            moved = dup_actions.set_aside(new, root)
            self.assertEqual(moved, root / "_duplicates" / "album" / "pic (2).jpg")
            self.assertEqual((root / "_duplicates" / "album" / "pic.jpg").read_bytes(), b"older aside")
            again = root / "x.jpg"
            again.write_bytes(b"x")
            (root / "x (duplicate).jpg").write_bytes(b"taken")
            self.assertEqual(dup_actions.rename_copy(again).name, "x (duplicate) (2).jpg")

    def test_keep_and_ask_touch_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a.jpg", root / "b.jpg"
            a.write_bytes(b"1")
            b.write_bytes(b"1")
            for action in ("keep", "ask", "replace"):
                rows = dup_actions.apply(action, [(str(b), str(a))], root, None)
                self.assertEqual(rows[0]["done"], "kept")
            self.assertTrue(a.exists() and b.exists())


class InARun(unittest.TestCase):
    def run_twice(self, action):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()  # Windows CI: the temp folder is an 8.3 short name
        appdata, folder = base / "appdata", base / "dl"
        stamp = time.time_ns()
        with LocalServer() as server:
            first = server.url("ok", f"orig{stamp}.jpg", size=120_000)
            copy = server.url("ok", f"copy{stamp}.jpg", size=120_000, **{"as": f"orig{stamp}.jpg"})
            one = collect({0: start_session(appdata, 0, [first], folder)})[0]
            two = collect({0: start_session(appdata, 0, [copy], folder, duplicateAction=action)})[0]
        return appdata, folder, one, two, stamp

    def test_skip_moves_the_new_copy_aside_and_history_follows(self):
        appdata, folder, one, two, stamp = self.run_twice("skip")
        self.assertEqual(two[-1]["saved"], 1, two[-6:])
        aside = list((folder / "_duplicates").rglob(f"copy{stamp}*.jpg"))
        self.assertEqual(len(aside), 1, [e for e in two if e.get("type") == "log"][-6:])
        self.assertEqual(list(folder.rglob(f"orig{stamp}*.jpg")).__len__(), 1)
        conn = sqlite3.connect(appdata / "jobs.sqlite")
        paths = [r[0] for r in conn.execute("SELECT destination FROM items WHERE url LIKE ?", (f"%copy{stamp}%",))]
        conn.close()
        self.assertEqual(paths, [str(aside[0])])

    def test_ask_lists_the_pairs_for_the_window(self):
        _appdata, folder, _one, two, stamp = self.run_twice("ask")
        events = [e for e in two if e.get("type") == "duplicates"]
        self.assertEqual(len(events), 1)
        self.assertTrue(Path(events[0]["items"][0]["of"]).name.startswith(f"orig{stamp}"))
        self.assertEqual(len(list(folder.rglob("*.jpg"))), 2)  # both kept until the person decides


if __name__ == "__main__":
    unittest.main()
