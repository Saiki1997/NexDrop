"""NEXDROP patch: what a half-downloaded file was, so a resume only appends bytes of the same file.

Beside every `<name>.part` the engine writes `<name>.part.nxmeta` (JSON): the link, the server's
ETag and Last-Modified, and the full size, taken from the answer that started the file. On resume:

- `If-Range` carries the ETag (or Last-Modified), so a server whose file changed sends the whole
  new file (200) instead of a range of it;
- a 206 answer must start exactly at the resume point, report the same full size, and carry the
  same ETag, or the partial file is deleted and the download starts again.

A partial file from an older NexDrop has no sidecar: only the range start is checked.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

SUFFIX = ".nxmeta"
_CONTENT_RANGE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", re.I)


def sidecar(partial_file: Path) -> Path:
    return partial_file.with_name(partial_file.name + SUFFIX)


def load(partial_file: Path) -> dict:
    try:
        data = json.loads(sidecar(partial_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(partial_file: Path, headers: Mapping[str, str], size: int | None, url: str = "") -> None:
    """Remember the validators of the answer that starts (or restarts) this partial file."""
    data = {
        "url": url.split("?", 1)[0],  # no tokens in a file beside the download
        "etag": str(headers.get("ETag") or ""),
        "last_modified": str(headers.get("Last-Modified") or ""),
        "size": int(size or 0),
    }
    if not (data["etag"] or data["last_modified"] or data["size"]):
        forget(partial_file)
        return
    try:
        partial_file.parent.mkdir(parents=True, exist_ok=True)  # the first answer comes before the folder exists
        sidecar(partial_file).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # without it the range start is still checked


def forget(partial_file: Path) -> None:
    try:
        sidecar(partial_file).unlink(missing_ok=True)
    except OSError:
        pass


def if_range(partial_file: Path) -> str:
    """The If-Range value: a strong ETag, else Last-Modified, else nothing."""
    data = load(partial_file)
    tag = str(data.get("etag") or "")
    if tag and not tag.startswith("W/"):
        return tag
    return str(data.get("last_modified") or "")


def parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    match = _CONTENT_RANGE.match(value or "")
    if not match:
        return None
    total = None if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), int(match.group(2)), total


def mismatch(partial_file: Path, headers: Mapping[str, str], resume_point: int) -> str:
    """Why a 206 answer does not continue this partial file, or '' when it does."""
    content_range = headers.get("Content-Range")
    parsed = parse_content_range(content_range)
    if parsed is None:
        return f"Server answered a range request without a usable Content-Range ({content_range or 'none'})"
    start, end, total = parsed
    if start != resume_point:
        return f"Server sent bytes from {start:,} but the partial file has {resume_point:,}"
    if end < start:
        return "Server sent an empty byte range"
    data = load(partial_file)
    known_size = int(data.get("size") or 0)
    if known_size and total is not None and total != known_size:
        return f"The file on the server is now {total:,} bytes, not {known_size:,}"
    known_tag = str(data.get("etag") or "")
    tag = str(headers.get("ETag") or "")
    if known_tag and tag and known_tag.removeprefix("W/") != tag.removeprefix("W/"):
        return "The file on the server changed since the partial file was started (ETag)"
    known_modified = str(data.get("last_modified") or "")
    modified = str(headers.get("Last-Modified") or "")
    if not known_tag and known_modified and modified and known_modified != modified:
        return "The file on the server changed since the partial file was started (Last-Modified)"
    return ""
