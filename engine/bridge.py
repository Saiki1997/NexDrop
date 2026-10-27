"""Run the patched Cyberdrop-DL engine and print one JSON object per line."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORTABLE = ROOT / "appdata"
PORTABLE.mkdir(parents=True, exist_ok=True)
# Windows ignores XDG_* and otherwise writes to %APPDATA%\cyberdrop-dl.
os.environ["CDL_APPDATA_FOLDER"] = str(PORTABLE)
os.environ["XDG_CONFIG_HOME"] = str(PORTABLE / "config")
os.environ["XDG_DATA_HOME"] = str(PORTABLE / "data")
os.environ["XDG_CACHE_HOME"] = str(PORTABLE / "cache")
os.environ["XDG_STATE_HOME"] = str(PORTABLE / "state")
_ffmpeg = ROOT.parent / "runtime" / "ffmpeg"
if _ffmpeg.is_dir():
    os.environ["PATH"] = str(_ffmpeg) + os.pathsep + os.environ.get("PATH", "")
_template = ROOT / "user-config.yaml"
_installed = PORTABLE / "config.yaml"


def _config_version(text: str) -> int:
    match = re.search(r"NEXDROP-CONFIG (\d+)", text)
    return int(match.group(1)) if match else 0


if _template.is_file():
    current = _installed.read_text(encoding="utf-8") if _installed.is_file() else ""
    # A newer bundled config replaces the installed copy (2: no deprecated ssl_context, which cost 4 s per start).
    if _config_version(current) < _config_version(_template.read_text(encoding="utf-8")):
        shutil.copyfile(_template, _installed)
sys.path.insert(0, str(ROOT / "vendor"))


PROGRESS_INTERVAL = 0.25  # seconds between progress snapshots
_out_lock = threading.Lock()
_URL_RE = re.compile(r"https?://[^\s)\"'<>]+")
# "<prefix> Failed: <url> (<reason>)"  e.g. "Download Failed: https://x/y.jpg (404 Not Found) -> Referer: ..."
_FAIL_RE = re.compile(r"^(?P<prefix>.*?)\bFailed: (?P<url>https?://\S+) \((?P<reason>.*?)\)(?: -> Referer: (?P<referer>\S+))?\s*$")


_FILE_EVENTS = frozenset({"file_start", "file_end", "saved", "failed", "skipped", "retry"})
_SENSITIVE = ("password", "token", "auth", "cookie", "session", "sig", "key", "secret")


def shown_url(url: str, *, keep_fragment: bool = False) -> str:
    """The link as the window and history see it: without tokens or signatures.

    Same rule as the engine's source-order list, so a file's numbering, progress
    and result land on one row even when its link carries a one-time token
    (Telegram, many CDNs)."""
    from urllib.parse import parse_qsl, urlsplit, urlunsplit

    try:
        parts = urlsplit(str(url))
    except ValueError:
        return str(url)
    if not parts.query:
        query = ""
    else:
        query = "&".join(f"{name}={value}" for name, value in parse_qsl(parts.query, keep_blank_values=True)
                         if not any(token in name.lower() for token in _SENSITIVE))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment if keep_fragment else ""))


def emit(payload: dict) -> None:
    kind = payload.get("type")
    if kind in _FILE_EVENTS and payload.get("url") and str(payload["url"]).startswith("http"):
        # A skipped album item is reported by its message link plus #1, #2 ...
        payload = {**payload, "url": shown_url(payload["url"], keep_fragment=kind == "skipped")}
    elif kind == "progress" and payload.get("files"):
        payload = {**payload, "files": [{**row, "url": shown_url(row["url"])} if str(row.get("url") or "").startswith("http") else row
                                        for row in payload["files"]]}
    elif kind == "done" and payload.get("saved"):
        payload = {**payload, "saved": [{**row, "url": shown_url(row["url"])} if str(row.get("url") or "").startswith("http") else row
                                        for row in payload["saved"]]}
    line = json.dumps(payload, default=str, ensure_ascii=False) + "\n"
    with _out_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _leaf_errors(record: logging.LogRecord) -> str:
    """'(2 sub-exceptions)' hides the real reasons; list the distinct ones instead."""
    exc = record.exc_info[1] if record.exc_info else None
    found: list[str] = []

    def walk(error: BaseException) -> None:
        if isinstance(error, BaseExceptionGroup):
            for sub in error.exceptions:
                walk(sub)
        else:
            text = str(error).strip() or type(error).__name__
            if text not in found:
                found.append(text)

    if exc is not None:
        walk(exc)
    return "; ".join(found)[:200]


def _http_url(message: str) -> str | None:
    match = _URL_RE.search(message)
    return match.group(0).rstrip(").,;:") if match else None


_QUIET_PREFIXES = (
    "Deleted conflicting old ",
    "Unable to parse upload date",
    "Download finished:",
)
_QUIET_SUBSTRINGS = (
    "Skipping tweet ",
    "Unable to fetch additional PeerTube instances",
    "Falling back to thread based resolver",
)


class _LogBridge(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: C901, PLR0911, PLR0912
        try:
            message = " ".join(record.getMessage().split())
        except Exception:  # noqa: BLE001 - a malformed log record must never stop a download
            return
        if "Forum result " in message and "files=" in message:
            emit({"type": "forum_result", "message": message[message.index("Forum result "):]})
            return
        raw = record.getMessage()
        if raw.startswith("Source order\t"):
            parts = raw.split("\t")
            if len(parts) >= 3 and parts[1].isdigit():
                event = {"type": "order", "sequence": int(parts[1]), "url": parts[2], "name": parts[3] if len(parts) > 3 else ""}
                # Smart naming record for the job: folder, original name, where the name came from.
                for index, key in enumerate(("folder", "original", "source", "confidence", "strategy", "title", "parent", "naming"), 4):
                    if len(parts) > index:
                        event[key] = parts[index]
                if len(parts) > 12:
                    # Smart naming V2: the file's identity (post, album, creator, type), links without query strings
                    try:
                        identity = json.loads(parts[12])
                    except ValueError:
                        identity = None
                    if isinstance(identity, dict):
                        event["identity"] = identity
                emit(event)
            return
        if raw.startswith("Telegram needs sign-in\t"):
            parts = raw.split("\t")
            if len(parts) >= 3:
                emit({"type": "skipped", "url": parts[1], "reason": f"{parts[2]}: sign in to Telegram in Settings > Sign-ins"})
            return
        if raw.startswith("Telegram result\t"):
            parts = raw.split("\t")
            if len(parts) >= 3:
                emit({"type": "log", "level": "info", "message": f"Scanned t.me/{parts[1]}: {parts[2]}"})
            return
        if raw.startswith("Forum result\t"):
            emit({"type": "forum_result", "message": raw.replace("\t", " ")})
            return
        if "Unsupported URL:" in message:
            emit({"type": "unsupported", "url": message.split("Unsupported URL:", 1)[1].strip(), "message": message[:500]})
            return
        if message.startswith("Download skipped "):
            url = _http_url(message) or ""
            lower = message.lower()
            reason = ("already downloaded" if "before the pause" in lower
                      else "file type turned off in Settings" if "extension" in lower or "ignored by config" in lower
                      else "smaller than the minimum size" if "filesize" in lower
                      else "filtered out")
            if url:
                emit({"type": "skipped", "url": url, "reason": reason})
            return
        if "already been scrapped" in message:
            return  # a page from an earlier run, not a file
        if "already been downloaded" in message:
            url = _http_url(message)
            if url:
                reason = "same hash as an earlier file" if "hash" in message else "already downloaded"
                emit({"type": "skipped", "url": url, "reason": reason})
                return
        if message.startswith("Scrape Failed:"):
            if "sub-exceptions" in message:
                inner = _leaf_errors(record)
                if inner:
                    message = f"Scrape Failed: {_http_url(message) or ''} ({inner})"
            emit({"type": "scrape_failed", "url": _http_url(message) or "", "message": message[:500]})
            return
        if record.levelno >= logging.ERROR:
            failure = _FAIL_RE.match(message)
            if failure and "Scrape" not in failure.group("prefix"):
                emit({"type": "failed", "url": failure.group("url").rstrip(").,;"), "reason": failure.group("reason")[:200],
                      "referer": failure.group("referer") or ""})
                return
        if message.startswith("Retrying "):
            emit({"type": "retry", "url": _http_url(message) or "", "message": message[:300]})
            return
        if "Illegal key" in message and message.startswith("Unable to parse cookie"):
            return
        if "ssl_context" in message and "deprecated" in message:
            return
        if message.startswith(_QUIET_PREFIXES) or any(part in message for part in _QUIET_SUBSTRINGS):
            return
        if "Stopping pagination" in message and "empty responses" in message:
            message = "Twitter returned empty pages, so that profile walk stopped. Other links continue."
        if "Too many downloads queued" in message:
            # Normal back-pressure while downloads catch up; say it once per run.
            if _SEEN.get("queued"):
                return
            _SEEN["queued"] = True
            emit({"type": "log", "level": "info", "message": "Finding files faster than they download. Scanning pauses until download slots free up."})
            return
        if message.startswith("Found cookies for"):
            return  # summarised once by _filter_cookies; the full list is in the engine log
        if message.startswith("ffmpeg is not installed"):
            message = "ffmpeg was not found next to NexDrop. HLS video will fail. Other files still download."
        useful = record.levelno >= logging.WARNING or "No valid links" in message
        if not useful:
            return
        level = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "info"
        emit({"type": "log", "level": level, "message": message[:300]})


class _ProgressTracker:
    """Byte counters for every active file, emitted together at a fixed rate.

    Only files that moved since the last snapshot are sent, so parallel
    downloads all keep updating and the pipe is not flooded.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.files: dict[int, dict] = {}
        self.dirty: set[int] = set()
        self.total_bytes = 0
        self.last = 0.0

    def start(self, key: int, url: str, name: str, domain: str, total: int | None) -> None:
        now = time.monotonic()
        with self.lock:
            self.files[key] = {"url": url, "read": 0, "total": total, "t0": now, "last_t": now, "last_read": 0, "speed": 0.0}
        emit({"type": "file_start", "url": url, "name": name, "domain": domain.lower(), "total": total})

    def advance(self, key: int, amount: int) -> None:
        now = time.monotonic()
        with self.lock:
            row = self.files.get(key)
            if row is None:
                return
            row["read"] += amount
            self.total_bytes += amount
            self.dirty.add(key)
            if now - self.last < PROGRESS_INTERVAL:
                return
            self.last = now
            batch = [self._public(self.files[k], now) for k in self.dirty if k in self.files]
            self.dirty.clear()
        emit({"type": "progress", "files": batch, "bytes": self.total_bytes})

    def resume(self, key: int, amount: int) -> None:
        """Bytes a resumed file already had on disk: progress only, not run bytes or speed."""
        with self.lock:
            row = self.files.get(key)
            if row is None:
                return
            row["read"] += amount
            row["last_read"] += amount
            row["resumed"] = row.get("resumed", 0) + amount
            self.dirty.add(key)

    def end(self, key: int) -> None:
        with self.lock:
            row = self.files.pop(key, None)
            self.dirty.discard(key)
        if row is not None:
            emit({"type": "file_end", **self._public(row, time.monotonic()), "bytes": self.total_bytes})

    @staticmethod
    def _public(row: dict, now: float) -> dict:
        # Current speed: bytes since the last report, smoothed so the number doesn't jump around.
        span = now - row["last_t"]
        if span >= 0.2:
            instant = (row["read"] - row["last_read"]) / span
            row["speed"] = instant if row["speed"] == 0 else row["speed"] * 0.5 + instant * 0.5
            row["last_t"], row["last_read"] = now, row["read"]
        elif row["speed"] == 0:
            row["speed"] = (row["read"] - row.get("resumed", 0)) / max(now - row["t0"], 1e-3)
        return {"url": row["url"], "read": row["read"], "total": row["total"], "speed": int(row["speed"])}


