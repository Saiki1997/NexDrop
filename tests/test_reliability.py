"""3.0 P0 checks for the desktop side: failure categories, direct downloads that stop early or
return a web page, expired links matched after the retry pass, and the job database."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cairn_session as session  # noqa: E402
import error_classes  # noqa: E402
from job_store import JobStore  # noqa: E402
from local_server import LocalServer, content  # noqa: E402


class Categories(unittest.TestCase):
    CASES = [
        ("404 Not Found", "", "not_found"),
        ("410 Gone", "https://example.com/a.jpg", "not_found"),
        ("403 Forbidden", "https://c3.scdn.st/storage/abc.mp4", "expired_url"),
        ("HTTP 403", "https://cdn.test/a.mp4?expires=1793000000&signature=x", "expired_url"),
        ("403 Forbidden", "https://simpcity.cr/attachments/1", "auth_required"),
        ("429 Too Many Requests", "", "rate_limited"),
        ("503 Service Unavailable", "", "server_busy"),
        ("999 Timeout - Download timeout reached", "", "timeout"),
        ("Connection timeout to host https://n3.coomer.st", "", "timeout"),
        ("ClientConnectorDNSError: getaddrinfo failed", "", "dns"),
        ("Server unreachable", "", "unreachable"),
        ("Corrupted File - Expected 300,000 bytes but only got 12 bytes", "", "corrupted"),
        ("Resume Mismatch - Server sent bytes from 0", "", "corrupted"),
        ("Incomplete transfer: got 10 of 20 bytes", "", "corrupted"),
        ("Got a web page instead of the file: 'Sign in'", "", "web_page"),
        ("Bot check page (DDoS-Guard)", "", "bot_check"),
        ("509 Bandwidth Limit Exceeded", "", "quota"),
        ("Insufficient Free Space", "", "disk_full"),
        ("[Errno 13] Permission denied: 'D:\\x'", "", "permission"),
        ("Password Protected", "", "password"),
        ("Something odd", "", "other"),
    ]

    def test_each_failure_gets_one_category(self):
        for reason, url, expected in self.CASES:
            self.assertEqual(error_classes.classify(reason, url), expected, reason)

    def test_retry_pass_only_takes_recoverable_categories(self):
        self.assertTrue(session.is_transient("403 Forbidden", "https://c3.scdn.st/storage/abc.mp4"))
        self.assertTrue(session.is_transient("Corrupted File - Expected 3 bytes but only got 1 bytes"))
        for reason in ("404 Not Found", "403 Forbidden", "Password Protected", "Insufficient Free Space"):
            self.assertFalse(session.is_transient(reason), reason)

    def test_every_category_has_a_label_and_an_action(self):
        for category in {c for _, _, c in self.CASES}:
            info = error_classes.describe(category)
            self.assertTrue(info["label"] and info["action"], category)


class DirectDownloads(unittest.TestCase):
    """The fallback for direct links the engine leaves alone (session.http_save)."""

    def setUp(self):
        self.server = LocalServer().__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self._blocked = session.blocked_reason
        session.blocked_reason = lambda host: None  # the test server is on 127.0.0.1
        session.FILES = session.FileTable()

    def tearDown(self):
        session.blocked_reason = self._blocked
        self.server.__exit__()
        self.tmp.cleanup()

    def test_whole_file_is_saved(self):
        url = self.server.url("ok", "whole.bin", size=70_000)
        result = session.http_save(url, Path(self.tmp.name))
        saved = Path(self.tmp.name) / result["filename"]
        self.assertEqual(saved.read_bytes(), content("whole.bin", 70_000))

    def test_connection_that_stops_early_is_not_saved(self):
        url = self.server.url("trunc", "short.bin", size=300_000)
        with self.assertRaises(Exception) as caught:
            session.http_save(url, Path(self.tmp.name))
        self.assertEqual(error_classes.classify(str(caught.exception)), "corrupted")
        self.assertEqual([p.name for p in Path(self.tmp.name).rglob("*") if p.is_file()], [])

    def test_web_page_instead_of_an_image_is_not_saved(self):
        url = self.server.url("html", "photo.jpg")
        with self.assertRaises(ValueError) as caught:
            session.http_save(url, Path(self.tmp.name))
        self.assertIn("web page", str(caught.exception))
        self.assertEqual([p.name for p in Path(self.tmp.name).rglob("*") if p.is_file()], [])


class RefreshedLinks(unittest.TestCase):
    def test_expired_link_saved_under_a_fresh_link_is_matched_by_file_name(self):
        session.FILES = session.FileTable()
        old = "https://c3.scdn.st/storage/Beach-Day-abc123.mp4"
        session.FILES.update(old, state="failed", reason="403 Forbidden")
        session.FILES.update("https://other.test/kept.jpg", state="done")
        before = {"https://other.test/kept.jpg"}
        session.FILES.update("https://c7.scdn.st/storage/Beach-Day-abc123.mp4", state="done", name="Beach Day-001.mp4")
        found = session._match_refreshed_links([old], before)
        self.assertEqual(found[old]["name"], "Beach Day-001.mp4")

    def test_short_or_ambiguous_names_are_not_matched(self):
        session.FILES = session.FileTable()
        session.FILES.update("https://a.test/1.jpg", state="failed")
        session.FILES.update("https://b.test/1.jpg", state="done")
        self.assertEqual(session._match_refreshed_links(["https://a.test/1.jpg"], set()), {})


class JobDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_locked_database_is_not_mistaken_for_a_broken_one(self):
        JobStore(self.db).close()
        holder = sqlite3.connect(self.db, timeout=0)
        holder.execute("PRAGMA journal_mode = DELETE")
        holder.execute("BEGIN EXCLUSIVE")
        try:
            store = JobStore.__new__(JobStore)
            store.path = self.db
            self.assertNotEqual(store._health(), "corrupt")
        finally:
            holder.rollback()
            holder.close()
        self.assertTrue(self.db.exists())

    def test_broken_database_is_kept_aside_with_its_journal(self):
        self.db.write_bytes(b"this is not a database" * 100)
        (self.db.parent / "jobs.sqlite-wal").write_bytes(b"x")
        store = JobStore(self.db)
        self.assertIsNotNone(store.moved_aside)
        self.assertTrue(store.moved_aside.exists())
        # its journal never reaches the fresh database (moved aside, or dropped by SQLite itself)
        self.assertFalse((self.db.parent / "jobs.sqlite-wal").read_bytes() == b"x" if (self.db.parent / "jobs.sqlite-wal").exists() else False)
        self.assertEqual(store.search("", status="all")["total"], 0)
        store.close()

    def test_file_events_are_batched_and_flushed_at_the_end(self):
        store = JobStore(self.db)
        job = store.start_job(["https://x.test/thread"], "D:/dl")
        store._last_commit = time.monotonic()  # inside one batch window
        for i in range(50):
            store.note(job, {"url": f"https://x.test/{i}.jpg", "event": "failure", "status": "503 Service Unavailable",
                             "category": "server_busy"})
        reader = sqlite3.connect(self.db)
        self.assertLess(reader.execute("SELECT COUNT(*) FROM items").fetchone()[0], 50)
        store.finish_job(job, "completed_with_errors")
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM items").fetchone()[0], 50)
        self.assertEqual(reader.execute("SELECT DISTINCT error_category FROM items").fetchone()[0], "server_busy")
        reader.close()
        store.close()

    def test_history_queries_use_indexes(self):
        store = JobStore(self.db)
        plan = " ".join(str(r[-1]) for r in store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM items WHERE status = 'completed' ORDER BY updated_at DESC LIMIT 200"))
        self.assertIn("items_status_updated", plan)
        store.close()

    def test_old_database_gets_new_columns(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                status TEXT NOT NULL, source_urls TEXT NOT NULL, download_dir TEXT NOT NULL, stop_reason TEXT NOT NULL DEFAULT '');
            CREATE TABLE items (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, url TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
                sequence INTEGER NOT NULL DEFAULT 0, filename TEXT NOT NULL DEFAULT '', destination TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued', bytes_read INTEGER NOT NULL DEFAULT 0, bytes_total INTEGER NOT NULL DEFAULT 0,
                retries INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL, UNIQUE (job_id, url));
            INSERT INTO jobs VALUES (1, 0, 0, 'completed', 'https://x.test', 'D:/dl', '');
            INSERT INTO items (job_id, url, status, updated_at) VALUES (1, 'https://x.test/a.jpg', 'completed', 0);
        """)
        conn.close()
        store = JobStore(self.db)
        self.assertEqual(store.search("", status="all")["total"], 1)
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(items)")}
        self.assertIn("error_category", columns)
        self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], 4)
        store.close()



