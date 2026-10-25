"""SimpCity pacing and backoff, moved Coomer hosts and the SimpCity-only proxy (from the 2026-10-25 19:15 run log)."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "engine"))

from cyberdrop_dl.clients.http import HTTPClient  # noqa: E402
from cyberdrop_dl.exceptions import ScrapeError  # noqa: E402
from cyberdrop_dl.url_objects import AbsoluteHttpURL  # noqa: E402

bridge = importlib.import_module("bridge")


class Guarded(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = HTTPClient.raw_request
        self.calls = []
        self.answers = []
        self.messages = []
        bridge.emit = self.messages.append
        bridge._SEEN.clear()
        bridge.GENTLE = bridge._GentleSite("SimpCity", gap=0.05, slots=2)
        bridge.GENTLE.min_pause = 0.01

        test = self

        @contextlib.asynccontextmanager
        async def fake(client, url, method="GET", **kwargs):
            test.calls.append((str(url), time.monotonic(), kwargs))
            status, headers = test.answers.pop(0) if test.answers else (200, {})
            yield SimpleNamespace(status=status, headers=headers)

        HTTPClient.raw_request = fake

    def tearDown(self):
        HTTPClient.raw_request = self.original

    async def get(self, url, **kwargs):
        async with HTTPClient.raw_request(object(), AbsoluteHttpURL(url), **kwargs) as resp:
            return resp.status

    async def test_dead_coomer_party_hosts_go_to_coomer_st(self):
        bridge._install_http_guard("", "all")
        await self.get("https://c6.coomer.party/data/7a/10/7a10.jpg?f=a.jpg")
        await self.get("https://kemono.su/data/aa/bb/x.png")
        self.assertEqual([c[0] for c in self.calls],
                         ["https://coomer.st/data/7a/10/7a10.jpg?f=a.jpg", "https://kemono.cr/data/aa/bb/x.png"])
        self.assertTrue(any("coomer.st" in m["message"] for m in self.messages))

    async def test_simpcity_requests_are_spaced_out(self):
        bridge._install_http_guard("", "all")
        await asyncio.gather(*(self.get(f"https://simpcity.cr/attachments/{n}") for n in range(4)))
        starts = [c[1] for c in self.calls]
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(all(gap >= 0.045 for gap in gaps), gaps)

    async def test_other_sites_are_not_slowed(self):
        bridge._install_http_guard("", "all")
        await asyncio.gather(*(self.get(f"https://bunkr.cr/f/{n}") for n in range(4)))
        starts = [c[1] for c in self.calls]
        self.assertLess(starts[-1] - starts[0], 0.04)

    async def test_429_pauses_then_retries(self):
        bridge._install_http_guard("", "all")
        self.answers = [(429, {"Retry-After": "0"}), (200, {})]
        self.assertEqual(await self.get("https://simpcity.cr/threads/x.1/page-2"), 200)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(any("asked to slow down" in m["message"] for m in self.messages))
        self.assertEqual(bridge.GENTLE.strikes, 0)

    async def test_stops_contacting_simpcity_after_repeated_limits(self):
        bridge._install_http_guard("", "all")
        self.answers = [(429, {"Retry-After": "0"})] * 8
        self.assertEqual(await self.get("https://simpcity.cr/a"), 429)
        self.assertEqual(await self.get("https://simpcity.cr/b"), 429)
        self.assertTrue(bridge.GENTLE.blocked)
        made = len(self.calls)
        with self.assertRaises(ScrapeError):
            await self.get("https://simpcity.cr/c")
        self.assertEqual(len(self.calls), made)
        self.assertTrue(any("stops contacting it" in m["message"] for m in self.messages))

    async def test_a_plain_503_is_not_a_rate_limit(self):
        bridge._install_http_guard("", "all")
        self.answers = [(503, {})]
        self.assertEqual(await self.get("https://simpcity.cr/a"), 503)
        self.assertEqual(len(self.calls), 1)

    async def test_proxy_only_for_simpcity(self):
        bridge._install_http_guard("http://127.0.0.1:8080", "simpcity")
        await self.get("https://simpcity.cr/a")
        await self.get("https://bunkr.cr/f/a")
        self.assertEqual(self.calls[0][2].get("proxy"), "http://127.0.0.1:8080")
        self.assertNotIn("proxy", self.calls[1][2])


class FlareSolverrProxy(unittest.TestCase):
    def test_simpcity_bot_check_is_solved_through_the_proxy(self):
        from cyberdrop_dl.clients import flaresolverr

        original = flaresolverr.Request.build
        try:
            bridge._install_http_guard("http://127.0.0.1:8080", "simpcity")
            config = SimpleNamespace(wait=0, use_session=True, proxy=None)
            simp = flaresolverr.Request.build("request.get", AbsoluteHttpURL("https://simpcity.cr/"), config, wait=None, session="s1")
            other = flaresolverr.Request.build("request.get", AbsoluteHttpURL("https://tikwm.com/"), config, wait=None, session="s1")
        finally:
            flaresolverr.Request.build = original
        self.assertEqual(simp.payload["proxy"], {"url": "http://127.0.0.1:8080"})
        self.assertNotIn("session", simp.payload)
        self.assertEqual(other.payload.get("session"), "s1")
        self.assertNotIn("proxy", other.payload)


class Helpers(unittest.TestCase):
    def test_retry_after(self):
        self.assertEqual(bridge.retry_after_seconds("120"), 120)
        self.assertIsNone(bridge.retry_after_seconds(""))
        self.assertAlmostEqual(bridge.retry_after_seconds("Sun, 25 Oct 2026 12:00:30 GMT", now=1792929600.0), 30, delta=1)

    def test_backoff_doubles_without_retry_after(self):
        site = bridge._GentleSite("SimpCity")
        self.assertEqual([site.limited(None) for _ in range(3)], [30, 60, 120])
        self.assertEqual(site.limited(5000), 900)

    def test_session_cookies_round_trip_without_overriding_the_users_file(self):
        jar = [_morsel(".simpcity.cr", "xf_session", "abc"), _morsel(".simpcity.cr", "xf_user", "old"),
               _morsel(".bunkr.cr", "other", "x")]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "simpcity.txt"
            self.assertEqual(bridge.save_site_cookies(_Jar(jar), path), 2)
            lines = bridge.saved_site_cookie_lines({("simpcity.cr", "xf_user")}, path)
        self.assertEqual([line.split("\t")[5] for line in lines], ["xf_session"])


def _morsel(domain, key, value):
    from http.cookies import SimpleCookie

    cookie = SimpleCookie()
    cookie[key] = value
    cookie[key]["domain"] = domain
    cookie[key]["path"] = "/"
    return cookie[key]


class _Jar(list):
    _expirations = {}


if __name__ == "__main__":
    unittest.main()
