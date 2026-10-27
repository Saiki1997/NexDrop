"""Run one NEXDROP session on the patched Cyberdrop-DL bridge.

stdin   line 1: the run config as JSON. Later lines: "stop" asks for a graceful stop.
stdout  one JSON object per line, read by the Electron main process.

Events for the window:
  run        {root, urls}
  phase      {stage, message}           stage: start | crawl | download | finalize
  log        {level, message}
  files      {items: [FileRow], bytes, speed, counts, total}
  error      {message}
  finished   {saved, failed, skipped, failedUrls, root, stopped, bytes, seconds, emptyReason}

FileRow: {url, host, name, sequence, state, read, total, speed, reason}
  state: queued | active | retrying | done | failed | skipped | cancelled
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

# python313._pth runs Python isolated. The script directory is not on sys.path,
# and Electron starts this file by absolute path with cwd set to the install root.
_DESKTOP_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.normcase(_DESKTOP_DIR) not in {os.path.normcase(os.path.abspath(e)) for e in sys.path if e}:
    sys.path.insert(0, _DESKTOP_DIR)
from auth_cookies import convert_any  # noqa: E402
import error_classes  # noqa: E402
from job_store import JobStore  # noqa: E402
from scrape_status import classify_scrape_message  # noqa: E402
import telegram_account  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
BRIDGE = ENGINE / "bridge.py"
VENDOR = ENGINE / "vendor"
APPDATA = ENGINE / "appdata"
HISTORY = APPDATA / "completed-urls.json"
RUNS = APPDATA / "runs.json"
RUN_COOKIES = APPDATA / "cookies" / "run"
MANIFEST = APPDATA / "order-manifest.jsonl"
FOLDER_REGISTRY = APPDATA / "folder-registry.jsonl"  # smart naming: which collection owns which folder
CORRECTIONS = APPDATA / "naming-corrections.jsonl"  # smart naming V2: names you changed in History
DOWNLOADS = ROOT / "downloads" / "nexdrop"
MAX_BYTES = 80 * 1024 * 1024
MAX_URLS = 200
MAX_RUN_HISTORY = 500
FLUSH_INTERVAL = 0.25
FILE_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp",
    ".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ts", ".m3u8",
    ".mp3", ".m4a", ".flac", ".wav", ".ogg",
    ".zip", ".rar", ".7z", ".pdf",
)
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
FINAL_STATES = ("done", "failed", "skipped", "cancelled")

PYTHON = os.environ.get("NEXDROP_PYTHON") or sys.executable
PROC: subprocess.Popen | None = None
FLARE: subprocess.Popen | None = None
FLARE_OWNED = False
STOP = threading.Event()      # the user asked to stop
FINISHED = threading.Event()  # the run is over; ends the flusher
_out_lock = threading.Lock()


# ─── output ──────────────────────────────────────────────────────────────────

RUN_LOG = None          # text file with everything that happened in this run
RUN_LOG_PATH: Path | None = None
ENGINE_LOG_PATH: Path | None = None
NAMING_LOG_PATH: Path | None = None  # "Explain file names": why each file got its name
RUN_LOG_KEEP = 30


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def write_run_log(text: str) -> None:
    if RUN_LOG is None:
        return
    try:
        RUN_LOG.write(text if text.endswith("\n") else text + "\n")
        RUN_LOG.flush()
    except (OSError, ValueError):
        pass


def _log_event(payload: dict) -> None:
    kind = payload.get("type")
    if kind == "log":
        write_run_log(f"{_stamp()} [{payload.get('level', 'info')}] {payload.get('message', '')}")
    elif kind == "phase":
        write_run_log(f"{_stamp()} [phase] {payload.get('message', '')}")
    elif kind == "error":
        write_run_log(f"{_stamp()} [error] {payload.get('message', '')}")
    elif kind == "finished":
        write_run_log(
            f"{_stamp()} [done] saved {payload.get('saved')}, already saved {payload.get('skipped')}, "
            f"failed {payload.get('failed')}, stopped {payload.get('stopped')}, {payload.get('bytes')} bytes in {payload.get('seconds')} s"
        )
        for url in payload.get("failedUrls") or []:
            write_run_log(f"           failed: {url}")


def emit(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with _out_lock:
        _log_event(payload)
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass  # the window is gone; keep shutting down quietly


def open_run_log(config: dict, urls: list[str]) -> None:
    """One readable log per run in engine\\appdata\\logs\\runs, plus the engine's detailed log beside it."""
    global RUN_LOG, RUN_LOG_PATH, ENGINE_LOG_PATH, NAMING_LOG_PATH
    folder = APPDATA / "logs" / "runs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        RUN_LOG_PATH = folder / f"{stamp}.log"
        ENGINE_LOG_PATH = folder / f"{stamp}.engine.log"
        NAMING_LOG_PATH = folder / f"{stamp}.naming.log" if config.get("explainNames") else None
        RUN_LOG = RUN_LOG_PATH.open("w", encoding="utf-8")
        old = sorted(p for p in folder.glob("*.log") if p.name.count(".") == 1)  # not .engine/.naming logs
        for stale in old[:-RUN_LOG_KEEP]:
            for path in [stale, *folder.glob(stale.stem + ".*.log")]:
                try:
                    path.unlink()
                except OSError:
                    pass
    except OSError:
        RUN_LOG = None
        return
    secret = {"auth", "telegram", "urls", "unsupported", "cookieFiles", "cookies", "cookieFolder", "accountCookies"}
    safe = {k: v for k, v in config.items() if k not in secret}
    write_run_log(f"NEXDROP run {datetime.now().isoformat(timespec='seconds')}")
    write_run_log(f"Python {sys.version.split()[0]} on {sys.platform}; folder {ROOT}")
    write_run_log(f"Settings: {json.dumps(safe, ensure_ascii=False)}")
    signins = sorted((config.get("auth") or {}).keys()) + (["telegram"] if config.get("telegram") else [])
    write_run_log(f"Cookie files: {len(config.get('cookieFiles') or [])}; site sign-ins: {', '.join(signins) or 'none'}")
    write_run_log("Links:")
    for url in urls:
        write_run_log(f"  {url}")
    write_run_log(f"Engine log: {ENGINE_LOG_PATH}")
    write_run_log("")


def log_file_outcome(row: dict) -> None:
    state = row.get("state")
    if state not in FINAL_STATES:
        return
    seq = f"{row.get('sequence'):04d} " if row.get("sequence") else ""
    reason = f"  ({row.get('reason')})" if row.get("reason") and state != "done" else ""
    size = f"  {row.get('read') or 0} bytes" if state == "done" else ""
    write_run_log(f"{_stamp()} [file] {state:<9} {seq}{row.get('name') or ''}  {row.get('url')}{size}{reason}")


def log(level: str, message: str) -> None:
    emit({"type": "log", "level": level, "message": message})


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.") or "unknown"