TRACKER = _ProgressTracker()


class _Duplicates:
    """Finds an earlier file with the same content (xxh128 and size) as a new download.

    Report only: both files are kept. The engine has already stored the new file's hash."""

    def __init__(self) -> None:
        self.conn = None
        self.reported: set[frozenset[str]] = set()

    def find(self, xxh128: str | None, path: Path, size: int) -> Path | None:
        if not xxh128 or not size:
            return None
        try:
            if self.conn is None:
                import sqlite3

                db = PORTABLE / "cyberdrop.db"
                if not db.is_file():
                    return None
                self.conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5, check_same_thread=False)
            rows = self.conn.execute(
                "SELECT folder, download_filename FROM hash WHERE hash_type = 'xxh128' AND hash = ? LIMIT 20", (xxh128,)
            ).fetchall()
        except Exception:  # noqa: BLE001 - a duplicate check never costs a download
            return None
        here = os.path.normcase(str(path.resolve()))
        for folder, name in rows:
            other = Path(folder) / name
            try:
                if os.path.normcase(str(other.resolve())) == here or not other.is_file() or other.stat().st_size != size:
                    continue
            except OSError:
                continue
            return other
        return None


DUPLICATES = _Duplicates()
_SEEN: dict[str, bool] = {}


def _install_download_clearance() -> None:
    """Retry a download once through FlareSolverr when the site answers with a bot-check page.

    The engine already does this when scanning pages, but not for files. SimpCity
    attachments are behind DDoS-Guard: once its short-lived clearance cookies
    expire, every attachment came back as an HTML challenge page ("Invalid
    Content Type"). One FlareSolverr visit per site refreshes the clearance for
    all of its files, then the file is requested again.
    """
    import asyncio

    try:
        from cyberdrop_dl.clients import downloads as dl
        from cyberdrop_dl.exceptions import DDOSGuardError, DownloadError, InvalidContentTypeError
        from cyberdrop_dl.url_objects import AbsoluteHttpURL
    except Exception:  # noqa: BLE001
        return

    original_check = dl._check_response

    async def check_response(media_item, resp, resume_point):
        try:
            return await original_check(media_item, resp, resume_point)  # the resume point that applies
        except InvalidContentTypeError as error:
            title = ""
            try:
                text = (await resp.text())[:20000]
                match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
                title = " ".join(match.group(1).split())[:120] if match else ""
                if "ddos-guard" in text.lower() or "checking your browser" in text.lower():
                    raise DDOSGuardError(message=f"Bot check page{f' ({title})' if title else ''}") from error
            except (DDOSGuardError,):
                raise
            except Exception:  # noqa: BLE001
                pass
            if title:
                raise InvalidContentTypeError(message=f"Got a web page instead of the file: '{title}'") from error
            raise

    dl._check_response = check_response

    original_download = dl.DownloadClient._download
    locks: dict[str, asyncio.Lock] = {}
    cleared: dict[str, float] = {}

    async def download(self, domain, media_item):
        try:
            return await original_download(self, domain, media_item)
        except (InvalidContentTypeError, DDOSGuardError) as error:
            if _is_site_error_page(str(error)):
                raise  # the site's own error page (deleted or no permission), not a bot check
            http = self.http_client
            flare = http.flaresolverr
            url = await media_item.resolve()
            host = getattr(url, "host", "") or ""
            if not flare or getattr(flare, "is_down", False) or not host:
                raise
            lock = locks.setdefault(host, asyncio.Lock())
            async with lock:
                if time.monotonic() - cleared.get(host, 0) > 90:
                    emit({"type": "log", "level": "info", "message": f"Got a bot-check page from {host}. Clearing it with FlareSolverr, then retrying."})
                    try:
                        await http._flaresolverr_request(AbsoluteHttpURL(str(url.origin()) + "/"))
                    except Exception as error:  # noqa: BLE001
                        emit({"type": "log", "level": "warning", "message": f"FlareSolverr could not clear the bot check on {host}: {str(error)[:160]}"})
                        cleared[host] = time.monotonic()
                        raise
                    cleared[host] = time.monotonic()
            return await original_download(self, domain, media_item)

    async def download_with_backoff(self, domain, media_item):
        try:
            site = UNREACHABLE.site((await media_item.resolve()).host or "")
        except Exception:  # noqa: BLE001
            site = ""
        if UNREACHABLE.is_down(site):
            raise DownloadError("Server unreachable", f"{site}'s file servers could not be reached earlier in this run")
        wait = BREAKER.blocked_for(site)
        if wait:
            raise DownloadError("Site Paused", f"{site} is paused for {int(wait)} s after repeated server errors")
        try:
            host = (await media_item.resolve()).host or ""
        except Exception:  # noqa: BLE001
            host = ""
        try:
            result = await download(self, domain, media_item)
        except Exception as error:
            text = str(error)
            if "429" in text or "503" in text:
                SERVER_SLOTS.back_off(host)
            if UNREACHABLE.failed(site, text):
                emit({"type": "log", "level": "warning", "message": (
                    f"Can't connect to {site}'s file servers: {UNREACHABLE.limit} downloads in a row timed out before the server answered. "
                    f"Skipping the rest of {site}'s files this run, so they show as failed and Retry unfinished can get them later.")})
            opened = BREAKER.failed(site, text)
            if opened:
                emit({"type": "log", "level": "warning", "message": (
                    f"{site} failed {BREAKER.threshold} downloads in a row with server errors. Pausing it for {int(opened)} s; "
                    "other sites keep downloading. Its paused files are tried again by the retry pass.")})
            raise
        UNREACHABLE.ok(site)
        BREAKER.ok(site)
        SERVER_SLOTS.ok(host)
        return result

    dl.DownloadClient._download = download_with_backoff


