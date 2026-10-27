"""3.1 "try the job again later": the finished event counts the failed files a later try can fix."""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_server import LocalServer  # noqa: E402
from test_jobs_at_once import collect, start_session  # noqa: E402


class RetryLaterCount(unittest.TestCase):
    def test_busy_server_counts_and_gone_does_not(self):
        stamp = time.time_ns()
        with tempfile.TemporaryDirectory() as tmp, LocalServer() as server:
            base = Path(tmp)
            urls = [server.url("flaky", f"busy{stamp}.jpg", size=1000, fail=99), server.url("gone", f"gone{stamp}.jpg"),
                    server.url("ok", f"fine{stamp}.jpg", size=1000)]
            events = collect({0: start_session(base / "appdata", 0, urls, base / "dl", autoRetry=False, attempts=1)})[0]
            finished = events[-1]
            self.assertEqual(finished["type"], "finished", events[-5:])
            self.assertEqual((finished["saved"], finished["failed"]), (1, 2))
            self.assertEqual(finished["retryLater"], 1)  # the 503 file; a deleted file won't come back


if __name__ == "__main__":
    unittest.main()