class FileTable:
    """Per-file state, sent to the window in batches instead of one event per chunk."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: dict[str, dict] = {}
        self.dirty: set[str] = set()
        self.bytes = 0
        self._samples: list[tuple[float, int]] = []
        self.counts = {state: 0 for state in FINAL_STATES}
        self.on_change = None  # callback(row) when a row changes state

    def update(self, url: str, **fields) -> None:
        if not url:
            return
        changed = None
        with self.lock:
            row = self.rows.get(url)
            if row is None:
                row = {"url": url, "host": host_of(url), "name": "", "sequence": 0, "state": "queued",
                       "read": 0, "total": None, "speed": 0, "reason": ""}
                self.rows[url] = row
            previous = row["state"]
            new_state = fields.get("state")
            # A finished row never moves back to queued/active (late progress after success).
            if previous in FINAL_STATES and new_state in (None, "queued", "active", "retrying"):
                fields.pop("state", None)
                fields.pop("speed", None)
            if fields.get("sequence") and row["sequence"]:
                fields.pop("sequence")
            row.update({key: value for key, value in fields.items() if value is not None})
            current = row["state"]
            if current != previous:
                if previous in self.counts:
                    self.counts[previous] -= 1
                if current in self.counts:
                    self.counts[current] += 1
                changed = dict(row)
            self.dirty.add(url)
        if changed is not None and self.on_change:
            self.on_change(changed)

    def set_bytes(self, total: int) -> None:
        with self.lock:
            self.bytes = max(self.bytes, total)

    def add_bytes(self, amount: int) -> None:
        with self.lock:
            self.bytes += amount

    def flush(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            items = [dict(self.rows[url]) for url in self.dirty]
            self.dirty.clear()
            now = time.monotonic()
            self._samples.append((now, self.bytes))
            self._samples = [s for s in self._samples if now - s[0] <= 3.0]
            first = self._samples[0]
            span = now - first[0]
            speed = int((self.bytes - first[1]) / span) if span > 0.2 else 0
            payload = {"type": "files", "items": items, "bytes": self.bytes, "speed": speed,
                       "counts": dict(self.counts), "total": len(self.rows)}
        emit(payload)

    def state(self, url: str) -> str | None:
        with self.lock:
            row = self.rows.get(url)
            return row["state"] if row else None

    def reason(self, url: str) -> str:
        with self.lock:
            row = self.rows.get(url)
            return str(row.get("reason") or "") if row else ""

    def unfinished(self) -> list[str]:
        with self.lock:
            return [url for url, row in self.rows.items() if row["state"] not in FINAL_STATES]


FILES = FileTable()


def _flusher() -> None:
    while not FINISHED.wait(FLUSH_INTERVAL):
        FILES.flush()


# ─── persistence ─────────────────────────────────────────────────────────────

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def load_history() -> set[str]:
    data = _read_json(HISTORY, [])
    return {item for item in data if isinstance(item, str)} if isinstance(data, list) else set()


def save_history(urls: set[str]) -> None:
    _write_json(HISTORY, sorted(urls))


def record_runs(urls: list[str], saved: int, failed: int, skipped: int, stopped: bool) -> None:
    runs = _read_json(RUNS, {})
    if not isinstance(runs, dict):
        runs = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for url in urls:
        entry = runs.get(url) if isinstance(runs.get(url), dict) else {}
        runs[url] = {
            "last_run": now,
            "runs": int(entry.get("runs", 0)) + 1,
            "saved": saved,
            "failed": failed,
            "skipped": skipped,
            "stopped": stopped,
        }
    if len(runs) > MAX_RUN_HISTORY:
        newest = sorted(runs.items(), key=lambda kv: kv[1].get("last_run", ""), reverse=True)[:MAX_RUN_HISTORY]
        runs = dict(newest)
    _write_json(RUNS, runs)


# ─── URL safety ──────────────────────────────────────────────────────────────

def looks_like_file(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(FILE_SUFFIXES)


def blocked_reason(host: str) -> str | None:
    name = host.strip("[]").rstrip(".").lower()
    if not name or name in {"localhost", "metadata.google.internal"} or name.endswith((".localhost", ".local", ".internal")):
        return "Private and local addresses are blocked"
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(name, None)}
    except (socket.gaierror, UnicodeError):
        return f"Could not resolve {name}"
    if not addresses:
        return f"Could not resolve {name}"
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return "Private and local addresses are blocked"
    return None


def public_url(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only http and https URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are blocked")
    reason = blocked_reason(parsed.hostname)
    if reason:
        raise ValueError(reason)
    return raw


def safe_filename(raw: str, fallback: str = "download") -> str:
    """Turn a URL path segment into a file name that cannot leave its folder."""
    name = unquote(raw).replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip().strip(".")
    if not name:
        name = fallback
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.upper() in WINDOWS_RESERVED:
        stem = f"_{stem}"
    ext = ext[:16]
    stem = stem[: 150 - len(ext)]
    return f"{stem}.{ext}" if ext else stem


def unique_path(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(1, 10_000):
        candidate = folder / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError("Too many files with the same name")


_WEB_PAGE_STARTS = (b"<!doctype html", b"<html", b"<head")


def looks_like_web_page(head: bytes, suffix: str) -> bool:
    """An image, video or audio file whose first bytes are an HTML page (login or error page)."""
    if suffix.lower() not in _IMAGE | _VIDEO | _AUDIO:
        return False
    return head[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower().startswith(_WEB_PAGE_STARTS)


def _pick_name(original: str, final: str, disposition: str | None) -> str:
    """Prefer the server's file name, then whichever URL has a file extension."""
    if disposition:
        match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I) or re.search(r'filename="?([^";]+)"?', disposition, re.I)
        if match:
            return safe_filename(match.group(1).strip())
    for candidate in (final, original):
        name = safe_filename(urlparse(candidate).path)
        if "." in name:
            return name
    return safe_filename(urlparse(original).path)


_NAME_LOCK = threading.Lock()
TYPE_FOLDERS_ON = False
_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".heic", ".jxl", ".tif", ".tiff"}
_VIDEO = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ts", ".m3u8", ".flv", ".wmv", ".mpg", ".mpeg"}
_AUDIO = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}


def type_folder(path: str) -> str:
    ext = os.path.splitext(path.lower())[1]
    return "Images" if ext in _IMAGE else "Videos" if ext in _VIDEO else "Audio" if ext in _AUDIO else "Other"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by hand so every hop passes public_url()."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_PROXY_RE = re.compile(r"^https?://(?:[^\s:@/]+(?::[^\s@/]*)?@)?[A-Za-z0-9.-]+(?::\d{1,5})?/?$")


def proxy_url(raw) -> str:
    """The proxy setting if it is an http(s)://host:port address, else ''."""
    text = str(raw or "").strip()
    return text.rstrip("/") if _PROXY_RE.match(text) else ""


def use_proxy(proxy: str) -> None:
    """Direct file downloads go through the same proxy as the engine."""
    global _OPENER
    handlers = [_NoRedirect]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    _OPENER = urllib.request.build_opener(*handlers)


_ORDER = None
_ORDER_STAMP: tuple[int, int] | None = None
NUMBERED = True  # "Numbered filenames" setting for this run
NUMBER_MODE = "suffix"  # Sequence Numbering: suffix (name-001.jpg), prefix (001_name.jpg) or off
JOB_KEY = ""  # same links, same job: numbers and names survive a resume or restart
MANIFEST_MAX_LINES = 20_000


def _numbering(config: dict) -> str:
    value = str(config.get("numbering") or "").lower()
    if value in {"suffix", "prefix", "off"}:
        return value
    if config.get("sequencePrefix") is False:
        return "off"  # "Numbered filenames" was off before 2.5
    return "suffix"


DOWNLOAD_MODE_LABELS = {"auto": "Auto", "balanced": "Balanced", "max": "Maximum speed", "low": "Low resource",
                        "custom": "Custom"}


def _choice(value: object, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").lower()
    return text if text in allowed else default


NAMING_ROWS: dict[str, dict] = {}  # url -> this run's naming record, for the job database


def _numbered_filename(url: str, original: str) -> tuple[int, str]:
    """Apply the engine's source-order number so fallback files continue its sequence."""
    global _ORDER, _ORDER_STAMP
    try:
        try:
            info = MANIFEST.stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        if _ORDER is not None and stamp != _ORDER_STAMP:
            _ORDER.reset(str(MANIFEST), JOB_KEY or None)  # the engine handed out more numbers since
            _ORDER_STAMP = stamp
        if _ORDER is None:
            import importlib.util

            spec = importlib.util.spec_from_file_location("nexdrop_source_order", VENDOR / "cyberdrop_dl" / "source_order.py")
            if spec is None or spec.loader is None:
                return 0, original
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.reset(str(MANIFEST), JOB_KEY or None)  # pick up the numbers the engine already handed out
            _ORDER = module
            _ORDER_STAMP = stamp
        known = _ORDER.stored(url)
        if known and known.get("name") and known.get("mode") == NUMBER_MODE:
            return int(known.get("sequence") or 0), str(known["name"])
        number, name = _ORDER.numbered_filename(url, original, enabled=NUMBERED, mode=NUMBER_MODE)
        _write_manifest({"url": _ORDER.public_url(url), "sequence": number, "name": name, "original": original,
                         "source": "original filename", "strategy": "off", "mode": NUMBER_MODE})
        try:
            info = MANIFEST.stat()
            _ORDER_STAMP = (info.st_mtime_ns, info.st_size)
        except OSError:
            pass
        return number, name
    except Exception:  # noqa: BLE001 - numbering is cosmetic
        return 0, original


def job_key(urls: list[str]) -> str:
    """One key per set of links, so pressing Resume or rechecking a watched thread is the same job."""
    import hashlib

    def norm(url: str) -> str:
        parts = urlparse(url.strip())
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}" + (f"?{parts.query}" if parts.query else "")

    clean = sorted({norm(u) for u in urls if u})
    return hashlib.sha1("\n".join(clean).encode("utf-8")).hexdigest()[:16] if clean else ""


