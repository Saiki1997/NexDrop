"""3.1 network settings: the site connection test, per-site download limits and the retry policy."""

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import net_check  # noqa: E402
from local_server import LocalServer  # noqa: E402


class NetCheck(unittest.TestCase):
    def test_reachable_site_reports_each_step(self):
        with LocalServer() as server:
            result = net_check.check(server.url("ok", "a.bin", size=10))
        self.assertTrue(result["ok"], result)
        self.assertEqual([s["step"] for s in result["steps"]], ["DNS lookup", "Connect", "Request"])
        self.assertEqual(result["status"], 200)
        self.assertTrue(all(isinstance(s["ms"], int) for s in result["steps"]))

    def test_missing_page_still_counts_as_answering(self):
        with LocalServer() as server:
            result = net_check.check(server.url("gone", "x"))
        self.assertTrue(result["ok"])
        self.assertIn("404", result["steps"][-1]["detail"])

    def test_closed_port_fails_at_connect(self):
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        result = net_check.check(f"http://127.0.0.1:{port}/")
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"][-1]["step"], "Connect")
        self.assertFalse(result["steps"][-1]["ok"])

    def test_unknown_host_fails_at_dns(self):
        result = net_check.check("https://nexdrop-test.invalid/")
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"][0]["step"], "DNS lookup")

    def test_blocked_address_is_not_contacted(self):
        result = net_check.check("http://10.0.0.1/", blocked=lambda host: "Local network addresses are not allowed")
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"], [])
        self.assertIn("Local network", result["error"])

    def test_not_a_web_address(self):
        self.assertIn("error", net_check.check("ftp://site.test/"))


class SiteLimits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import bridge

        cls.bridge = bridge

    def test_configure_keeps_only_valid_entries(self):
        limits = self.bridge._SiteLimits()
        limits.configure({"www.Bunkr.cr": 2, "bad": "x", "zero.test": 0, "big.test": 99, "ok.test": "3"})
        self.assertEqual(limits.limits, {"bunkr.cr": 2, "ok.test": 3})

    def test_cdn_hosts_share_the_site_limit(self):
        limits = self.bridge._SiteLimits()
        limits.configure({"bunkr.cr": 2})

        async def run():
            self.assertIs(limits.get("c3.bunkr.cr"), limits.get("bunkr.cr"))
            self.assertIsNot(limits.get("notbunkr.cr"), limits.get("bunkr.cr"))
            gate = limits.get("media.bunkr.cr")
            active = peak = 0

            async def one():
                nonlocal active, peak
                async with limits.get("i.bunkr.cr"):
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0.01)
                    active -= 1

            await asyncio.gather(*(one() for _ in range(6)))
            self.assertEqual(peak, 2)
            return gate

        asyncio.run(run())

    def test_unlisted_site_is_not_limited(self):
        import contextlib

        limits = self.bridge._SiteLimits()
        self.assertIsInstance(limits.get("gofile.io"), contextlib.nullcontext)

    def test_retry_policy_changes_the_waits(self):
        from cyberdrop_dl.downloader import http

        old = http.RETRY_MAX_DELAY, http.RETRY_AFTER_CAP
        try:
            self.bridge._install_retry_policy(5, 30)

            class Error(Exception):
                retry = True
                retry_after = 500

            self.assertEqual(http.retry_delay(Error(), 1), 30.0)
            Error.retry_after = None
            self.assertLessEqual(http.retry_delay(Error(), 10), 5 * 1.25)
        finally:
            http.RETRY_MAX_DELAY, http.RETRY_AFTER_CAP = old


if __name__ == "__main__":
    unittest.main()