# Hosts that moved. Links in old forum posts still point at the old names, which no longer resolve
# ("getaddrinfo failed"). The same /data/... path works on the new main domain.
_MOVED_HOSTS = (
    (re.compile(r"(?:^|\.)coomer\.(?:party|su)$"), "coomer.st"),
    (re.compile(r"(?:^|\.)kemono\.(?:party|su)$"), "kemono.cr"),
)
_GENTLE_HOSTS = ("simpcity.cr", "simpcity.su")


def moved_host(host: str) -> str | None:
    host = (host or "").lower()
    for pattern, new in _MOVED_HOSTS:
        if pattern.search(host):
            return new
    return None


def is_gentle_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == name or host.endswith("." + name) for name in _GENTLE_HOSTS)


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    """A Retry-After header as seconds: a number, or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


class _GentleSite:
    """Keeps SimpCity traffic under its rate limit so the IP address is not blocked.

    Every request to the site, page or attachment, waits its turn: at most `slots`
    at once and `gap` seconds between starts. A 429 (or a 503 that says when to
    come back) pauses the whole site, honouring Retry-After or doubling from 30 s,
    and the request is tried again. After `give_up` limits in a row the site is
    left alone for the rest of the run instead of being asked again and again.
    """

    def __init__(self, name: str, gap: float = 2.0, slots: int = 2, give_up: int = 5) -> None:
        self.name = name
        self.gap = gap
        self.slots = slots
        self.give_up = give_up
        self.next_start = 0.0
        self.cool_until = 0.0
        self.strikes = 0
        self.blocked = False
        self.min_pause = 10.0
        self.loop = None
        self.sem = None
        self.lock = None

    def _bind(self) -> None:
        import asyncio

        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop, self.sem, self.lock = loop, asyncio.Semaphore(self.slots), asyncio.Lock()

    async def turn(self) -> None:
        import asyncio

        async with self.lock:
            wait = max(self.next_start, self.cool_until) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.next_start = time.monotonic() + self.gap

    def limited(self, retry_after: float | None) -> float:
        """Record one rate-limit answer. Returns the pause in seconds."""
        self.strikes += 1
        if self.strikes >= self.give_up:
            self.blocked = True
        delay = retry_after if retry_after is not None else 30 * 2 ** (self.strikes - 1)
        delay = min(max(delay, self.min_pause), 900.0)
        self.cool_until = max(self.cool_until, time.monotonic() + delay)
        return delay

    def ok(self) -> None:
        self.strikes = 0


GENTLE = _GentleSite("SimpCity")


def _is_rate_limit(resp) -> float | None | bool:
    """False when the answer is not a rate limit, else the Retry-After seconds (or None)."""
    status = getattr(resp, "status", 0)
    headers = getattr(resp, "headers", {}) or {}
    retry = retry_after_seconds(headers.get("Retry-After"))
    if status == 429 or (status == 503 and retry is not None):
        return retry
    return False


def _install_http_guard(proxy: str, proxy_scope: str) -> None:
    """Every engine request passes here: moved hosts, SimpCity pacing, SimpCity-only proxy."""
    import contextlib

    try:
        from cyberdrop_dl.clients import flaresolverr
        from cyberdrop_dl.clients.http import HTTPClient
        from cyberdrop_dl.exceptions import ScrapeError
    except Exception:  # noqa: BLE001
        return

    site_proxy = proxy if proxy and proxy_scope == "simpcity" else ""
    original_raw = HTTPClient.raw_request

    @contextlib.asynccontextmanager
    async def gentle(client, url, method, kwargs):
        if GENTLE.blocked:
            raise ScrapeError(429, f"{GENTLE.name} kept asking to slow down, so NexDrop stopped contacting it for this run")
        GENTLE._bind()
        async with GENTLE.sem:
            for attempt in range(4):
                await GENTLE.turn()
                async with original_raw(client, url, method, **kwargs) as resp:
                    retry = _is_rate_limit(resp)
                    if retry is False:
                        GENTLE.ok()
                        yield resp
                        return
                    delay = GENTLE.limited(retry)
                    if attempt == 3 or GENTLE.blocked:
                        if GENTLE.blocked:
                            emit({"type": "log", "level": "warning", "message": (
                                f"{GENTLE.name} answered 'too many requests' {GENTLE.strikes} times in a row. NexDrop stops "
                                f"contacting it for this run so your IP address is not blocked. Try again later, or lower "
                                f"Settings > Speed > Per site.")})
                        yield resp
                        return
                    emit({"type": "log", "level": "warning", "message": (
                        f"{GENTLE.name} asked to slow down ({resp.status}). Pausing all {GENTLE.name} requests for "
                        f"{int(delay)} s, then trying again.")})

    def raw_request(self, url, /, method="GET", **kwargs):
        new_host = moved_host(getattr(url, "host", "") or "")
        if new_host:
            if not _SEEN.get(f"moved:{new_host}"):
                _SEEN[f"moved:{new_host}"] = True
                emit({"type": "log", "level": "info", "message": f"Old {url.host.split('.')[-2].title()} links point to a host that no longer exists. Using {new_host} instead."})
            url = url.with_host(new_host)
        if not is_gentle_host(getattr(url, "host", "") or ""):
            return original_raw(self, url, method, **kwargs)
        if site_proxy:
            kwargs.setdefault("proxy", site_proxy)
        return gentle(self, url, method, kwargs)

    HTTPClient.raw_request = raw_request

    if site_proxy:
        # FlareSolverr must solve SimpCity's bot check from the same address the requests then use.
        original_build = flaresolverr.Request.build.__func__

        def build(cls, command, url, /, config, *, wait, session):
            request = original_build(cls, command, url, config, wait=wait, session=session)
            if is_gentle_host(getattr(url, "host", "") or ""):
                request.payload.pop("session", None)
                request.payload["proxy"] = {"url": site_proxy}
            return request

        flaresolverr.Request.build = classmethod(build)


SESSION_COOKIES = PORTABLE / "cookies" / "session" / "simpcity.txt"
_PROTECTED_MARK = b"NEXDROP-DPAPI1\n"


def _dpapi(data: bytes, protect: bool) -> bytes | None:
    """Windows DPAPI for the current user (what Electron's safeStorage uses too). None elsewhere."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = call(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result))  # 0x1: no UI
    if not ok:
        return None
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def write_protected(path: Path, text: str) -> None:
    """Saved sign-in cookies are encrypted for this Windows user. (Plain text only off Windows, for tests.)"""
    raw = text.encode("utf-8")
    sealed = _dpapi(raw, True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(_PROTECTED_MARK + sealed if sealed is not None else raw)
    os.replace(temp, path)


def read_protected(path: Path) -> str:
    """The text of a file written by write_protected(), or of an older plain-text file."""
    raw = path.read_bytes()
    if raw.startswith(_PROTECTED_MARK):
        opened = _dpapi(raw[len(_PROTECTED_MARK):], False)
        return opened.decode("utf-8", errors="replace") if opened is not None else ""
    return raw.decode("utf-8", errors="replace")


def save_site_cookies(jar, path: Path = SESSION_COOKIES) -> int:
    """Keep SimpCity's cookies (sign-in, bot-check clearance) for the next run.

    A new session and a new bot check on every run look like a new visitor each
    time; reusing them is what a browser does."""
    now = time.time()
    expirations = getattr(jar, "_expirations", {}) or {}
    rows = []
    for morsel in jar:
        domain = str(morsel.get("domain") or "")
        if not is_gentle_host(domain.lstrip(".")) or not morsel.value:
            continue
        path_value = str(morsel.get("path") or "/")
        expires = 0
        for key, when in expirations.items():
            if len(key) == 3 and key[0] == domain.lstrip(".") and key[2] == morsel.key:
                expires = int(when)
                break
        if expires and expires <= now:
            continue
        secure = "TRUE" if morsel.get("secure") else "FALSE"
        rows.append("\t".join(("." + domain.lstrip("."), "TRUE", path_value, secure, str(expires), morsel.key, morsel.value)))
    if not rows:
        return 0
    write_protected(path, "# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n")
    return len(rows)


def saved_site_cookie_lines(taken: set[tuple[str, str]], path: Path = SESSION_COOKIES) -> list[str]:
    """Saved SimpCity cookies that the user's own cookie file does not already set."""
    try:
        lines = read_protected(path).splitlines()
    except OSError:
        return []
    now = time.time()
    out = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 7 or line.startswith("# "):
            continue
        site = parts[0].removeprefix("#HttpOnly_").lstrip(".").lower().removeprefix("www.")
        if not is_gentle_host(site) or (site, parts[5]) in taken:
            continue
        if parts[4].isdigit() and int(parts[4]) and int(parts[4]) <= now:
            continue
        out.append(line)
    return out


_SITE_ERROR_TITLES = ("oops! we ran into some problems", "you do not have permission", "the requested", "not found")


def _is_site_error_page(message: str) -> bool:
    match = re.search(r"instead of the file: '([^']*)'", message)
    return bool(match) and any(part in match.group(1).lower() for part in _SITE_ERROR_TITLES)


class _HostSlots:
    """Up to `limit` downloads at once from one server; the limit can drop while in use."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0
        self.cond = None

    async def __aenter__(self):
        import asyncio

        loop = asyncio.get_running_loop()
        if self.cond is None or getattr(self, "loop", None) is not loop:
            self.cond, self.loop, self.active = asyncio.Condition(), loop, 0
        async with self.cond:
            await self.cond.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *_):
        async with self.cond:
            self.active -= 1
            self.cond.notify_all()


class _UnreachableSites:
    """Stops asking a site whose file servers can't be reached at all.

    Coomer and Kemono pages load, but their file servers (n1..n4) can be blocked on
    some networks: every download then waits out the connection timeout, twice,
    and a big profile sits there for an hour without saving anything. After
    `limit` connection timeouts in a row for one site (no download in between
    worked), its remaining files fail at once with "Server unreachable".
    """

    _TIMEOUTS = ("Connection timeout to host", "ConnectionTimeoutError", "semaphore timeout period has expired")

    def __init__(self, limit: int = 8) -> None:
        self.limit = limit
        self.streak: dict[str, int] = {}
        self.down: set[str] = set()

    @staticmethod
    def site(host: str) -> str:
        """n3.coomer.st and coomer.st count as one site."""
        return ".".join((host or "").lower().split(".")[-2:])

    def is_down(self, site: str) -> bool:
        return bool(site) and site in self.down

    def failed(self, site: str, text: str) -> bool:
        """Count a failed download; True when this one marks the site unreachable."""
        if not site or site in self.down:
            return False
        if not any(mark in text for mark in self._TIMEOUTS):
            self.streak.pop(site, None)
            return False
        self.streak[site] = self.streak.get(site, 0) + 1
        if self.streak[site] < self.limit:
            return False
        self.down.add(site)
        return True

    def ok(self, site: str) -> None:
        self.streak.pop(site, None)


UNREACHABLE = _UnreachableSites()


class _SiteBreaker:
    """Pauses one site that keeps failing with server errors, so the rest of the run carries on.

    After `threshold` downloads in a row from a site fail with a busy or rate-limit answer
    (429, 5xx), the site is paused: its files fail at once as "Site Paused" for `cooldown`
    seconds (doubling on each new pause, at most 10 minutes) and the retry pass gets them later.
    When the pause ends, the next download is a probe: one success closes the breaker, one more
    failure opens it again."""

    _SERVER_ERRORS = re.compile(r"\b(429|500|502|503|504|52[0-4])\b")

    def __init__(self, threshold: int = 6, cooldown: float = 60.0, ceiling: float = 600.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.ceiling = ceiling
        self.streak: dict[str, int] = {}
        self.opens: dict[str, int] = {}
        self.until: dict[str, float] = {}

    def blocked_for(self, site: str, now: float | None = None) -> float:
        until = self.until.get(site)
        if not site or until is None:
            return 0.0
        left = until - (time.monotonic() if now is None else now)
        return left if left > 0 else 0.0

    def failed(self, site: str, text: str, now: float | None = None) -> float:
        """Count one failure; returns the pause in seconds when this one opens the breaker."""
        if not site or not self._SERVER_ERRORS.search(text or "") or "Site Paused" in (text or ""):
            if site and "Site Paused" not in (text or ""):
                self.streak.pop(site, None)
            return 0.0
        self.streak[site] = self.streak.get(site, 0) + 1
        probing = site in self.until  # a failure right after a pause ends opens it again
        if self.streak[site] < self.threshold and not probing:
            return 0.0
        if self.blocked_for(site, now):
            return 0.0
        self.opens[site] = self.opens.get(site, 0) + 1
        pause = min(self.cooldown * 2 ** (self.opens[site] - 1), self.ceiling)
        self.until[site] = (time.monotonic() if now is None else now) + pause
        self.streak[site] = 0
        return pause

    def ok(self, site: str) -> None:
        self.streak.pop(site, None)
        self.until.pop(site, None)


BREAKER = _SiteBreaker()


class _ServerSlots:
    """Replaces the engine's one-download-per-server lock (used for Bunkr and a few others).

    The stock lock downloaded every file on a Bunkr CDN server one by one; a
    thread whose files sit on two servers then ran on two connections. This
    allows a few at once and drops a server back to one connection as soon as
    it answers 429 (too many requests) or 503.
    """

    RECOVER_AFTER = 120.0  # seconds without a slow-down answer before a server gets one connection back

    def __init__(self) -> None:
        self.per_server = 3
        self.hosts: dict[str, _HostSlots] = {}
        self.reduced: dict[str, float] = {}  # host -> when it last asked to slow down

    def get(self, host: str) -> _HostSlots:
        slot = self.hosts.get(host)
        if slot is None:
            slot = self.hosts[host] = _HostSlots(self.per_server)
        return slot

    def back_off(self, host: str) -> None:
        slot = self.hosts.get(host)
        if not host or slot is None:
            return
        first = host not in self.reduced
        self.reduced[host] = time.monotonic()
        if slot.limit <= 1:
            return
        slot.limit = 1
        if first:
            emit({"type": "log", "level": "warning", "message": f"{host} asked to slow down. Using one connection for it for now."})

    def ok(self, host: str, now: float | None = None) -> None:
        """A download from a slowed-down server worked: after a quiet spell, allow one more connection."""
        since = self.reduced.get(host)
        slot = self.hosts.get(host)
        if since is None or slot is None:
            return
        now = time.monotonic() if now is None else now
        if now - since < self.RECOVER_AFTER:
            return
        slot.limit = min(self.per_server, slot.limit + 1)
        if slot.limit >= self.per_server:
            del self.reduced[host]
        else:
            self.reduced[host] = now  # the next step up waits another quiet spell


SERVER_SLOTS = _ServerSlots()


def _install_server_slots(per_server: int) -> None:
    import contextlib

    try:
        from cyberdrop_dl.downloader.http import Downloader
    except Exception:  # noqa: BLE001
        return
    SERVER_SLOTS.per_server = max(1, per_server)

    @contextlib.asynccontextmanager
    async def lock(self, url):
        server = SERVER_SLOTS.get(url.host or "") if self.use_server_lock else contextlib.nullcontext()
        async with server, self._semaphore, self.manager.http_client.limiter.downloads:
            yield

    Downloader.lock = lock


TYPE_FOLDERS = {"image": "Images", "video": "Videos", "audio": "Audio", "other": "Other"}


def _install_type_folders(enabled: bool) -> None:
    """Save each file into an Images / Videos / Audio / Other folder inside its usual folder.

    Done per file at download time. The engine's own sorter instead moves every
    file in the whole download folder (old runs included) after the run.
    """
    if not enabled:
        return
    try:
        from cyberdrop_dl.clients import downloads as dl
        from cyberdrop_dl.constants import FileExt
    except Exception:  # noqa: BLE001
        return
    original = dl.DownloadClient._download

    async def download(self, domain, media_item):
        folder = getattr(media_item, "download_folder", None)
        if folder is not None and not getattr(media_item, "is_segment", False) and folder.name not in TYPE_FOLDERS.values():
            ext = str(getattr(media_item, "ext", "") or "").lower()
            kind = "image" if ext in FileExt.IMAGE else "video" if ext in FileExt.VIDEO else "audio" if ext in FileExt.AUDIO else "other"
            media_item.download_folder = folder / TYPE_FOLDERS[kind]
        return await original(self, domain, media_item)

    dl.DownloadClient._download = download


def _install_last_post_report() -> None:
    """Report the last forum post reached, so a watched thread can continue from it."""
    try:
        from cyberdrop_dl.csv_logs import CSVLogsManager
    except Exception:  # noqa: BLE001
        return
    original = CSVLogsManager.write_last_forum_post

    def write_last_forum_post(self, url):
        emit({"type": "last_post", "url": str(url)})
        return original(self, url)

    CSVLogsManager.write_last_forum_post = write_last_forum_post


def _install_resume_skip(urls: list) -> None:
    """On Resume, skip files that finished before the pause (needed when the run uses Download again)."""
    skip = {str(u) for u in urls or [] if u}
    if not skip:
        return
    try:
        from cyberdrop_dl.downloader.http import Downloader
        from cyberdrop_dl.exceptions import SkipDownloadError
    except Exception:  # noqa: BLE001
        return

    class SavedBeforePause(SkipDownloadError):
        def __init__(self, origin) -> None:
            super().__init__("Saved before the pause", message="saved before the pause", origin=origin)

    original = Downloader._check_skip_by_config

    async def check(self, media_item):
        if str(media_item.url) in skip or shown_url(str(media_item.url)) in skip:
            raise SavedBeforePause(media_item)
        return await original(self, media_item)

    Downloader._check_skip_by_config = check


RESERVE_MB = 16


def _install_db_reserve() -> None:
    """Shrink the engine's database reserve from 250 MB to 16 MB.

    Cyberdrop-DL writes a 250 MB zero-filled block into its database on every
    start (through the write-ahead log, so ~500 MB of disk writes) as a guard
    against a full disk. The history it protects is about 1 MB, so a 16 MB
    reserve keeps the guard without the cost.
    """
    try:
        from cyberdrop_dl.database import _db, common
    except Exception:  # noqa: BLE001
        return

    async def pre_allocate_small(db_conn) -> None:
        cursor = await db_conn.execute("PRAGMA freelist_count;")
        free = await cursor.fetchone()
        if free and free[0] > 1024:
            return
        await db_conn.executescript(
            "CREATE TABLE IF NOT EXISTS t(x);"
            f"INSERT INTO t VALUES(zeroblob({RESERVE_MB}*1024*1024));"
            "DROP TABLE t;"
        )
        await db_conn.commit()

    for module in (common, _db):
        if hasattr(module, "pre_allocate_250mb"):
            module.pre_allocate_250mb = pre_allocate_small


def _install_progress() -> None:
    """Report progress per file and success only when the engine records a saved file."""
    from contextlib import contextmanager

    from cyberdrop_dl.manager import Manager
    from cyberdrop_dl.progress.scraping import downloads as downloads_module
    from cyberdrop_dl.progress.scraping.downloads import DownloadsPanel

    original_file = DownloadsPanel.download_file

    def download_file(self, description, /, domain, total, *, url=None):
        hook = original_file(self, description, domain, total, url=url)
        key = id(hook)
        TRACKER.start(key, str(url or ""), str(description), str(domain), total or None)
        advance, done = hook.advance, hook.done

        def wrapped_advance(amount: int = 1) -> None:
            advance(amount)
            TRACKER.advance(key, amount)

        def wrapped_done() -> None:
            TRACKER.end(key)
            done()

        def wrapped_resume(amount: int) -> None:
            advance(amount)
            TRACKER.resume(key, amount)

        hook.advance = wrapped_advance
        hook.done = wrapped_done
        hook.resume = wrapped_resume
        return hook

    DownloadsPanel.download_file = download_file

    # HLS videos arrive as segments. The engine tracks the active video in a
    # context variable; segments are matched to their video through it.
    original_hls = DownloadsPanel.download_hls
    original_seg = DownloadsPanel.download_hls_seg
    hls_keys: dict[object, int] = {}
    counter = iter(range(1, 1 << 62))

    @contextmanager
    def download_hls(self, filename, /, domain, segments, *, url=None):
        key = -next(counter)
        TRACKER.start(key, str(url or ""), str(filename), str(domain), None)
        task_id = None
        try:
            with original_hls(self, filename, domain, segments, url=url):
                task_id = downloads_module._current_hls_task.get(None)
                if task_id is not None:
                    hls_keys[task_id] = key
                yield
        finally:
            if task_id is not None:
                hls_keys.pop(task_id, None)
            TRACKER.end(key)

    def download_hls_seg(self):
        hook = original_seg(self)
        key = hls_keys.get(downloads_module._current_hls_task.get(None))
        if key is None:
            return hook
        advance = hook.advance

        def wrapped_advance(amount: int) -> None:
            advance(amount)
            TRACKER.advance(key, amount)

        hook.advance = wrapped_advance
        return hook

    DownloadsPanel.download_hls = download_hls
    DownloadsPanel.download_hls_seg = download_hls_seg

    original_completed = Manager.add_completed

    def add_completed(self, media_item) -> None:
        original_completed(self, media_item)
        if getattr(media_item, "is_segment", False):
            return
        try:
            path = Path(media_item.path)
            size = path.stat().st_size if path.is_file() else 0
            emit({"type": "saved", "url": str(media_item.url), "filename": path.name, "bytes": size, "path": str(path)})
        except Exception:  # noqa: BLE001
            emit({"type": "saved", "url": str(getattr(media_item, "url", "")), "filename": "", "bytes": 0})
            return
        same = DUPLICATES.find(getattr(media_item, "xxhash", None), path, size)
        pair = frozenset((os.path.normcase(str(path)), os.path.normcase(str(same))))
        if same and pair not in DUPLICATES.reported:  # two copies finishing together match each other
            DUPLICATES.reported.add(pair)
            emit({"type": "duplicate", "url": str(media_item.url), "path": str(path), "of": str(same)})

    Manager.add_completed = add_completed


def _bound_int(value: object, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _engine_log_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).resolve()
    try:
        path.relative_to((PORTABLE / "logs").resolve())
    except ValueError:
        return None
    return path


def _file_log(path: Path | None):
    """The engine's own detailed log (every request outcome and traceback) for this run."""
    import contextlib

    if path is None:
        return contextlib.nullcontext()
    try:
        from cyberdrop_dl.logs import setup_file_logging

        return setup_file_logging(path, level=logging.INFO, log_http_traffic=False)
    except Exception:  # noqa: BLE001 - logging must never block a download
        return contextlib.nullcontext()


def main() -> int:
    from cyberdrop_dl.logs import disable_console_logging, setup_console_logging

    with setup_console_logging(), disable_console_logging():
        return _run()


def _cookie_source(value: object) -> Path | None:
    """Only a cookie file or folder that the session prepared inside appdata is accepted."""
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value).resolve()
    allowed = (ROOT / "appdata").resolve()
    try:
        candidate.relative_to(allowed)
    except ValueError:
        return None
    return candidate if candidate.exists() else None


def _cookie_domain_ok(domain: str, keys: list[str], hosts: set[str]) -> bool:
    domain = domain.lower().removeprefix("#httponly_").lstrip(".").removeprefix("www.")
    if not domain:
        return False
    for host in hosts:
        if domain == host or domain.endswith("." + host) or host.endswith("." + domain):
            return True
    labels = set(domain.split("."))
    for key in keys:
        if "." in key:
            key = key.lstrip(".")
            if domain == key or domain.endswith("." + key) or key.endswith("." + domain):
                return True
        elif key in labels:
            return True
    return False


def _filter_cookies(source: Path | None, mapping: dict, urls: list[str]) -> Path | None:
    """Keep only cookies for sites the engine can download from (or the pasted links).

    A full browser export can hold hundreds of unrelated sites, including banks
    and email. Only the relevant ones are handed to the engine.
    """
    from urllib.parse import urlparse

    if source is None:
        files = []
    else:
        files = [source] if source.is_file() else sorted(source.glob("*.txt"))
    keys = [str(key).lower() for key in mapping]
    hosts = {(urlparse(url).hostname or "").lower().removeprefix("www.") for url in urls} - {""}
    target = PORTABLE / "cookies" / "engine"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    kept_sites: set[str] = set()
    dropped_sites: set[str] = set()
    taken: set[tuple[str, str]] = set()
    for index, file in enumerate(files):
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        out = ["# Netscape HTTP Cookie File"]
        for line in lines:
            if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            site = parts[0].removeprefix("#HttpOnly_").lstrip(".").lower()
            if _cookie_domain_ok(parts[0], keys, hosts):
                out.append(line)
                kept_sites.add(site)
                taken.add((site.removeprefix("www."), parts[5]))
            else:
                dropped_sites.add(site)
        if len(out) > 1:
            (target / f"{index:02d}.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
    session = saved_site_cookie_lines(taken)
    if session:
        (target / "session.txt").write_text("# Netscape HTTP Cookie File\n" + "\n".join(session) + "\n", encoding="utf-8")
        emit({"type": "log", "level": "info", "message": "Reusing SimpCity's session from the last run."})
        if not kept_sites:
            return target
    if not kept_sites and not dropped_sites:
        return None
    message = f"Using cookies for {len(kept_sites)} supported site(s)"
    if dropped_sites:
        message += f". Ignored {len(dropped_sites)} other site(s) in your cookie file"
    emit({"type": "log", "level": "info", "message": message + "."})
    return target if kept_sites else None


def _relative(path: Path, root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if any(part == ".." for part in relative.parts):
        return None
    return relative.as_posix()


def _run() -> int:
    payload = json.loads(sys.stdin.readline() or "{}")
    urls = [str(url) for url in payload.get("urls", []) if str(url).startswith(("http://", "https://"))]
    if not urls:
        emit({"type": "error", "message": "No http(s) URLs were provided"})
        return 2

    destination = Path(payload.get("downloadFolder") or (ROOT.parent / "downloads" / "nexdrop")).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    _install_progress()
    _install_db_reserve()
    _install_download_clearance()
    _install_server_slots(_bound_int(payload.get("serverSlots"), 1, 6, 3))
    _install_type_folders(bool(payload.get("typeFolders")))
    _install_last_post_report()
    _install_resume_skip(payload.get("skipUrls") or [])
    # Read by the TikTok patch in engine/vendor-patches.
    os.environ["NEXDROP_TIKTOK_AUDIO"] = "1" if payload.get("tiktokAudio") else "0"
    bridge = _LogBridge()
    bridge.setLevel(logging.INFO)
    logging.getLogger("cyberdrop_dl").addHandler(bridge)
    logging.getLogger("cyberdrop_dl").setLevel(logging.INFO)

    from cyberdrop_dl import __version__, aio
    from cyberdrop_dl.commands import CLIarguments
    from cyberdrop_dl.commands.scrape import prepare_manager
    from cyberdrop_dl.config import Config
    from cyberdrop_dl.config.settings import Downloads, MaxChildren, Network, Sort, UIMode, UIOptions
    from cyberdrop_dl.models.validators import to_yarl_url
    from cyberdrop_dl.scrape_mapper import ScrapeMapper

    profile = str(payload.get("profile") or "balanced")
    if profile not in {"fast", "balanced", "deep"}:
        profile = "balanced"
    fields_cookie = _cookie_source(payload.get("cookieFolder"))
    from cyberdrop_dl.scrape_mapper import get_crawlers_mapping

    mapping = get_crawlers_mapping()
    fields_cookie = _filter_cookies(fields_cookie, mapping, urls)
    simpcity = mapping.get("simpcity.cr")
    if simpcity is None and any("simpcity" in url for url in urls):
        emit({"type": "log", "level": "error", "message": "The SimpCity crawler is not registered."})
    flare_url = str(payload.get("flareSolverUrl") or "").strip()
    emit({"type": "ready", "engine": "NexDrop", "version": __version__, "profile": profile, "simpcity": simpcity is not None, "flaresolverr": bool(flare_url)})
    fields = {
        "download_folder": destination,
        "ui": UIOptions(mode=UIMode.DISABLED, show_stats=False),
        "update_check": False,
        "ignore_history": bool(payload.get("ignoreHistory")),
        "cookies": fields_cookie,
        "sort": Sort(output_folder=destination.parent / f"{destination.name} sorted"),
    }
    raw_auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    auth_fields = {}
    from cyberdrop_dl.config.auth import ApiKeyAuth, Authentication, EmailAuth

    for name in ("gofile", "pixeldrain", "nova", "real_debrid"):
        key = str((raw_auth.get(name) or {}).get("api_key") or "").strip()
        if key:
            auth_fields[name] = ApiKeyAuth(api_key=key)
    mega = raw_auth.get("mega_nz") or {}
    if str(mega.get("email") or "").strip() and str(mega.get("password") or ""):
        auth_fields["mega_nz"] = EmailAuth(email=str(mega["email"]).strip(), password=str(mega["password"]))
    if auth_fields:
        fields["auth"] = Authentication(**auth_fields)
    def _bound(value: object, low: int, high: int, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    order_mode = str(payload.get("orderMode") or "source")
    if order_mode not in {"source", "parallel", "fastest"}:
        order_mode = "source"
    if order_mode == "source":
        downloads = 1
        per_host = 1
    else:
        downloads = _bound(payload.get("maxDownloads"), 1, 32, 16)
        per_host = _bound(payload.get("perHost"), 1, 16, 6)
    attempts = _bound(payload.get("attempts"), 1, 3, 2)
    connect_timeout = _bound(payload.get("connectionTimeout"), 10, 30, 15)
    read_timeout = _bound(payload.get("readTimeout"), 30, 300, 60)
    thread_depth = _bound(payload.get("threadDepth"), 0, 3, 0)
    speed = payload.get("speedLimit")
    try:
        speed_bytes = int(speed or 0)
    except (TypeError, ValueError):
        speed_bytes = 0
    chrome = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    network = {
        "user_agent": chrome,
        "connection_timeout": connect_timeout,
        "read_timeout": read_timeout,
        # Global request cap. The stock 25/s throttles many parallel small files;
        # per-site limits (SimpCity's slow scan rate included) still apply.
        "rate_limit": 100,
    }
    if flare_url:
        network["flaresolverr"] = flare_url
        network["flaresolverr_wait"] = 5
    proxy = str(payload.get("proxy") or "").strip()
    proxy_scope = "simpcity" if payload.get("proxyScope") == "simpcity" else "all"
    if proxy and proxy_scope == "all":
        network["proxy"] = proxy
    GENTLE.gap = float(_bound_int(payload.get("simpcityGap"), 1, 30, 2))
    _install_http_guard(proxy, proxy_scope)
    fields["network"] = Network(**network)
    download_fields = {
        "concurrency": downloads,
        "concurrency_per_domain": per_host,
        "attempts": attempts,
        "back_pressure": True,
    }
    if speed_bytes > 0:
        download_fields["speed_limit"] = speed_bytes
    fields["downloads"] = Downloads(**download_fields)
    fields["max_thread_depth"] = thread_depth
    fields["deep_scrape"] = bool(payload.get("deepScrape")) or profile == "deep"
    types = payload.get("fileTypes") if isinstance(payload.get("fileTypes"), dict) else {}
    min_image_kb = _bound_int(payload.get("minImageKB"), 0, 100_000, 0)
    try:
        from cyberdrop_dl.config.filters import Filters, _FileFilter, _FileSizes, _SizeLimit

        filters = {"files": _FileFilter(
            images=types.get("images", True) is not False,
            videos=types.get("videos", True) is not False,
            audio=types.get("audio", True) is not False,
            non_media=types.get("other", True) is not False,
        )}
        if min_image_kb:
            filters["sizes"] = _FileSizes(image=_SizeLimit(min=min_image_kb * 1024))
        fields["filters"] = Filters(**filters)
    except Exception as error:  # noqa: BLE001
        emit({"type": "log", "level": "warning", "message": f"File filters could not be applied: {error}"})
    from cyberdrop_dl.smart_naming import CORRECTIONS, REGISTRY, REPORT, SETTINGS as NAMING, NamingSettings

    naming = NamingSettings.from_payload(payload)
    fields["max_children"] = MaxChildren()
    try:
        from cyberdrop_dl.config.settings import SubFolders, _SubFoldersInclude

        # "Include Website Name in Folder" (off by default since 2.5): "@allecakes", not "@allecakes (Twitter)".
        fields["subfolders"] = SubFolders(include=_SubFoldersInclude(domain=naming.site_in_folder))
    except Exception as error:  # noqa: BLE001
        emit({"type": "log", "level": "warning", "message": f"Folder naming could not be applied: {error}"})
    prefix_on = naming.numbering != "off"
    album_names_on = True if payload.get("albumNames") is None else bool(payload.get("albumNames"))
    post_folders_on = True if payload.get("postFolders") is None else bool(payload.get("postFolders"))
    emit({
        "type": "log",
        "stage": "system",
        "level": "info",
        "message": f"Limits: {downloads} downloads, {per_host} per site, {attempts} attempts, order {order_mode}, names: {naming.describe()}{', album name for albums' if album_names_on and not naming.smart else ''}, {'a folder for each post' if post_folders_on else 'no post folders'}, SimpCity every {int(GENTLE.gap)} s{' through the proxy' if proxy and proxy_scope == 'simpcity' else ''}.",
    })
    start_page = max(1, int(payload.get("startPage") or 1))
    end_raw = payload.get("endPage")
    end_page = int(end_raw) if end_raw else None
    if end_page is not None and end_page < start_page:
        emit({"type": "error", "message": "Last page cannot be lower than the start page"})
        return 2
    from cyberdrop_dl.crawlers._forum import FORUM_PAGE_END, FORUM_PAGE_START
    from cyberdrop_dl.source_order import ALBUM_NAMES, NUMBER_MODE, ORDER_MODE, POST_FOLDERS, SEQUENCE_PREFIX, reset as reset_order

    reset_order(str(payload.get("orderManifest") or ""), str(payload.get("jobKey") or "") or None)
    REGISTRY.load(str(payload.get("folderRegistry") or "") or None, destination)
    CORRECTIONS.load(str(payload.get("namingCorrections") or "") or None)
    REPORT.open(str(payload.get("namingReport") or "") or None)
    order_token = ORDER_MODE.set(order_mode)
    naming_token = NAMING.set(naming)
    number_token = NUMBER_MODE.set(naming.numbering)
    prefix_token = SEQUENCE_PREFIX.set(prefix_on)
    album_token = ALBUM_NAMES.set(album_names_on)
    post_token = POST_FOLDERS.set(post_folders_on)

    page_start = FORUM_PAGE_START.set(start_page)
    page_end = FORUM_PAGE_END.set(end_page)
    if start_page > 1 or end_page:
        emit({
            "type": "log",
            "stage": "crawl",
            "level": "info",
            "message": f"Forum pages {start_page} through {end_page or 'the last linked page'}. Sites that do not number pages ignore this.",
        })
    overrides = Config(**fields)
    engine_log = _engine_log_path(payload.get("engineLog"))
    try:
        with prepare_manager(CLIarguments(), overrides)() as manager, _file_log(engine_log):
            manager.log_config_settings()

            async def go():
                async with manager.database:
                    async with ScrapeMapper(manager)() as mapper:
                        await mapper.run(tuple(to_yarl_url(url) for url in urls))
                        mapper.shutdown()

            aio.run(go())
            try:
                save_site_cookies(manager.http_client.cookies)
            except Exception as error:  # noqa: BLE001 - never fail a finished run over cookies
                emit({"type": "log", "level": "info", "message": f"SimpCity's session was not saved for the next run: {str(error)[:120]}"})
            if payload.get("embeddedTitles"):
                _embedded_titles(manager, naming)
            saved = []
            for item in manager.completed_downloads:
                path = Path(item.path)
                relative = _relative(path, destination)
                if relative is None or not path.is_file():
                    continue
                saved.append(
                    {
                        "url": str(item.url),
                        "filename": path.name,
                        "bytes": path.stat().st_size,
                        "relative": relative,
                    }
                )
            emit({"type": "done", "saved": saved, "completed": len(saved)})
    finally:
        # The filtered cookie copies are sign-ins in plain text; they only exist during the run.
        shutil.rmtree(PORTABLE / "cookies" / "engine", ignore_errors=True)
        FORUM_PAGE_START.reset(page_start)
        FORUM_PAGE_END.reset(page_end)
        ORDER_MODE.reset(order_token)
        NAMING.reset(naming_token)
        NUMBER_MODE.reset(number_token)
        SEQUENCE_PREFIX.reset(prefix_token)
        ALBUM_NAMES.reset(album_token)
        POST_FOLDERS.reset(post_token)
    return 0


def _embedded_titles(manager, naming) -> None:
    """Smart naming V2, optional: files that kept a meaningless name get the title written inside them."""
    from cyberdrop_dl.smart_naming import embedded_name

    for item in manager.completed_downloads:
        try:
            path = Path(item.path)
            if not path.is_file():
                continue
            name, where = embedded_name(str(item.url), path, naming)
            if not name or name == path.name:
                continue
            target = path.with_name(name)
            if target.exists():
                continue
            path.rename(target)
            item.path = target
        except Exception as error:  # noqa: BLE001 - a tag never costs a download
            emit({"type": "log", "level": "info", "message": f"Could not read the title inside {getattr(item, 'url', '')}: {str(error)[:120]}"})
            continue
        emit({"type": "renamed", "url": str(item.url), "name": name, "path": str(target), "source": where})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        raise SystemExit(1) from exc