def _write_manifest(row: dict) -> None:
    """Persist a file's naming record: number, name, folder, original name and where the name came from."""
    if JOB_KEY:
        row = {**row, "job": JOB_KEY}
    try:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with MANIFEST.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def trim_manifest() -> None:
    """Keep the order manifest bounded; it only needs recent URLs to keep their numbers."""
    try:
        if not MANIFEST.is_file() or MANIFEST.stat().st_size < 4_000_000:
            return
        lines = MANIFEST.read_text(encoding="utf-8", errors="replace").splitlines()[-MANIFEST_MAX_LINES:]
        MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def http_save(url: str, folder: Path) -> dict:
    current = public_url(url)
    for _ in range(6):
        request = urllib.request.Request(current, headers={"Accept-Encoding": "identity", "User-Agent": "NEXDROP/2.1"})
        try:
            response = _OPENER.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        status = getattr(response, "status", None) or response.getcode()
        if status in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError(f"HTTP {status} without a Location header")
            current = public_url(urljoin(current, location))
            continue
        if status >= 400:
            response.close()
            raise ValueError(f"HTTP {status}")
        length = response.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else None
        if total and total > MAX_BYTES:
            response.close()
            raise ValueError("File exceeds the 80 MB transfer limit")
        target_dir = folder  # NEXDROP: no "Loose Files" folder
        if TYPE_FOLDERS_ON:
            target_dir = target_dir / type_folder(urlparse(current).path)
        target_dir.mkdir(parents=True, exist_ok=True)
        number, name = _numbered_filename(url, _pick_name(url, current, response.headers.get("Content-Disposition")))
        with _NAME_LOCK:  # parallel fallback downloads must never pick the same name
            target = unique_path(target_dir, safe_filename(name))
            target.touch()
        partial = target.with_name(target.name + ".part")
        read = 0
        started = time.monotonic()
        FILES.update(url, name=target.name, sequence=number or None, state="active", total=total, read=0)
        try:
            with partial.open("wb") as handle:
                first = True
                while True:
                    if STOP.is_set():
                        raise ValueError("Stopped")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    if first:
                        first = False
                        if looks_like_web_page(chunk, target.suffix):
                            raise ValueError("Got a web page instead of the file")
                    read += len(chunk)
                    if read > MAX_BYTES:
                        raise ValueError("File exceeds the 80 MB transfer limit")
                    handle.write(chunk)
                    FILES.add_bytes(len(chunk))
                    elapsed = max(time.monotonic() - started, 1e-3)
                    FILES.update(url, read=read, speed=int(read / elapsed))
            # urllib returns a short body without an error when the connection drops early.
            if total is not None and read != total:
                raise ValueError(f"Incomplete transfer: got {read:,} of {total:,} bytes")
            if read == 0:
                raise ValueError("Incomplete transfer: the file is empty")
            os.replace(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            target.unlink(missing_ok=True)  # release the reserved name
            raise
        finally:
            response.close()
        FILES.update(url, state="done", read=read, total=read, speed=0, path=str(target))
        return {"url": url, "filename": target.name, "bytes": read}
    raise ValueError("Too many redirects")


# ─── cookies ─────────────────────────────────────────────────────────────────

# ─── cookies ─────────────────────────────────────────────────────────────────

def prepare_cookies(account_folder: str, fallback_files: list[str], account_texts: dict | None = None) -> str | None:
    """Build this run's cookie folder: per-site account cookies plus any fallback files.

    A fresh folder each run, so a changed or removed cookie file never lingers. Saved site
    cookies arrive decrypted through stdin (`account_texts`) and only exist on disk during the
    run: clear_run_cookies() removes the folder when the run ends.
    """
    clear_run_cookies()
    RUN_COOKIES.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, text in sorted((account_texts or {}).items()):
        text = convert_any(str(text or ""))
        if text.count("\n") >= 1 and "\t" in text:
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(name))[:40]
            (RUN_COOKIES / f"account-{safe}.txt").write_text(text, encoding="utf-8")
            written += 1
    sources: list[Path] = []
    if account_folder:
        folder = Path(account_folder)
        if folder.is_dir():
            sources.extend(sorted(folder.glob("*.txt")))
    sources.extend(Path(item) for item in dict.fromkeys(f for f in fallback_files if f))
    for index, path in enumerate(sources):
        try:
            if not path.is_file() or path.stat().st_size > 5_000_000:
                log("warning", f"Cookie file skipped (missing or over 5 MB): {path.name}")
                continue
            text = convert_any(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            log("warning", f"Cookie file could not be read: {path.name} ({error.strerror})")
            continue
        if text.count("\n") <= 1:
            log("warning", f"No cookies found in {path.name}. Export a Netscape cookies.txt or storage_state.json.")
            continue
        (RUN_COOKIES / f"{index:02d}.txt").write_text(text, encoding="utf-8")
        written += 1
    if written:
        return str(RUN_COOKIES)
    return None


def clear_run_cookies() -> None:
    """Cookie copies are sign-ins in plain text; none stays on disk after a run."""
    for folder in (RUN_COOKIES, APPDATA / "cookies" / "engine"):
        shutil.rmtree(folder, ignore_errors=True)


def note_source(url: str, message: str, category: str) -> None:
    try:
        from source_registry import log_dir, record_event

        record_event(log_dir(), url, reason=message[:240], category=category, retried=False)
    except Exception:  # noqa: BLE001 - the registry is a diagnostic aid only
        return


def flare_base(config: dict) -> str:
    raw = str(config.get("flareSolverUrl") or "http://127.0.0.1:8191").strip().rstrip("/")
    return raw[:-3] if raw.endswith("/v1") else raw


def flare_alive(base: str) -> bool:
    request = urllib.request.Request(
        base.rstrip("/") + "/v1",
        data=b'{"cmd":"sessions.list"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            body = response.read(800).decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    return '"status"' in body and "ok" in body


def flare_binary() -> Path | None:
    folder = ROOT / "runtime" / "flaresolverr"
    for name in ("flaresolverr.exe", "flaresolverr"):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


FLARE_LOG = APPDATA / "logs" / "flaresolverr.log"
FLARE_WAIT = 150  # seconds; the first start unpacks and launches Chrome, which can take over a minute


def _flare_tail(lines: int = 6) -> list[str]:
    try:
        text = FLARE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line.strip() for line in text[-lines:] if line.strip()]


def _wait_for_flare(base: str, proc: subprocess.Popen | None) -> bool:
    started = time.monotonic()
    next_note = 20
    while time.monotonic() - started < FLARE_WAIT and not STOP.is_set():
        if flare_alive(base):
            return True
        if proc is not None and proc.poll() is not None:
            return False
        elapsed = time.monotonic() - started
        if elapsed >= next_note:
            log("info", f"Still waiting for FlareSolverr ({int(elapsed)} s). The first start can take a minute or two.")
            next_note += 20
        time.sleep(1)
    return flare_alive(base)


def ensure_flaresolverr(config: dict) -> str | None:
    """FlareSolverr solves DDoS-Guard and Cloudflare checks.

    The window starts it in the background when NEXDROP opens and keeps it
    running between runs; this only waits for it. If the window is not
    managing it, the session starts it for this run.
    """
    global FLARE, FLARE_OWNED
    if config.get("flareSolver") is False:
        return None
    base = flare_base(config)
    if flare_alive(base):
        log("info", f"FlareSolverr is ready at {base}.")
        return base + "/v1"
    binary = flare_binary()
    if binary is None:
        log("warning", "FlareSolverr is not installed in runtime\\flaresolverr, so sites behind DDoS-Guard or Cloudflare may fail.")
        return None
    emit({"type": "phase", "stage": "start", "message": "Waiting for FlareSolverr"})
    proc = None
    if os.environ.get("NEXDROP_FLARE_MANAGED") != "1":
        env = os.environ.copy()
        env.update({"PORT": str(urlparse(base).port or 8191), "HOST": "127.0.0.1", "LOG_LEVEL": "info", "LOG_HTML": "false", "HEADLESS": "true"})
        FLARE_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = FLARE_LOG.open("a", encoding="utf-8")
            handle.write(f"\n--- started by a download run {datetime.now().isoformat(timespec='seconds')} ---\n")
            handle.flush()
        except OSError:
            handle = subprocess.DEVNULL
        kwargs: dict = {"cwd": str(binary.parent), "env": env, "stdout": handle, "stderr": subprocess.STDOUT}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            FLARE = subprocess.Popen([str(binary)], **kwargs)
        except OSError as error:
            # Antivirus quarantine or a blocked exe must not stop downloads that don't need it.
            log("warning", f"FlareSolverr could not start ({error.strerror or error}). Continuing without it.")
            return None
        finally:
            if handle is not subprocess.DEVNULL:
                handle.close()  # the child has its own copy
        FLARE_OWNED = True
        proc = FLARE
    if _wait_for_flare(base, proc):
        log("info", "FlareSolverr is ready.")
        return base + "/v1"
    if STOP.is_set():
        return None
    log("warning", f"FlareSolverr did not become ready within {FLARE_WAIT} s. Continuing without it, so sites behind DDoS-Guard or Cloudflare may fail.")
    for line in _flare_tail():
        log("warning", f"FlareSolverr: {line[:240]}")
    log("info", f"Full FlareSolverr output: {FLARE_LOG}")
    _stop_flare()
    return None


def _kill_tree(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _stop_flare() -> None:
    global FLARE, FLARE_OWNED
    proc, owned = FLARE, FLARE_OWNED
    FLARE, FLARE_OWNED = None, False
    if owned:
        _kill_tree(proc)


def request_stop() -> None:
    if STOP.is_set() or FINISHED.is_set():
        return
    STOP.set()
    log("warning", "Stopping…")
    _kill_tree(PROC)


def _listen_for_stop() -> None:
    for line in sys.stdin:
        if line.strip() == "stop":
            request_stop()
            return
    # stdin closed: the window went away.
    request_stop()


def _on_signal(_signum, _frame) -> None:
    request_stop()


# ─── engine database upkeep ──────────────────────────────────────────────────

def maintain_engine_db() -> None:
    """Fold the write-ahead log back in and compact the engine database when it is mostly empty.

    Runs only while the engine is not running. The stock engine reserved
    250 MB inside the database on every start; one compaction reclaims that,
    and the bridge keeps the reserve at 16 MB afterwards.
    """
    import sqlite3

    db = APPDATA / "cyberdrop.db"
    wal = db.with_name(db.name + "-wal")
    if not db.is_file():
        return
    try:
        before = db.stat().st_size + (wal.stat().st_size if wal.is_file() else 0)
        if before < 32 * 1024 * 1024:
            return
        conn = sqlite3.connect(db, timeout=5)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
            # More than the bridge's 16 MB reserve is left over from the engine's old 250 MB one.
            if free * page_size > 64 * 1024 * 1024:
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        after = db.stat().st_size + (wal.stat().st_size if wal.is_file() else 0)
        if before - after > 8 * 1024 * 1024:
            log("info", f"Compacted the engine database from {before // 1048576} MB to {max(1, after // 1048576)} MB.")
    except Exception as error:  # noqa: BLE001 - upkeep must never block a download
        write_run_log(f"{_stamp()} [info] Database upkeep skipped: {error}")


# ─── run ─────────────────────────────────────────────────────────────────────

class Hints:
    """Turns the engine's raw failures into one clear next step each, shown at the end of a run."""

    def __init__(self, order_mode: str, *, flare_ready: bool) -> None:
        self.order_mode = order_mode
        self.flare_ready = flare_ready
        self.page_instead_of_file: dict[str, int] = {}
        self.quota = 0
        self.gone: dict[str, int] = {}
        self.site_error: dict[str, int] = {}
        self.busy: dict[str, int] = {}
        self.expired: dict[str, int] = {}
        self.nested = 0
        self.password_links: list[str] = []
        self.no_such_host: dict[str, int] = {}
        self.unreachable: dict[str, int] = {}
        self.bot_blocked: list[str] = []
        self.files_in_thread = 0

    def failed(self, url: str, reason: str) -> None:
        """Count one failure per file (a retry that fails again replaces the earlier count)."""
        self._failed_urls = getattr(self, "_failed_urls", {})
        earlier = self._failed_urls.pop(url, None)
        if earlier:
            self._uncount(url, earlier)
        if "Oops! We ran into some problems" in reason or "do not have permission" in reason:
            bucket = "site_error"
        elif "Invalid Content Type" in reason or "DDoS-Guard" in reason:
            # The retry's message drops the page title; keep the site's own error page if the first try saw it.
            bucket = "site_error" if earlier == "site_error" else "page_instead_of_file"
        elif "403" in reason and "/storage/" in url and host_of(url).endswith("scdn.st"):
            bucket = "expired"
        elif "509" in reason or "Bandwidth Limit" in reason:
            bucket = "quota"
        elif "ClientConnectorDNSError" in reason or "getaddrinfo failed" in reason or "Name or service not known" in reason:
            bucket = "no_such_host"
        elif "Connection timeout to host" in reason or "Server unreachable" in reason:
            bucket = "unreachable"
        elif "503" in reason or "502" in reason or "429" in reason:
            bucket = "busy"
        elif "404" in reason or "410" in reason:
            bucket = "gone"
        else:
            return
        self._failed_urls[url] = bucket
        counts = getattr(self, bucket)
        if isinstance(counts, dict):
            host = host_of(url)
            counts[host] = counts.get(host, 0) + 1
        else:
            setattr(self, bucket, counts + 1)

    def _uncount(self, url: str, bucket: str) -> None:
        counts = getattr(self, bucket)
        if isinstance(counts, dict):
            host = host_of(url)
            if counts.get(host):
                counts[host] -= 1
                if not counts[host]:
                    del counts[host]
        else:
            setattr(self, bucket, max(0, counts - 1))

    def forum(self, message: str) -> str:
        found = dict(re.findall(r"(\w+)=(\S+)", message))
        files = int(found["files"]) if found.get("files", "").isdigit() else 0
        self.files_in_thread += files
        parts = []
        if found.get("posts"):
            parts.append(f"{found['posts']} posts")
        parts.append(f"{files} files")
        if found.get("nested", "0") not in ("", "0"):
            parts.append(f"{found['nested']} linked threads")
        return f"Scanned {found.get('url', 'the thread')}: {', '.join(parts)}."

    def scrape_failed(self, url: str, message: str) -> tuple[str, str]:
        detail = message.removeprefix("Scrape Failed:").strip()
        if "Max Children" in message or "depth limit" in message:
            self.nested += 1
            return "info", f"Skipped linked thread {url}"
        if "Unknown URL path" in message:
            return "info", f"No downloader for this kind of link: {url}"
        if "Password Protected" in message or "password protected" in message:
            self.password_links.append(url)
            return "warning", f"{url} needs a password. Add ?password=THE_PASSWORD to the end of the link and start it again."
        if "DDoS-Guard" in message or "anti-bot" in message or "Turnstile" in message:
            self.bot_blocked.append(host_of(url))
            return "warning", f"Blocked by a bot check: {url}"
        if "sub-exceptions" in message:
            return "warning", f"{url} failed with several errors at once. Details are in the engine log."
        return "warning", detail[:300] or message[:300]

    def forget_saved(self, is_saved) -> None:
        """Drop counts for files that a retry pass saved after all."""
        for url, bucket in list(getattr(self, "_failed_urls", {}).items()):
            if is_saved(url):
                self._uncount(url, self._failed_urls.pop(url))

    def summary(self) -> list[str]:
        out = []
        for host, count in self.page_instead_of_file.items():
            out.append(
                f"{count} file(s) from {host} came back as a web page instead of the file, even after a FlareSolverr retry. "
                "The page title is in the run log. If it is a login or permission page, sign in to the site in your browser, "
                "export fresh cookies for it under Settings > Sign-ins, then use Retry unfinished."
            )
        for host, count in self.site_error.items():
            out.append(
                f"{count} file(s) from {host} returned the site's own error page (\"Oops! We ran into some problems\"), "
                "not a bot check. The attachment was removed, or your account can't view it. Open one of the links in your browser "
                "while signed in to check; if it opens there, export fresh cookies and use Retry unfinished."
            )
        for host, count in self.expired.items():
            out.append(
                f"{count} Bunkr storage link(s) on {host} were refused (403). These links expire, so a copied storage link "
                "stops working. Paste the Bunkr page link (bunkr.cr/f/...) instead, or use Retry unfinished on the run that found them."
            )
        for host, count in self.busy.items():
            out.append(f"{count} file(s) on {host} got a temporary server error (busy or overloaded). Use Retry unfinished in a few minutes.")
        for host, count in self.gone.items():
            if count >= 3:
                out.append(f"{count} file(s) on {host} no longer exist (404 Not Found). The host has deleted them, so retrying won't help.")
        for host, count in self.no_such_host.items():
            out.append(
                f"{count} file(s) on {host} failed because that server name no longer exists (DNS lookup failed). "
                "The site has moved or shut down; the files can only come from a newer link in the thread."
            )
        for host, count in self.unreachable.items():
            out.append(
                f"{count} file(s) from {host} failed because its file servers never answered (connection timed out). "
                f"{host} itself opened, so your internet provider or network is most likely blocking its file servers. "
                "Open one of the failed links in your browser: if it doesn't load there either, turn on a VPN or set a proxy "
                "under Settings > Speed > Proxy, then use Retry unfinished."
            )
        if self.password_links:
            out.append(
                f"{len(self.password_links)} link(s) need a password. Add ?password=THE_PASSWORD to the end of each link "
                "(the password is usually in the post) and start them again."
            )
        if self.quota:
            out.append(
                f"{self.quota} MEGA file(s) hit MEGA's free transfer quota (509 Bandwidth Limit Exceeded). "
                "Try again in a few hours, or sign in to MEGA under Settings > Sign-ins."
            )
        if self.nested:
            out.append(
                f"Skipped {self.nested} linked thread(s) because Nested threads to follow is 0. "
                "Set it to 1 in Settings > Downloads to include them."
            )
        if self.bot_blocked:
            sites = ", ".join(sorted(set(self.bot_blocked)))
            if self.flare_ready:
                out.append(f"FlareSolverr could not get past the bot check on {sites}. Signing in with that site's cookies may help.")
            else:
                out.append(f"{sites} needs a bot check solved, but FlareSolverr wasn't ready for this run. Start the run again once it is ready.")
        if self.order_mode == "source" and self.files_in_thread >= 50:
            out.append(
                f"This thread has {self.files_in_thread} files. Page order downloads one at a time; "
                "Parallel keeps the same numbering and is several times faster."
            )
        return out


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _job_status(saved: int, failed: int, skipped: int, stopped: bool) -> str:
    if stopped:
        return "stopped"
    if not saved and not skipped:
        return "failed"
    return "completed_with_errors" if failed else "completed"


RETRY_WAIT = int(os.environ.get("NEXDROP_RETRY_WAIT", "45"))  # seconds before the automatic retry pass
# The source registry keeps its own, older category names.
REGISTRY_CATEGORY = {"not_found": "unavailable", "auth_required": "access_denied", "expired_url": "access_denied",
                     "timeout": "timeout", "unreachable": "timeout"}


def is_transient(reason: str, url: str = "") -> bool:
    """Worth one more try in this run: busy, rate-limited, dropped, incomplete, or an expired link."""
    return error_classes.is_retryable(error_classes.classify(reason, url))


def retry_pass(failed: list[str], referers: dict[str, str], payload: dict, engine_pass, direct_fetch, direct: set[str]) -> None:
    """Try files that failed with a temporary error once more, after a short pause.

    The engine is pointed at the page each file came from (album, post or
    thread) so it requests the file with the right headers; files already
    saved are skipped from history.
    """
    retry_files = [url for url in dict.fromkeys(failed) if is_transient(FILES.reason(url), url)]
    if not retry_files:
        return
    targets = list(dict.fromkeys(referers.get(url) or url for url in retry_files))
    expired = [url for url in retry_files if error_classes.classify(FILES.reason(url), url) == "expired_url"]
    for url in retry_files:
        FILES.update(url, state="retrying", reason=f"retrying in {RETRY_WAIT} s")
    log("info", f"{len(retry_files)} file(s) failed with a temporary error. Trying them again in {RETRY_WAIT} s.")
    if expired:
        log("info", f"{len(expired)} of them had an expired download link. Their pages are read again for fresh links.")
    saved_before = {url for url, row in FILES.rows.items() if row["state"] == "done"}
    if STOP.wait(RETRY_WAIT):
        for url in retry_files:
            FILES.update(url, state="failed", reason="stopped before the retry")
        return
    emit({"type": "phase", "stage": "download", "message": "Retrying failed files"})
    for url in retry_files:
        failed.remove(url)
    by_engine = [u for u in targets if u not in direct]
    if by_engine:
        retry_log = str(ENGINE_LOG_PATH).replace(".engine.log", ".retry.engine.log") if ENGINE_LOG_PATH else ""
        engine_pass({**payload, "urls": by_engine, "ignoreHistory": False, "startPage": 1, "endPage": None, "engineLog": retry_log})
    for url in retry_files:
        if url in direct and not STOP.is_set():
            direct_fetch(url)
    refreshed = _match_refreshed_links(retry_files, saved_before)
    for url in retry_files:
        if url in refreshed:
            # The file has its own new row; this one only says where it went.
            FILES.update(url, state="skipped", speed=0, reason=f"saved from a fresh link as {refreshed[url].get('name') or 'a new row'}")
        elif FILES.state(url) not in FINAL_STATES:
            FILES.update(url, state="failed", reason="still failing after a retry")
            failed.append(url)
        elif FILES.state(url) == "failed":
            failed.append(url)
    recovered = sum(1 for url in retry_files if FILES.state(url) == "done") + len(refreshed)
    log("ok" if recovered else "warning", f"Retry saved {recovered} of {len(retry_files)} file(s).")


def _match_refreshed_links(retry_files: list[str], saved_before: set[str]) -> dict[str, dict]:
    """Failed rows whose file was saved during the retry pass under a fresh link.

    An expired CDN link comes back from its page with a new token or on another server, so the
    saved row has a different link. The file is the same when the link's file name is the same
    (at least 6 characters, unique among the new rows)."""
    with FILES.lock:
        fresh = [dict(row) for url, row in FILES.rows.items() if row["state"] == "done" and url not in saved_before]
    by_name: dict[str, list[dict]] = {}
    for row in fresh:
        key = unquote(urlparse(row["url"]).path.rsplit("/", 1)[-1]).lower()
        if len(key) >= 6:
            by_name.setdefault(key, []).append(row)
    found = {}
    for url in retry_files:
        if FILES.state(url) == "done":
            continue
        key = unquote(urlparse(url).path.rsplit("/", 1)[-1]).lower()
        matches = by_name.get(key) or []
        if len(key) >= 6 and len(matches) == 1:
            found[url] = matches[0]
    return found


_ENGINE_DB = None  # read-only connection, opened once per run (a big chat checks every message)


def engine_has_telegram(link: str) -> bool:
    """True when the engine's public t.me crawler already saved this message."""
    global _ENGINE_DB
    try:
        if _ENGINE_DB is None:
            db = APPDATA / "cyberdrop.db"
            if not db.is_file():
                return False
            import sqlite3

            _ENGINE_DB = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5, check_same_thread=False)
        row = _ENGINE_DB.execute(
            # link, or link#1, link#2 ...: a range keeps it on the referer index ('$' follows '#')
            "SELECT 1 FROM media WHERE domain = 'telegram' AND completed = 1 AND referer >= ? AND referer < ? LIMIT 1",
            (link, link + "$"),
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001 - history is a convenience
        return False


def _close_engine_db() -> None:
    global _ENGINE_DB
    if _ENGINE_DB is not None:
        try:
            _ENGINE_DB.close()
        except Exception:  # noqa: BLE001
            pass
        _ENGINE_DB = None


class _TelegramHooks:
    """Connects the Telegram account downloader to this run's file list and history."""

    def __init__(self, *, redownload: bool, skip_urls: set[str], saved: list, failed: list, skipped: list) -> None:
        self.stop = STOP
        self._history = set() if redownload else load_history()
        self._redownload = redownload
        self._skip_urls = skip_urls
        self._saved, self._failed, self._skipped = saved, failed, skipped

    def update(self, url: str, **fields) -> None:
        FILES.update(url, **fields)

    def add_bytes(self, amount: int) -> None:
        FILES.add_bytes(amount)

    def number(self, url: str, name: str) -> tuple[int, str]:
        return _numbered_filename(url, name)

    def skip(self, url: str) -> str:
        if url in self._skip_urls:
            return "already downloaded"
        if self._redownload:
            return ""
        if url in self._history or engine_has_telegram(url):
            return "already downloaded"
        return ""

    def skipped(self, url: str, reason: str) -> None:
        if FILES.state(url) == "done":
            return
        FILES.update(url, state="skipped", reason=reason, speed=0)
        if reason.startswith("already"):
            self._skipped.append(url)

    def saved(self, url: str, _path) -> None:
        self._saved.append(url)

    def failed(self, url: str, reason: str) -> None:
        if url not in self._failed:
            self._failed.append(url)
        FILES.update(url, state="failed", reason=reason[:200], speed=0)

    def log(self, level: str, text: str) -> None:
        log(level, text)


def run(config: dict) -> int:  # noqa: C901, PLR0912, PLR0915
    started = time.monotonic()
    seen: set[str] = set()
    raw_urls: list[str] = []
    for item in config.get("urls") or []:
        url = str(item).strip()
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            raw_urls.append(url)
    if len(raw_urls) > MAX_URLS:
        log("warning", f"Only the first {MAX_URLS} of {len(raw_urls)} URLs are used in one run.")
        raw_urls = raw_urls[:MAX_URLS]
    urls: list[str] = []
    rejected: list[str] = []
    for url in raw_urls:
        try:
            urls.append(public_url(url))
        except ValueError as error:
            rejected.append(url)
            log("error", f"{url}: {error}")
    for item in config.get("unsupported") or []:
        note_source(str(item), "No BitTorrent handler is included", "missing_handler")
        log("error", f"Unsupported: {str(item)[:120]}. Magnet links and .torrent files need a BitTorrent client, which this build does not include.")

    folder = Path(str(config.get("downloadsDir") or DOWNLOADS)).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    redownload = bool(config.get("redownload"))
    open_run_log(config, urls + rejected)
    emit({"type": "run", "root": str(folder), "urls": urls,
          "log": str(RUN_LOG_PATH or ""), "engineLog": str(ENGINE_LOG_PATH or ""),
          "namingLog": str(NAMING_LOG_PATH or "")})
    if not urls:
        emit({"type": "error", "message": "No public http(s) URLs to download."})
        return _finish(urls, folder, [], rejected, [], started, stopped=False)

    start_page = _int(config.get("startPage"), 1, 1, 100_000)
    end_page = _int(config.get("endPage"), 0, 0, 100_000) or None
    if end_page is not None and end_page < start_page:
        emit({"type": "error", "message": "Last page cannot be lower than the start page."})
        return _finish(urls, folder, [], [], [], started, stopped=False)

    store = None
    job_id = None
    try:
        store = JobStore(APPDATA / "jobs.sqlite")
        recovered = store.recover()
        if recovered:
            log("warning", f"Marked {len(recovered)} unfinished job(s) as interrupted. They were not started again.")
        job_id = store.start_job(urls, str(folder))
    except Exception as error:  # noqa: BLE001
        log("warning", f"Job history could not be opened: {error}")
        store = None

    def remember(row: dict) -> None:
        log_file_outcome(row)
        if not store or not job_id:
            return
        event = {"done": "success", "skipped": "skipped", "failed": "failure", "retrying": "retry",
                 "cancelled": "cancelled"}.get(row["state"], "queued")
        try:
            store.note(job_id, {
                "url": row["url"], "host": row.get("host") or "", "event": event,
                "sequence": row.get("sequence") or 0, "name": row.get("name") or "",
                "bytes_read": row.get("read") or 0, "bytes_total": row.get("total") or 0,
                "status": row.get("reason") or "", "destination": row.get("path") or "",
                "category": row.get("category") or "",
                "naming": NAMING_ROWS.get(row["url"]),
            })
        except Exception:  # noqa: BLE001
            return

    FILES.on_change = remember
    threading.Thread(target=_flusher, daemon=True).start()
    emit({"type": "phase", "stage": "start", "message": f"Preparing {len(urls)} URL(s)"})
    flare_url = ensure_flaresolverr(config)
    fallback = [str(c) for c in (config.get("cookieFiles") or []) if c]
    if config.get("cookies"):
        fallback.append(str(config["cookies"]))
    account_texts = config.get("accountCookies") if isinstance(config.get("accountCookies"), dict) else {}
    cookie_folder = prepare_cookies(str(config.get("cookieFolder") or ""), fallback, account_texts)
    if STOP.is_set():
        return _finish(urls, folder, [], rejected, [], started, stopped=True, store=store, job_id=job_id)

    global NUMBERED, NUMBER_MODE, JOB_KEY, TYPE_FOLDERS_ON
    TYPE_FOLDERS_ON = bool(config.get("typeFolders"))
    NUMBER_MODE = _numbering(config)
    NUMBERED = NUMBER_MODE != "off"
    JOB_KEY = job_key(urls)
    trim_manifest()
    proxy = proxy_url(config.get("proxy"))
    proxy_scope = "simpcity" if config.get("proxyScope") == "simpcity" else "all"
    if config.get("proxy") and not proxy:
        log("warning", "The proxy setting is not an http:// or https:// address with a host and port, so no proxy is used.")
    elif proxy and proxy_scope == "simpcity":
        log("info", "SimpCity pages and attachments go through the proxy from Settings > Speed. Other sites connect directly.")
    elif proxy:
        log("info", "Downloads go through the proxy from Settings > Speed. Telegram account downloads connect directly.")
    use_proxy(proxy if proxy_scope == "all" else "")
    order_mode = config.get("orderMode") if config.get("orderMode") in {"source", "parallel", "fastest"} else "source"
    payload = {
        "urls": urls,
        "attempts": _int(config.get("attempts"), 2, 1, 3),
        "maxDownloads": _int(config.get("maxDownloads"), 16, 1, 32),
        "perHost": _int(config.get("perHost"), 6, 1, 16),
        "serverSlots": _int(config.get("serverSlots"), 3, 1, 6),
        "tiktokAudio": bool(config.get("tiktokAudio")),
        "typeFolders": bool(config.get("typeFolders")),
        "fileTypes": config.get("fileTypes") if isinstance(config.get("fileTypes"), dict) else {},
        "minImageKB": _int(config.get("minImageKB"), 0, 0, 100_000),
        "skipUrls": [str(u) for u in (config.get("skipUrls") or [])][:200_000],
        "connectionTimeout": _int(config.get("connectionTimeout"), 15, 10, 30),
        "readTimeout": _int(config.get("readTimeout"), 60, 30, 300),
        "threadDepth": _int(config.get("threadDepth"), 0, 0, 3),
        "speedLimit": _int(config.get("speedLimit"), 0, 0, 10_000 * 1024 * 1024),
        "proxy": proxy,
        "proxyScope": proxy_scope,
        "simpcityGap": _int(config.get("simpcityGap"), 2, 1, 30),
        "ignoreHistory": redownload,
        "deepScrape": bool(config.get("deepScrape")),
        "cookieFolder": cookie_folder or "",
        "downloadFolder": str(folder),
        "flareSolverUrl": flare_url or "",
        "startPage": start_page,
        "endPage": end_page,
        "orderMode": order_mode,
        "sequencePrefix": NUMBERED,
        "numbering": NUMBER_MODE,
        "albumNames": True if config.get("albumNames") is None else bool(config.get("albumNames")),
        # Smart naming (2.5). Missing settings take the defaults from the naming spec.
        "smartNames": True if config.get("smartNames") is None else bool(config.get("smartNames")),
        "nameStrategy": _choice(config.get("nameStrategy"), ("auto", "original", "media", "post", "album"), "auto"),
        "folderNaming": _choice(config.get("folderNaming"), ("auto", "album", "post", "original"), "auto"),
        "siteInFolder": bool(config.get("siteInFolder")),
        "replaceGenericFolders": bool(config.get("replaceGenericFolders")),
        "titleFolders": True if config.get("titleFolders") is None else bool(config.get("titleFolders")),
        "keepMeaningfulNames": True if config.get("keepMeaningfulNames") is None else bool(config.get("keepMeaningfulNames")),
        "maxNameLength": _int(config.get("maxNameLength"), 120, 20, 200),
        "jobKey": JOB_KEY,
        "folderRegistry": str(FOLDER_REGISTRY),
        "postFolders": True if config.get("postFolders") is None else bool(config.get("postFolders")),
        "orderManifest": str(MANIFEST),
        "auth": config.get("auth") if isinstance(config.get("auth"), dict) else {},
        "engineLog": str(ENGINE_LOG_PATH or ""),
        # Smart naming V2: the naming report, names you corrected, titles inside files
        "namingReport": str(NAMING_LOG_PATH or ""),
        "namingCorrections": str(CORRECTIONS) if config.get("learnNames") is not False else "",
        "embeddedTitles": bool(config.get("embeddedTitles")),
    }
    if order_mode != "source":
        mode = _choice(config.get("downloadMode"), tuple(DOWNLOAD_MODE_LABELS), "custom")
        log("info", f"Download mode: {DOWNLOAD_MODE_LABELS[mode]}, {payload['maxDownloads']} at once, "
                    f"{payload['perHost']} per site, {payload['serverSlots']} per server.")
    maintain_engine_db()  # tidy up after an earlier crash or forced stop
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(VENDOR),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "TWITTER_MAX_EMPTY_PAGES": "2",
        "CDL_APPDATA_FOLDER": str(APPDATA),
    })
    ffmpeg_dir = ROOT / "runtime" / "ffmpeg"
    if ffmpeg_dir.is_dir():
        env["PATH"] = str(ffmpeg_dir) + os.pathsep + env.get("PATH", "")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    saved_urls: list[str] = []
    failed: list[str] = list(rejected)
    skipped_urls: list[str] = []
    duplicates: list[tuple[str, str]] = []  # (new file, the earlier file with the same content)
    unsupported: list[str] = []
    scrape_notes: list[str] = []
    referers: dict[str, str] = {}
    forum_note = ""
    downloading = False
    hints = Hints(order_mode, flare_ready=bool(flare_url))

    def engine_pass(pass_payload: dict) -> int:
        """Run the engine once and fold its events into this run's state."""
        global PROC
        nonlocal forum_note, downloading
        bytes_base = FILES.bytes  # each engine process counts from zero
        proc = subprocess.Popen(
            [PYTHON, "-u", str(BRIDGE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            env=env,
            **kwargs,
        )
        PROC = proc
        if STOP.is_set():
            _kill_tree(proc)
        assert proc.stdin and proc.stdout
        try:
            proc.stdin.write((json.dumps(pass_payload) + "\n").encode())
            proc.stdin.close()
        except OSError:
            pass
        for line in proc.stdout:
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            if not text.startswith("{"):
                if "Traceback" in text or "Error" in text:
                    log("warning", text[:400])
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "order":
                url = str(event.get("url") or "")
                sequence = _int(event.get("sequence"), 0, 0, 10**9)
                if url and sequence:
                    FILES.update(url, sequence=sequence, name=str(event.get("name") or "") or None)
                    row = {"url": url, "sequence": sequence, "name": event.get("name") or ""}
                    for key in ("folder", "original", "source", "confidence", "strategy", "title", "parent", "naming"):
                        if event.get(key) not in (None, ""):
                            row[key] = event[key]
                    _write_manifest(row)
                    if isinstance(event.get("identity"), dict):
                        row = {**row, "identity": event["identity"]}  # job database only, not the manifest
                    NAMING_ROWS[url] = row
            elif kind == "renamed":
                # "Read titles inside files" renamed a finished file
                url = str(event.get("url") or "")
                name = str(event.get("name") or "")
                if url and name:
                    row = NAMING_ROWS.get(url) or {"url": url}
                    row.update(name=name, source=str(event.get("source") or "title inside the file"), confidence=0.7)
                    NAMING_ROWS[url] = row
                    FILES.update(url, name=name, path=str(event.get("path") or "") or None)
                    if FILES.on_change and url in FILES.rows:
                        FILES.on_change(dict(FILES.rows[url]))  # the job database gets the new name
                    log("info", f"Named {name} from the {row['source']} inside the file.")
            elif kind == "file_start":
                url = str(event.get("url") or "") or f"file:{event.get('name')}"
                if not downloading:
                    downloading = True
                    emit({"type": "phase", "stage": "download", "message": "Downloading"})
                domain = str(event.get("domain") or "").strip()
                if not domain or domain in {"no_crawler", "http", "download"} or "." not in domain:
                    domain = host_of(url)
                FILES.update(url, name=str(event.get("name") or "") or None, host=domain,
                              state="active", total=event.get("total"), read=0, reason="")
            elif kind in ("progress", "file_end"):
                rows = event.get("files") if kind == "progress" else [event]
                for row in rows or []:
                    url = str(row.get("url") or "")
                    if url:
                        FILES.update(url, read=row.get("read") or 0, total=row.get("total"), speed=row.get("speed") or 0)
                if isinstance(event.get("bytes"), int):
                    FILES.set_bytes(bytes_base + event["bytes"])
            elif kind == "saved":
                url = str(event.get("url") or "")
                if url:
                    saved_urls.append(url)
                    size = event.get("bytes") or None
                    FILES.update(url, state="done", name=str(event.get("filename") or "") or None, read=size, total=size, speed=0, reason="",
                                 path=str(event.get("path") or "") or None)
            elif kind == "failed":
                url = str(event.get("url") or "")
                reason = str(event.get("reason") or "failed")
                category = error_classes.classify(reason, url)
                if url and url not in failed:
                    failed.append(url)
                    if event.get("referer"):
                        referers[url] = str(event["referer"])
                    note_source(url, reason, REGISTRY_CATEGORY.get(category, "other"))
                    hints.failed(url, reason)
                FILES.update(url, state="failed", reason=reason, speed=0, category=category)
            elif kind == "skipped":
                url = str(event.get("url") or "")
                if url and FILES.state(url) != "done":
                    reason = str(event.get("reason") or "already downloaded")
                    if reason.startswith(("already", "same hash")):
                        skipped_urls.append(url)  # only real duplicates go into history
                    FILES.update(url, state="skipped", reason=reason, speed=0)
            elif kind == "duplicate":
                # same bytes as a file saved earlier; both are kept, the user decides
                url = str(event.get("url") or "")
                other = str(event.get("of") or "")
                if url and other:
                    duplicates.append((str(event.get("path") or ""), other))
                    FILES.update(url, reason=f"same content as {Path(other).name}")
            elif kind == "retry":
                url = str(event.get("url") or "")
                if url and FILES.state(url) not in FINAL_STATES:
                    FILES.update(url, state="retrying", reason="retrying")
            elif kind == "unsupported":
                url = str(event.get("url") or "")
                if url and looks_like_file(url):
                    unsupported.append(url)
                elif url:
                    note_source(url, "No site handler claimed this URL", "missing_handler")
                    scrape_notes.append("No handler accepted this page, so it was not downloaded.")
            elif kind == "last_post":
                emit({"type": "last_post", "url": str(event.get("url") or "")})
            elif kind == "forum_result":
                forum_note = str(event.get("message") or "")
                log("info", hints.forum(forum_note))
            elif kind == "scrape_failed":
                message = str(event.get("message") or "")
                if "Max Children" in message or "depth limit" in message:
                    note_source(urls[0], message, "unsupported_pattern")
                scrape_notes.append(classify_scrape_message(message))
                level, text = hints.scrape_failed(str(event.get("url") or ""), message)
                if text:
                    log(level, text)
            elif kind == "ready":
                log("info", f"NexDrop engine {event.get('version', '')} ready.")
            elif kind == "log":
                level = str(event.get("level") or "info")
                log("warning" if level == "warn" else level, str(event.get("message") or ""))
            elif kind == "error":
                emit({"type": "error", "message": str(event.get("message") or "Engine error")})
            elif kind == "done":
                for item in event.get("saved") or []:
                    url = str(item.get("url") or "")
                    if url and url not in saved_urls:
                        saved_urls.append(url)
                        FILES.update(url, state="done", name=item.get("filename") or None, read=item.get("bytes"), total=item.get("bytes"))
        code = proc.wait()
        PROC = None
        return code

    # Telegram links go through the signed-in account when there is one; the engine's
    # public t.me crawler handles them otherwise.
    account = config.get("telegram") if isinstance(config.get("telegram"), dict) else None
    tg_urls = [u for u in urls if telegram_account.is_telegram(u)] if account else []
    if tg_urls and not telegram_account.available():
        log("warning", "The Telegram library is missing from this NEXDROP folder, so Telegram links use the public preview instead of your account.")
        tg_urls = []
    tg_failed: list[str] = []
    if tg_urls:
        emit({"type": "phase", "stage": "download", "message": "Downloading from Telegram"})
        downloading = True
        log("info", f"Telegram: {len(tg_urls)} link(s) through your signed-in account.")
        hooks = _TelegramHooks(redownload=redownload, skip_urls=set(payload["skipUrls"]),
                               saved=saved_urls, failed=tg_failed, skipped=skipped_urls)
        try:
            telegram_account.TelegramDownloads(
                account, folder, hooks, parallel=payload["perHost"], type_folders=payload["typeFolders"],
                file_types=payload["fileTypes"], min_image_kb=payload["minImageKB"],
            ).run(tg_urls)
        except Exception as error:  # noqa: BLE001 - never lose the other links
            log("error", f"Telegram: {telegram_account.explain(error)}")
            for url in tg_urls:
                if FILES.state(url) is None:
                    hooks.failed(url, telegram_account.explain(error))
    engine_urls = [u for u in urls if u not in tg_urls]
    payload["urls"] = engine_urls
    if engine_urls and not STOP.is_set():
        emit({"type": "phase", "stage": "crawl", "message": "Crawling"})
        code = engine_pass(payload)
        if code not in (0, None) and not STOP.is_set() and not saved_urls and not skipped_urls:
            emit({"type": "error", "message": f"The engine exited with code {code}. See engine\\appdata\\logs for details."})

    def fetch(url: str) -> None:
        if STOP.is_set():
            return
        try:
            http_save(url, folder)
            saved_urls.append(url)
        except Exception as error:  # noqa: BLE001
            failed.append(url)
            reason = str(error)[:200]
            FILES.update(url, state="failed", reason=reason, speed=0, category=error_classes.classify(reason, url))

    if unsupported and not STOP.is_set():
        emit({"type": "phase", "stage": "finalize", "message": f"Fetching {len(unsupported)} direct file(s)"})
        known = load_history()
        todo = []
        for url in dict.fromkeys(unsupported):
            if not redownload and url in known:
                skipped_urls.append(url)
                FILES.update(url, state="skipped", reason="already downloaded")
            else:
                todo.append(url)

        # Several direct files at once, within the per-site limit.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, min(payload["perHost"], 8, len(todo) or 1))) as pool:
            list(pool.map(fetch, todo))

    if config.get("autoRetry", True) and not STOP.is_set():
        retry_pass(failed, referers, payload, engine_pass, fetch, set(unsupported))
    failed.extend(tg_failed)  # the Telegram downloader already retried its own files
    failed[:] = [url for url in dict.fromkeys(failed) if FILES.state(url) != "done"]
    hints.forget_saved(lambda url: FILES.state(url) == "done")

    for text in hints.summary():
        log("warning", text)
    if duplicates:
        log("info", f"{len(duplicates)} saved file(s) have the same content as files you already had. "
                    "Both copies were kept; nothing was deleted.")
        for path, other in duplicates[:20]:
            log("info", f"  {path}  =  {other}")
    with FILES.lock:
        needs_account = sum(1 for row in FILES.rows.values() if "sign in to Telegram" in (row.get("reason") or ""))
    if needs_account:
        log("warning", f"{needs_account} Telegram file(s) are not in the public preview (too large, or documents). "
                       "Sign in to Telegram in Settings > Sign-ins and run the link again to get them.")
    empty_reason = ""
    notes = list(dict.fromkeys(scrape_notes))
    if not saved_urls:
        for note in notes:
            log("warning", note)
        if not notes and forum_note:
            log("warning", forum_note.replace("\t", " "))
        if not skipped_urls and not failed:
            empty_reason = notes[0] if notes else forum_note.replace("\t", " ")
    return _finish(urls, folder, saved_urls, failed, skipped_urls, started, stopped=STOP.is_set(),
                   store=store, job_id=job_id, empty_reason=empty_reason)


def _finish(urls, folder, saved_urls, failed, skipped_urls, started, *, stopped,
            store=None, job_id=None, empty_reason="") -> int:
    for url in FILES.unfinished():
        if stopped:
            FILES.update(url, state="cancelled", reason="stopped before it finished", speed=0)
        else:
            # Listed by the page but never attempted (for example removed by a filter).
            FILES.update(url, state="skipped", reason="not downloaded", speed=0)
    FILES.flush()
    try:
        history = load_history()
        history.update(saved_urls)
        history.update(skipped_urls)
        if history:
            save_history(history)
        if urls:
            record_runs(urls, len(saved_urls), len(failed), len(skipped_urls), stopped)
    except OSError as error:
        log("warning", f"Could not save history: {error}")
    if store and job_id:
        try:
            store.finish_job(job_id, _job_status(len(saved_urls), len(failed), len(skipped_urls), stopped), empty_reason)
            store.close()
        except Exception:  # noqa: BLE001
            pass
    _stop_flare()
    clear_run_cookies()
    _close_engine_db()
    maintain_engine_db()
    FINISHED.set()
    emit({
        "type": "finished",
        "saved": len(saved_urls),
        "failed": len(failed),
        "skipped": len(skipped_urls),
        "failedUrls": list(dict.fromkeys(failed))[:500],
        "root": str(folder),
        "stopped": stopped,
        "bytes": FILES.bytes,
        "seconds": round(time.monotonic() - started, 1),
        "emptyReason": empty_reason,
    })
    global RUN_LOG
    if RUN_LOG is not None:
        try:
            RUN_LOG.close()
        except OSError:
            pass
        RUN_LOG = None
    return 0 if not failed else 1


def main() -> int:
    config = json.loads(sys.stdin.readline() or "{}")
    if not isinstance(config, dict):
        config = {}
    threading.Thread(target=_listen_for_stop, daemon=True).start()
    return run(config)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)
    try:
        raise SystemExit(main())
    except SystemExit:
        _stop_flare()
        raise
    except BaseException as exc:  # noqa: BLE001
        _stop_flare()
        clear_run_cookies()
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        emit({"type": "finished", "saved": 0, "failed": 0, "skipped": 0, "failedUrls": [], "root": "",
              "stopped": False, "bytes": 0, "seconds": 0, "emptyReason": ""})
        raise SystemExit(1) from exc
