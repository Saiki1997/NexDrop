"""3.1 naming preview: every file gets its proposed name, and nothing is downloaded or recorded."""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_server import LocalServer  # noqa: E402
from test_jobs_at_once import collect, start_session  # noqa: E402


class NamingPreview(unittest.TestCase):
    def preview(self, **extra):
        with tempfile.TemporaryDirectory() as tmp, LocalServer() as server:
            base = Path(tmp)
            stamp = time.time_ns()
            urls = [server.url("ok", f"IMG_{stamp}_{i}.jpg", size=50_000) for i in range(3)]
            events = collect({0: start_session(base / "appdata", 0, urls, base / "dl", previewNames=True, **extra)})[0]
            files = [p for p in (base / "dl").rglob("*") if p.is_file()]
            return events, files, sum(server.hits.values()), (base / "appdata"), stamp

    def test_names_without_downloading(self):
        events, files, hits, appdata, stamp = self.preview(smartNames=True)
        preview = [e for e in events if e.get("type") == "naming-preview"]
        self.assertEqual(len(preview), 1, events[-5:])
        items = preview[0]["items"]
        self.assertEqual([i["sequence"] for i in items], [1, 2, 3])
        for index, item in enumerate(items):
            self.assertEqual(item["original"], f"IMG_{stamp}_{index}.jpg")
            self.assertTrue(item["name"].endswith(f"-{index + 1:03}.jpg"), item["name"])
            self.assertIn("source", item)
            self.assertIn("confidence", item)
        self.assertEqual(files, [])
        self.assertEqual(hits, 0)  # direct file links are named without asking the server
        self.assertFalse((appdata / "jobs.sqlite").exists())
        self.assertFalse((appdata / "runs.json").exists())
        self.assertEqual(events[-1]["type"], "finished")
        self.assertEqual(events[-1]["saved"], 0)

    def test_template_changes_the_proposed_names(self):
        events, _files, _hits, _appdata, stamp = self.preview(smartNames=True, nameTemplate="{original} by {site}", numbering="off")
        items = next(e for e in events if e.get("type") == "naming-preview")["items"]
        # {site} is the second-to-last part of the host: "0" for 127.0.0.1
        self.assertEqual([i["name"] for i in items], [f"IMG_{stamp}_{n} by 0.jpg" for n in range(3)])

if __name__ == "__main__":
    unittest.main()
