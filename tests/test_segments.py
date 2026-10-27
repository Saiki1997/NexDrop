"""Multi-segment downloads (3.1): a large file over a few connections, only when it is safe.

Unit tests for the plan, the eligibility rules and the merge, plus one engine run against the
local test server: a file split into parts, a server that answers a part wrongly (falls back to
one stream), a part that drops half way (resumes from its own size), and splitting turned off.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_server import LocalServer, content  # noqa: E402
from test_engine_integrity import by_url, run_bridge  # noqa: E402

from cyberdrop_dl.clients import resume_validators, segments  # noqa: E402

MB = 1024 * 1024
OK_HEADERS = {"Accept-Ranges": "bytes", "ETag": '"a1"'}


class Plan(unittest.TestCase):
    def test_ranges_cover_the_file_without_gaps(self):
        for size, parts in ((100, 3), (64 * MB, 4), (64 * MB + 7, 4)):
            layout = segments.plan(size, parts)
            self.assertEqual(len(layout), parts)
            self.assertEqual(layout[0][0], 0)
            self.assertEqual(layout[-1][1], size - 1)
            for (a, b), (c, _) in zip(layout, layout[1:]):
                self.assertEqual(b + 1, c)

    def test_small_files_and_mode_off_use_one_stream(self):
        self.assertEqual(segments.parts_for(10 * MB, 4), 1)
        self.assertEqual(segments.parts_for(500 * MB, 0), 1)
        self.assertEqual(segments.parts_for(500 * MB, 1), 1)
        self.assertEqual(segments.parts_for(500 * MB, 4), 4)
        self.assertEqual(segments.parts_for(segments.MIN_SIZE, 8), segments.MIN_SIZE // segments.MIN_PART)


class Eligible(unittest.TestCase):
    def setUp(self):
        segments._no_split.clear()

    def check(self, headers, size=200 * MB, impersonated=False, url="https://cdn.test/a.mp4"):
        return segments.eligible(url, "cdn.test", headers, size, impersonated=impersonated)

    def test_needs_ranges_and_a_validator(self):
        self.assertTrue(self.check(OK_HEADERS))
        self.assertTrue(self.check({"Accept-Ranges": "bytes", "Last-Modified": "Mon, 26 Oct 2026 10:00:00 GMT"}))
        self.assertFalse(self.check({"Accept-Ranges": "none", "ETag": '"a1"'}))
        self.assertFalse(self.check({"ETag": '"a1"'}))
        self.assertFalse(self.check({"Accept-Ranges": "bytes", "ETag": 'W/"weak"'}))
        self.assertFalse(self.check({"Accept-Ranges": "bytes"}))

    def test_never_compressed_impersonated_small_or_refused(self):
        self.assertFalse(self.check({**OK_HEADERS, "Content-Encoding": "gzip"}))
        self.assertFalse(self.check(OK_HEADERS, impersonated=True))
        self.assertFalse(self.check(OK_HEADERS, size=MB))
        segments.forget_link("https://cdn.test/a.mp4?token=1")
        self.assertFalse(self.check(OK_HEADERS, url="https://cdn.test/a.mp4?token=2"))

    def test_host_that_asked_to_slow_down_gets_one_stream(self):
        old = segments.host_ok
        segments.host_ok = lambda host: host != "cdn.test"
        try:
            self.assertFalse(self.check(OK_HEADERS))
        finally:
            segments.host_ok = old


class PartChecks(unittest.TestCase):
    def test_only_the_exact_range_of_the_same_file_is_accepted(self):
        good = {"Content-Range": "bytes 100-199/1000", "ETag": '"a1"'}
        segments.check_part(good, 206, 100, 199, 1000, {"etag": '"a1"'})
        for headers, status in (
            (good, 200),
            ({"Content-Range": "bytes 0-199/1000"}, 206),
            ({"Content-Range": "bytes 100-299/1000"}, 206),
            ({"Content-Range": "bytes 100-199/2000"}, 206),
            ({"Content-Range": "bytes 100-199/1000", "ETag": '"a2"'}, 206),
        ):
            with self.assertRaises(segments.SegmentError):
                segments.check_part(headers, status, 100, 199, 1000, {"etag": '"a1"'})


class LayoutAndMerge(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.part = Path(self.dir.name) / "v.mp4.part"
        self.data = os.urandom(1000)
        self.layout = segments.plan(1000, 3)

    def tearDown(self):
        self.dir.cleanup()

    def write_parts(self):
        a, b = self.layout[0]
        self.part.write_bytes(self.data[a:b + 1])
        for index in range(1, 3):
            a, b = self.layout[index]
            segments.seg_file(self.part, index).write_bytes(self.data[a:b + 1])

    def test_layout_survives_in_the_sidecar_beside_the_validators(self):
        resume_validators.save(self.part, {"ETag": '"a1"'}, 1000, "https://x.test/v.mp4")
        segments.save_layout(self.part, self.layout)
        self.assertEqual(segments.load_layout(self.part), self.layout)
        self.assertEqual(resume_validators.if_range(self.part), '"a1"')
        segments.discard(self.part)
        self.assertEqual(segments.load_layout(self.part), [])
        self.assertEqual(resume_validators.load(self.part)["etag"], '"a1"')

    def test_merge_joins_parts_in_order(self):
        self.write_parts()
        segments.save_layout(self.part, self.layout)
        segments.merge(self.part, self.layout)
        self.assertEqual(self.part.read_bytes(), self.data)
        self.assertEqual(list(Path(self.dir.name).glob("*.seg*")), [])

    def test_merge_refuses_a_short_part(self):
        self.write_parts()
        seg = segments.seg_file(self.part, 2)
        seg.write_bytes(seg.read_bytes()[:-1])
        with self.assertRaises(segments.SegmentError):
            segments.merge(self.part, self.layout)
        self.assertEqual(len(self.part.read_bytes()), self.layout[0][1] + 1)  # nothing appended

    def test_resume_asks_part_one_again_up_to_its_own_end(self):
        segments.save_layout(self.part, self.layout)
        end0 = self.layout[0][1]
        self.assertEqual(segments.resume_range(self.part, 100), (100, f"bytes=100-{end0}"))
        self.assertEqual(segments.resume_range(self.part, end0 + 1), (end0, f"bytes={end0}-{end0}"))


class EngineSegments(unittest.TestCase):
    SIZE = 3 * MB + 123

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.tmp.name)
        cls.server = LocalServer().__enter__()
        s = cls.server
        stamp = str(time.time_ns())
        cls.urls = {
            "split": s.url("ok", f"sp{stamp}.mp4", size=cls.SIZE),
            "wrong": s.url("seg-wrong", f"sw{stamp}.mp4", size=cls.SIZE),
            "drop": s.url("seg-drop", f"sd{stamp}.mp4", size=cls.SIZE),
        }
        cls.events = run_bridge(list(cls.urls.values()), cls.folder, segments=4, segmentMinMB=1)
        cls.saved = by_url(cls.events, "saved")
        cls.failed = by_url(cls.events, "failed")
        off_folder = cls.folder / "off"
        cls.off_url = s.url("seg-drop", f"so{stamp}.mp4", size=cls.SIZE)
        cls.off_events = run_bridge([cls.off_url], off_folder, segments=0, segmentMinMB=1)

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()
        cls.tmp.cleanup()

    def name(self, key):
        return self.urls[key].split("/")[4].split("?")[0]

    def saved_bytes(self, key):
        event = self.saved.get(self.urls[key])
        self.assertIsNotNone(event, f"{key} not saved; failed: {self.failed.get(self.urls[key])}")
        return Path(event["path"]).read_bytes()

    def test_large_file_downloads_in_parts_and_is_whole(self):
        self.assertEqual(self.saved_bytes("split"), content(self.name("split"), self.SIZE))
        self.assertGreaterEqual(self.server.hits[f"ok/{self.name('split')}"], 4)
        ranged = [h for k, h in self.server.headers.items() if k == f"ok/{self.name('split')}"]
        self.assertTrue(ranged and ranged[0].get("If-Range") == f'"{self.name("split")}-{self.SIZE}"', ranged)

    def test_wrong_part_answer_falls_back_to_one_stream(self):
        self.assertEqual(self.saved_bytes("wrong"), content(self.name("wrong"), self.SIZE))

    def test_dropped_part_resumes_from_its_own_size(self):
        self.assertEqual(self.saved_bytes("drop"), content(self.name("drop"), self.SIZE))
        resumed = self.server.ranges.get(f"seg-drop/{self.name('drop')}", [])
        layout = segments.plan(self.SIZE, 4)
        starts = {f"bytes={a}-{b}" for a, b in layout[1:]}
        self.assertTrue(any(r not in starts for r in resumed), resumed)  # one part asked from its middle

    def test_no_part_files_or_sidecars_left(self):
        leftovers = [p.name for p in self.folder.rglob("*") if ".seg" in p.name or p.name.endswith((".nxmeta", ".part"))]
        self.assertEqual(leftovers, [])

    def test_splitting_off_uses_one_connection(self):
        saved = by_url(self.off_events, "saved")
        self.assertIn(self.off_url, saved)
        name = self.off_url.split("/")[4].split("?")[0]
        self.assertEqual(self.server.ranged[f"seg-drop/{name}"], 0)
        self.assertEqual(Path(saved[self.off_url]["path"]).read_bytes(), content(name, self.SIZE))


if __name__ == "__main__":
    unittest.main()
