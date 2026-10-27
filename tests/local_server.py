"""A local HTTP file server for engine tests (127.0.0.1, random port, its own thread).

Every file's bytes are a pure function of its name and version, so a test can check that a saved
file holds exactly one version of one file. Routes, all GET:

  /ok/<name>?size=N                 a file with Range, ETag and Last-Modified support
                                    (&as=<other> serves the bytes of <other> under this name)
  /trunc/<name>?size=N              no range support; announces N bytes, sends half, then drops (every time)
  /drop-once/<name>?size=N          the first request drops half way, later ones behave like /ok
  /flaky/<name>?size=N&fail=K       the first K requests answer 503 (Retry-After: 0), then /ok
  /limit/<name>?size=N&fail=K       the first K requests answer 429 (Retry-After: 1), then /ok
  /gone/<name>                      404
  /html/<name>                      200 application/octet-stream with an HTML login page as the body
  /badrange/<name>?size=N           first request drops half way; a range request is answered 206 from
                                    byte 0 (wrong); a plain request gets the whole file
  /changed/<name>?size=N            first request drops half way with ETag "v1"; afterwards the file is
                                    version 2 (ETag "v2") and If-Range: "v1" gets a 200 with all of v2
  /changed-ignore/<name>?size=N     like /changed, but the server ignores If-Range and answers 206 with v2

`server.hits[path]` counts the requests per path and `server.headers[path]` keeps the last
request headers.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import threading
from collections import Counter

from aiohttp import web


def content(name: str, size: int, version: int = 1) -> bytes:
    seed = hashlib.sha256(f"{name}:{version}".encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


class LocalServer:
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self.headers: dict[str, dict[str, str]] = {}
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    # ─── lifecycle ──────────────────────────────────────────────────────────

    def __enter__(self) -> "LocalServer":
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("test server did not start")
        return self

    def __exit__(self, *_) -> None:
        if self._loop and self._runner:
            asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(10)
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)

    def url(self, route: str, name: str, **query) -> str:
        tail = "&".join(f"{k}={v}" for k, v in query.items())
        return f"http://127.0.0.1:{self.port}/{route}/{name}" + (f"?{tail}" if tail else "")

    def _serve(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        app.router.add_get("/{route}/{name}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        self._loop.run_until_complete(self._runner.setup())
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        site = web.SockSite(self._runner, sock)
        self._loop.run_until_complete(site.start())
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    # ─── routes ─────────────────────────────────────────────────────────────

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        route, name = request.match_info["route"], request.match_info["name"]
        key = f"{route}/{name}"
        self.hits[key] += 1
        self.headers[key] = dict(request.headers)
        n = self.hits[key]
        size = int(request.query.get("size", "200000"))
        fail = int(request.query.get("fail", "1"))
        if route == "gone":
            return web.Response(status=404, text="not found")
        if route == "html":
            body = b"<!DOCTYPE html><html><head><title>Sign in</title></head><body>log in</body></html>"
            return web.Response(status=200, body=body, content_type="application/octet-stream")
        if route == "flaky" and n <= fail:
            return web.Response(status=503, headers={"Retry-After": "0"}, text="busy")
        if route == "limit" and n <= fail:
            return web.Response(status=429, headers={"Retry-After": "1"}, text="slow down")
        if route == "trunc":  # no range support, and every answer stops half way
            return await self._send(request, content(name, size), etag='"t1"', drop_at=size // 2, ranges=False)
        if route == "drop-once":
            return await self._send(request, content(name, size), etag='"d1"', drop_at=size // 2 if n == 1 else None)
        if route == "badrange":
            data = content(name, size)
            if n == 1:
                return await self._send(request, data, etag=None, drop_at=size // 2)
            if request.headers.get("Range"):
                return await self._send(request, data, etag=None, force_range_start=0)
            return await self._send(request, data, etag=None, ranges=False)
        if route in ("changed", "changed-ignore"):
            if n == 1:
                return await self._send(request, content(name, size, 1), etag='"v1"', drop_at=size // 2)
            new = content(name, size, 2)
            if route == "changed":
                if request.headers.get("If-Range") and request.headers.get("If-Range") != '"v2"':
                    return await self._send(request, new, etag='"v2"', ranges=False)
                return await self._send(request, new, etag='"v2"')
            return await self._send(request, new, etag='"v2"')  # ignores If-Range
        same = request.query.get("as", name)  # /ok/<name>?as=<other> serves <other>'s bytes
        return await self._send(request, content(same, size), etag=f'"{same}-{size}"')

    async def _send(self, request: web.Request, data: bytes, *, etag: str | None, drop_at: int | None = None,
                    ranges: bool = True, force_range_start: int | None = None) -> web.StreamResponse:
        size = len(data)
        start = 0
        status = 200
        headers = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes",
                   "Last-Modified": "Mon, 26 Oct 2026 10:00:00 GMT"}
        if etag:
            headers["ETag"] = etag
        wanted = request.headers.get("Range", "")
        if ranges and wanted.startswith("bytes="):
            start = int(wanted[6:].split("-")[0] or 0)
            if start >= size:
                return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            if force_range_start is not None:
                start = force_range_start
            status = 206
            headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
        body = data[start:]
        headers["Content-Length"] = str(len(body))
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        if drop_at is not None and drop_at - start > 0:
            await response.write(body[: drop_at - start])
            await asyncio.sleep(0.5)  # let the client read what was sent before the drop
            request.transport.close()  # the client sees a broken transfer
            return response
        for offset in range(0, len(body), 65536):
            await response.write(body[offset:offset + 65536])
        await response.write_eof()
        return response