class HistoryTools(unittest.TestCase):
    """3.0 History: filters, missing files, CSV export and the site health summary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = JobStore(self.dir / "jobs.sqlite")
        job = self.store.start_job(["https://simpcity.cr/threads/a.1/"], str(self.dir))
        kept = self.dir / "kept.jpg"
        kept.write_bytes(b"x")
        rows = [
            {"url": "https://bunkr.cr/kept.jpg", "host": "bunkr.cr", "event": "success", "name": "kept.jpg",
             "destination": str(kept), "bytes_total": 1},
            {"url": "https://bunkr.cr/gone.mp4", "host": "bunkr.cr", "event": "success", "name": "gone.mp4",
             "destination": str(self.dir / "gone.mp4"), "bytes_total": 5},
            {"url": "https://bunkr.cr/busy.mp4", "host": "bunkr.cr", "event": "failure", "status": "503 Service Unavailable",
             "category": "server_busy"},
            {"url": "https://gofile.io/x.zip", "host": "gofile.io", "event": "failure", "status": "=HYPERLINK(evil) 404"},
        ]
        for row in rows:
            self.store.note(job, row)
        self.store.flush()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_type_and_missing_filters(self):
        self.assertEqual([i["name"] for i in self.store.search(kind="video")["items"]], ["gone.mp4"])
        missing = self.store.search(status="missing")
        self.assertEqual([i["name"] for i in missing["items"]], ["gone.mp4"])
        self.assertTrue(missing["items"][0]["missing"])
        self.assertEqual(self.store.search(status="all", days=1)["total"], 4)

    def test_failed_items_carry_their_category(self):
        failed = {i["url"]: i for i in self.store.search(status="failed")["items"]}
        self.assertEqual(failed["https://bunkr.cr/busy.mp4"]["label"], "Server busy")
        self.assertEqual(failed["https://gofile.io/x.zip"]["category"], "not_found")  # classified when not stored

    def test_csv_export_is_safe_to_open_in_a_spreadsheet(self):
        import csv

        dest = self.dir / "history.csv"
        self.assertEqual(self.store.export_csv({"status": "all"}, dest), 4)
        with dest.open(encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0][:3], ["date", "result", "file"])
        details = [r[8] for r in rows[1:]]
        self.assertIn("'=HYPERLINK(evil) 404", details)
        self.assertIn("saved, file missing", [r[1] for r in rows[1:]])

    def test_site_health(self):
        sites = {s["host"]: s for s in self.store.health(30)}
        self.assertEqual((sites["bunkr.cr"]["saved"], sites["bunkr.cr"]["failed"]), (2, 1))
        self.assertEqual(sites["bunkr.cr"]["reasons"][0]["label"], "Server busy")
        self.assertEqual(sites["gofile.io"]["state"], "failing")
        self.assertEqual(sites["bunkr.cr"]["state"], "unstable")


class CookieCopies(unittest.TestCase):
    NETSCAPE = "# Netscape HTTP Cookie File\n.simpcity.cr\tTRUE\t/\tTRUE\t1900000000\txf_user\tsecret\n"

    def test_saved_site_cookies_exist_only_during_the_run(self):
        folder = session.prepare_cookies("", [], {"simpcity": self.NETSCAPE, "../evil": self.NETSCAPE, "empty": ""})
        files = sorted(p.name for p in Path(folder).iterdir())
        self.assertEqual(files, ["account-___evil.txt", "account-simpcity.txt"])
        session.clear_run_cookies()
        self.assertFalse(session.RUN_COOKIES.exists())

    def test_session_cookie_file_round_trip(self):
        sys.path.insert(0, str(ROOT / "engine"))
        import bridge

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "simpcity.txt"
            bridge.write_protected(path, self.NETSCAPE)
            self.assertEqual(bridge.read_protected(path), self.NETSCAPE)
            if sys.platform == "win32":
                self.assertNotIn(b"secret", path.read_bytes())  # DPAPI-encrypted for this Windows user
            path.write_text(self.NETSCAPE, encoding="utf-8")  # a 2.x plain file still reads
            self.assertEqual(bridge.read_protected(path), self.NETSCAPE)


class EngineSafeguards(unittest.TestCase):
    """The 3.0 bridge additions: per-site pause, slot recovery and duplicate-content reports."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "engine"))
        import bridge

        cls.bridge = bridge

    def test_site_pauses_after_repeated_server_errors_and_reopens_on_success(self):
        breaker = self.bridge._SiteBreaker(threshold=3, cooldown=60, ceiling=600)
        for _ in range(2):
            self.assertEqual(breaker.failed("bunkr", "503 Service Unavailable", now=0), 0)
        self.assertEqual(breaker.failed("bunkr", "503 Service Unavailable", now=0), 60)
        self.assertEqual(breaker.blocked_for("bunkr", now=30), 30)
        self.assertEqual(breaker.blocked_for("gofile", now=30), 0)  # other sites carry on
        self.assertEqual(breaker.failed("bunkr", "Site Paused", now=30), 0)  # paused files don't count
        # the probe after the pause fails: paused again, twice as long
        self.assertEqual(breaker.failed("bunkr", "502 Bad Gateway", now=61), 120)
        breaker.ok("bunkr")
        self.assertEqual(breaker.blocked_for("bunkr", now=500), 0)
        self.assertEqual(breaker.failed("bunkr", "500", now=500), 0)  # streak starts over

    def test_other_errors_break_the_streak(self):
        breaker = self.bridge._SiteBreaker(threshold=2)
        breaker.failed("x", "503", now=0)
        breaker.failed("x", "404 Not Found", now=0)
        self.assertEqual(breaker.failed("x", "503", now=0), 0)
        self.assertEqual(breaker.failed("x", "503", now=0), 60)

    def test_site_paused_is_a_temporary_server_problem(self):
        reason = "Site Paused: bunkr is paused for 60 s after repeated server errors"
        self.assertEqual(error_classes.classify(reason), "server_busy")
        self.assertTrue(error_classes.is_retryable("server_busy"))

    def test_slowed_down_server_gets_connections_back_after_quiet_spells(self):
        slots = self.bridge._ServerSlots()
        slots.per_server = 3
        slot = slots.get("s1.cdn")
        slots.back_off("s1.cdn")
        self.assertEqual(slot.limit, 1)
        since = slots.reduced["s1.cdn"]
        slots.ok("s1.cdn", now=since + 10)
        self.assertEqual(slot.limit, 1)  # too soon
        slots.ok("s1.cdn", now=since + slots.RECOVER_AFTER)
        self.assertEqual(slot.limit, 2)
        slots.ok("s1.cdn", now=since + slots.RECOVER_AFTER + 10)
        self.assertEqual(slot.limit, 2)  # each step waits its own quiet spell
        slots.ok("s1.cdn", now=since + 2 * slots.RECOVER_AFTER)
        self.assertEqual(slot.limit, 3)
        self.assertNotIn("s1.cdn", slots.reduced)

    def test_duplicate_content_is_reported_and_both_files_kept(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            first, second, other_size = tmp / "a.mp4", tmp / "b.mp4", tmp / "c.mp4"
            first.write_bytes(b"same" * 100)
            second.write_bytes(b"same" * 100)
            other_size.write_bytes(b"same" * 50)
            db = tmp / "cyberdrop.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE hash (folder TEXT, download_filename TEXT, hash_type TEXT, hash TEXT)")
            for path in (first, second, other_size):
                conn.execute("INSERT INTO hash VALUES (?, ?, 'xxh128', 'h1')", (str(path.parent), path.name))
            conn.commit()
            conn.close()
            finder = self.bridge._Duplicates()
            finder.conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            self.assertEqual(finder.find("h1", second, 400), first)
            self.assertIsNone(finder.find("h1", other_size, 200))  # same hash row, different size
            self.assertIsNone(finder.find("h2", second, 400))
            self.assertIsNone(finder.find(None, second, 400))
            finder.conn.close()
            self.assertTrue(first.exists() and second.exists())


if __name__ == "__main__":
    unittest.main()
