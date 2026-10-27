"""Several jobs at once (3.1): two sessions running side by side share history files under a
lock, keep their own cookie copies and engine logs, and never mark each other as interrupted."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_server import LocalServer  # noqa: E402
from state_lock import RunningMarker  # noqa: E402

COUNTER = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[2])
    from state_lock import shared_state
    folder = Path(sys.argv[1])
    for _ in range(40):
        with shared_state(folder):
            path = folder / "n.json"
            n = json.loads(path.read_text()) if path.exists() else 0
            path.write_text(json.dumps(n + 1))
""")


class SharedState(unittest.TestCase):
    def test_two_processes_never_lose_an_update(self):
        with tempfile.TemporaryDirectory() as folder:
            procs = [subprocess.Popen([sys.executable, "-c", COUNTER, folder, str(ROOT / "desktop")]) for _ in range(2)]
            for proc in procs:
                self.assertEqual(proc.wait(60), 0)
            self.assertEqual(json.loads((Path(folder) / "n.json").read_text()), 80)

    def test_running_marker_sees_live_sessions_and_clears_dead_ones(self):
        with tempfile.TemporaryDirectory() as folder:
            other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                (Path(folder) / "running").mkdir()
                (Path(folder) / "running" / "2.pid").write_text(str(other.pid))
                (Path(folder) / "running" / "3.pid").write_text("999999")
                with RunningMarker(Path(folder), 0) as marker:
                    self.assertEqual(marker.others(), [other.pid])
                    self.assertFalse((Path(folder) / "running" / "3.pid").exists())
                self.assertFalse((Path(folder) / "running" / "0.pid").exists())
            finally:
                other.kill()
                other.wait()


def start_session(appdata: Path, slot: int, urls: list[str], folder: Path, **extra) -> subprocess.Popen:
    env = {**os.environ, "NEXDROP_APPDATA": str(appdata), "NEXDROP_SLOT": str(slot), "PYTHONIOENCODING": "utf-8",
           "NEXDROP_FLARE_MANAGED": "1", "NEXDROP_RETRY_WAIT": "0"}
    config = {"urls": urls, "downloadsDir": str(folder), "orderMode": "parallel", "maxDownloads": 4, "perHost": 4,
              "smartNames": False, "flareSolver": False, "redownload": True, "autoRetry": False, **extra}
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "session_harness.py")], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    proc.stdin.write(json.dumps(config) + "\n")
    proc.stdin.flush()
    return proc


def collect(procs: dict[int, subprocess.Popen]) -> dict[int, list[dict]]:
    """Read both sessions' events at once. stdin stays open until a session reports "finished",
    because the session takes a closed stdin as "stop"."""
    import threading

    events: dict[int, list[dict]] = {slot: [] for slot in procs}

    def read(slot: int, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            events[slot].append(event)
            if event.get("type") == "finished":
                break
        proc.stdin.close()
        proc.wait(60)

    threads = [threading.Thread(target=read, args=item) for item in procs.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(300)
    return events


class TwoJobsTogether(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.appdata = base / "appdata"
        cls.server = LocalServer().__enter__()
        stamp = time.time_ns()
        cls.urls = {
            0: [cls.server.url("ok", f"a{stamp}-{i}.jpg", size=150_000) for i in range(3)],
            1: [cls.server.url("ok", f"b{stamp}-{i}.jpg", size=150_000) for i in range(3)]
            + [cls.server.url("gone", f"g{stamp}.jpg")],  # its failure goes to the second job's own CSV log
        }
        procs = {slot: start_session(cls.appdata, slot, cls.urls[slot], base / f"dl{slot}") for slot in (0, 1)}
        cls.events = collect(procs)

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()
        cls.tmp.cleanup()

    def finished(self, slot):
        done = [e for e in self.events[slot] if e.get("type") == "finished"]
        self.assertTrue(done, self.events[slot][-5:])
        return done[-1]

    def test_both_jobs_save_their_files(self):
        for slot in (0, 1):
            self.assertEqual(self.finished(slot)["saved"], 3, self.events[slot][-8:])

    def test_history_keeps_both_jobs_links(self):
        history = set(json.loads((self.appdata / "completed-urls.json").read_text(encoding="utf-8")))
        for slot in (0, 1):
            self.assertTrue(set(self.urls[slot][:3]) <= history)
        runs = json.loads((self.appdata / "runs.json").read_text(encoding="utf-8"))
        self.assertTrue(all(url in runs for slot in (0, 1) for url in self.urls[slot]))

    def test_neither_job_is_marked_interrupted(self):
        conn = sqlite3.connect(self.appdata / "jobs.sqlite")
        statuses = [row[0] for row in conn.execute("SELECT status FROM jobs")]
        conn.close()
        notes = [e.get("message") for slot in (0, 1) for e in self.events[slot] if "istory" in str(e.get("message"))]
        self.assertEqual(len(statuses), 2, notes)
        self.assertNotIn("interrupted", statuses)

    def test_second_job_has_its_own_engine_logs_and_no_cookies_are_left(self):
        csv = list((self.appdata / "logs" / "job1").rglob("*.csv"))
        self.assertTrue(csv, list((self.appdata / "logs").rglob("*")))
        self.assertTrue(any("-job1" in p.name for p in (self.appdata / "logs" / "runs").glob("*.log")))
        cookies = self.appdata / "cookies"
        leftovers = [p.name for p in cookies.iterdir()] if cookies.is_dir() else []
        self.assertEqual([n for n in leftovers if n.startswith(("run", "engine"))], [])
        self.assertEqual(list((self.appdata / "running").glob("*.pid")), [])


if __name__ == "__main__":
    unittest.main()
