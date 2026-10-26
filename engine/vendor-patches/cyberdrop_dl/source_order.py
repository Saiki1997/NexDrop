"""Remember the order a page listed its files. This does not download anything."""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

ORDER_MODE: ContextVar[str] = ContextVar("ORDER_MODE", default="source")
SEQUENCE_PREFIX: ContextVar[bool] = ContextVar("SEQUENCE_PREFIX", default=True)
ALBUM_NAMES: ContextVar[bool] = ContextVar("ALBUM_NAMES", default=False)
POST_FOLDERS: ContextVar[bool] = ContextVar("POST_FOLDERS", default=True)
# NEXDROP patch (smart naming): where the source-order number goes. "suffix" is "name-001.ext",
# "prefix" the older "001_name.ext". SEQUENCE_PREFIX=False still turns numbering off.
NUMBER_MODE: ContextVar[str] = ContextVar("NUMBER_MODE", default="prefix")
_SENSITIVE = ("password", "token", "auth", "cookie", "session", "sig", "key", "secret")
_sequences: dict[str, int] = {}
_next = 1
_titles: dict[str, str] = {}  # folder title -> album name as the site shows it
_albums: dict[str, dict[str, int]] = {}  # album folder -> {file URL: number}
_ALBUM_NAME_MAX = 120
_names: dict[str, dict] = {}  # URL -> naming row this job already handed out (name, folder, ...)
_earlier: dict[str, dict] = {}  # URL -> newest naming row from another job (a retry of one file, ...)
_job = ""


def reset(manifest: str | None = None, job: str | None = None) -> None:
    """Forget this process's numbers, then load the ones `job` already handed out.

    With a job key, numbering starts at 1 for each download job and only rows written
    for the same job (same links, for example a resumed or rechecked run) are reused.
    Without one, every row counts (the behaviour before 2.5).
    """
    global _next, _job
    _sequences.clear()
    _albums.clear()
    _names.clear()
    _earlier.clear()
    _next = 1
    _job = str(job or "")
    if not manifest:
        return
    path = Path(manifest)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if _job and str(item.get("job") or "") != _job:
            if url and item.get("job") and item.get("name"):
                _earlier[url] = item
            continue
        try:
            number = int(item.get("sequence") or 0)
        except (TypeError, ValueError):
            continue
        if url and number > 0:
            _sequences[url] = number
            _next = max(_next, number + 1)
            if _job and item.get("name"):
                _names[url] = item


def stored(url: str) -> dict | None:
    """The naming row this job already gave `url`, so a resumed job keeps its names."""
    key = public_url(url)
    return _names.get(key) or _earlier.get(key)


def job_key() -> str:
    return _job


def public_url(url: str) -> str:
    parts = urlsplit(str(url))
    query = "&".join(
        f"{name}={value}"
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not any(token in name.lower() for token in _SENSITIVE)
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def assign(url: str) -> int:
    global _next
    key = public_url(url)
    if key in _sequences:
        return _sequences[key]
    _sequences[key] = _next
    _next += 1
    return _sequences[key]


def prefix_name(number: int, name: str) -> str:
    if re.match(r"^\d{3}_", name) or re.match(r"^\d{3} - ", name):
        return name
    return f"{number:03d}_{name}"


_COMPOUND_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz", ".tar.z")


def split_name(name: str, ext: str | None = None) -> tuple[str, str]:
    """("backup", ".tar.gz") for "backup.tar.gz". `ext` is the extension the crawler chose."""
    lowered = name.lower()
    for compound in _COMPOUND_EXTS:
        if lowered.endswith(compound) and len(name) > len(compound):
            return name[: -len(compound)], name[-len(compound) :]
    if ext and lowered.endswith(ext.lower()) and len(name) > len(ext):
        return name[: -len(ext)], name[-len(ext) :]
    stem, dot, tail = name.rpartition(".")
    if dot and stem and tail and len(tail) <= 12 and " " not in tail:
        return stem, "." + tail
    return name, ""


def suffix_name(number: int, name: str, ext: str | None = None) -> str:
    """"name-001.ext". The number is right before the extension and grows past 999."""
    stem, extension = split_name(name, ext)
    if stem.endswith(f"-{number:03d}"):
        return name
    return f"{stem}-{number:03d}{extension}"


def format_number(number: int, name: str, mode: str | None = None, ext: str | None = None) -> str:
    mode = mode or NUMBER_MODE.get()
    if not number or mode in {"off", "disabled", "none"}:
        return name
    if mode == "suffix":
        return suffix_name(number, name, ext)
    return prefix_name(number, name)


def numbered_filename(
    url: str, original: str, *, enabled: bool | None = None, mode: str | None = None
) -> tuple[int, str]:
    """Sequence from source order, then the saved filename. Same URL keeps its number."""
    number = assign(url)
    if enabled is None:
        enabled = SEQUENCE_PREFIX.get()
    name = format_number(number, original, mode) if enabled else original
    return number, name


def remember_title(title: str, name: str) -> None:
    """Map a folder title ("Name 123 (Bunkr)") back to the album's own name."""
    _titles[title] = name


def album_name(title: str) -> str:
    return (_titles.get(title) or title).strip()


def album_filename(album: str, folder: str, url: str, ext: str, preferred: int | None = None) -> str:
    """'<album> 001.ext'. Numbers count per album folder; one URL keeps its number."""
    numbers = _albums.setdefault(folder, {})
    key = public_url(url)
    number = numbers.get(key)
    if number is None:
        used = set(numbers.values())
        if preferred and preferred > 0 and preferred not in used:
            number = preferred
        else:
            number = max(used | {0}) + 1
        numbers[key] = number
    name = " ".join(album.split())[:_ALBUM_NAME_MAX].strip() or "Album"
    return f"{name} {number:03d}{ext}"


def order_urls(html: str, urls: list[str]) -> list[str]:
    """Document order. A URL that is not in the HTML stays after the ones that are."""
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for index, url in enumerate(urls):
        if not url or url in seen:
            continue
        seen.add(url)
        position = html.find(url)
        if position < 0:
            bare = url.split("?", 1)[0]
            position = html.find(bare) if bare else -1
        ranked.append((position if position >= 0 else 10**9, index, url))
    ranked.sort()
    return [url for _, _, url in ranked]


def remember(path: Path, url: str, sequence: int, name: str, method: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"url": public_url(url), "sequence": sequence, "name": name, "method": method}
    if _job:
        row["job"] = _job
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
