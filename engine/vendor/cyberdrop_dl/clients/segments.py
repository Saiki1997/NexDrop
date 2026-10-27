"""NEXDROP patch: download one large file over a few connections at once (multi-segment).

Used only when it is safe:

- the file is large (MIN_SIZE or more) and its size is known;
- the server says `Accept-Ranges: bytes` and gives a strong ETag or a Last-Modified, so every
  extra range can carry `If-Range` and a changed file is never mixed with the old one;
- the answer is not compressed, not a stream segment (HLS), and not sent through browser
  impersonation;
- the server has not asked NexDrop to slow down (429/503) during this run.

The first answer (already open) fills the first part, straight into the usual `.part` file.
Every other part is a ranged request written to `<name>.part.segN`; each answer must be a 206
for exactly the requested bytes of a file with the same size and validators. When everything
is there the parts are appended to the `.part` file in order and the normal checks (exact size,
web page instead of file) run on the result.

The layout is saved in the `.part.nxmeta` sidecar, so after a crash or a pause each part
resumes from its own size. Anything unexpected (a 200 instead of 206, another range, a changed
file) deletes the parts, remembers that this link must not be split, and the retry downloads it
as one stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from cyberdrop_dl.clients import resume_validators

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

MIN_SIZE = 64 * 1024 * 1024  # files smaller than this are faster as one stream
MIN_PART = 16 * 1024 * 1024
MAX_PARTS = 4  # set by the bridge from the download mode; 0 or 1 turns splitting off
PART_CHUNK = 1024 * 1024
SEG_FILES_MAX = 16  # part files ever written are .seg1 ... .seg(MAX_PARTS - 1); the setting goes up to 8
COPY_CHUNK = 1024 * 1024

# The bridge replaces this with a check of the hosts that asked to slow down
host_ok: Callable[[str], bool] = lambda host: True  # noqa: E731
_no_split: set[str] = set()  # links that answered a part wrongly in this run


class SegmentError(Exception):
    """A part answered with something other than exactly the requested bytes."""


def parts_for(size: int, max_parts: int | None = None) -> int:
    limit = MAX_PARTS if max_parts is None else max_parts
    if limit <= 1 or size < MIN_SIZE:
        return 1
    return max(1, min(limit, size // MIN_PART))


def plan(size: int, parts: int) -> list[tuple[int, int]]:
    """Inclusive byte ranges that cover 0..size-1 in `parts` nearly equal pieces."""
    step = size // parts
    ranges = []
    start = 0
    for index in range(parts):
        end = size - 1 if index == parts - 1 else start + step - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def _key(url: Any) -> str:
    return str(url).split("?", 1)[0]


def forget_link(url: Any) -> None:
    _no_split.add(_key(url))


def eligible(url: Any, host: str, headers: Mapping[str, str], size: int, *, impersonated: bool) -> bool:
    if impersonated or parts_for(size) <= 1 or _key(url) in _no_split:
        return False
    if headers.get("Accept-Ranges", "").lower().strip() != "bytes":
        return False
    if headers.get("Content-Encoding", "identity").lower() not in ("identity", ""):
        return False
    tag = str(headers.get("ETag") or "")
    if not (tag and not tag.startswith("W/")) and not headers.get("Last-Modified"):
        return False
    try:
        return bool(host_ok(host))
    except Exception:  # noqa: BLE001
        return True


# ---- layout in the sidecar -------------------------------------------------------------------


def seg_file(partial_file: Path, index: int) -> Path:
    return partial_file.with_name(f"{partial_file.name}.seg{index}")


def save_layout(partial_file: Path, layout: list[tuple[int, int]]) -> None:
    path = resume_validators.sidecar(partial_file)
    data = resume_validators.load(partial_file)
    data["segments"] = [list(pair) for pair in layout]
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def load_layout(partial_file: Path) -> list[tuple[int, int]]:
    raw = resume_validators.load(partial_file).get("segments")
    if not isinstance(raw, list) or len(raw) < 2:
        return []
    try:
        layout = [(int(a), int(b)) for a, b in raw]
    except (TypeError, ValueError):
        return []
    if layout[0][0] != 0 or any(layout[i][1] + 1 != layout[i + 1][0] for i in range(len(layout) - 1)):
        return []
    return layout


def discard(partial_file: Path | None) -> None:
    """Delete the part files of `partial_file` (never the finished file)."""
    if partial_file is None:
        return
    # By name, not by listing the folder: a glob per file made each download slower as the folder filled.
    for index in range(1, SEG_FILES_MAX + 1):
        try:
            seg_file(partial_file, index).unlink(missing_ok=True)
        except OSError:
            pass
    data = resume_validators.load(partial_file)
    if data.pop("segments", None) is not None:
        try:
            resume_validators.sidecar(partial_file).write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass


def resume_range(partial_file: Path, have: int) -> tuple[int, str] | None:
    """For a split file being resumed: (bytes of part 0 to keep, the Range header for part 0).

    Part 0 always asks for at least one byte, so the first request still checks the file;
    when part 0 is already complete its last byte is asked again.
    """
    layout = load_layout(partial_file)
    if not layout:
        return None
    end0 = layout[0][1]
    keep = min(have, end0)  # a complete part 0 gives back its last byte
    return keep, f"bytes={keep}-{end0}"


def existing_bytes(partial_file: Path, layout: list[tuple[int, int]]) -> int:
    total = 0
    for index in range(1, len(layout)):
        try:
            total += min(seg_file(partial_file, index).stat().st_size, layout[index][1] - layout[index][0] + 1)
        except OSError:
            pass
    return total


def check_part(headers: Mapping[str, str], status: int, start: int, end: int, size: int, validators: dict) -> None:
    if status != 206:
        raise SegmentError(f"Part {start}-{end} answered HTTP {status} instead of 206")
    parsed = resume_validators.parse_content_range(headers.get("Content-Range"))
    if parsed is None or parsed[0] != start or parsed[1] != end or (parsed[2] is not None and parsed[2] != size):
        raise SegmentError(f"Part {start}-{end} got {headers.get("Content-Range") or 'no Content-Range'}")
    tag, known = str(headers.get("ETag") or ""), str(validators.get("etag") or "")
    if known and tag and known.removeprefix("W/") != tag.removeprefix("W/"):
        raise SegmentError("The file changed on the server while its parts were downloading")
    modified, known_modified = str(headers.get("Last-Modified") or ""), str(validators.get("last_modified") or "")
    if not known and known_modified and modified and known_modified != modified:
        raise SegmentError("The file changed on the server while its parts were downloading")


def merge(partial_file: Path, layout: list[tuple[int, int]]) -> None:
    """Append every part to the `.part` file in order, after checking each one's size."""
    first = layout[0][1] + 1
    if partial_file.stat().st_size != first:
        raise SegmentError(f"Part 1 has {partial_file.stat().st_size:,} bytes, expected {first:,}")
    for index in range(1, len(layout)):
        want = layout[index][1] - layout[index][0] + 1
        got = seg_file(partial_file, index).stat().st_size
        if got != want:
            raise SegmentError(f"Part {index + 1} has {got:,} bytes, expected {want:,}")
    with partial_file.open("ab") as out:
        for index in range(1, len(layout)):
            with seg_file(partial_file, index).open("rb") as src:
                while chunk := src.read(COPY_CHUNK):
                    out.write(chunk)
    discard(partial_file)


async def write_body(resp: Any, path: Path, mode: str, limit: int, chunk_size: int, on_chunk: Callable[[int], Any]) -> int:
    """Write at most `limit` bytes of `resp` to `path`; returns the number written."""
    from cyberdrop_dl import aio

    written = 0
    async with aio.open(path, mode=mode) as handle:
        async for chunk in resp.iter_chunked(chunk_size):
            if written + len(chunk) > limit:
                chunk = chunk[: limit - written]
            if chunk:
                await handle.write(chunk)
                written += len(chunk)
                await on_chunk(len(chunk))
            if written >= limit:
                break
    return written


async def run_parts(
    first_resp: Any,
    partial_file: Path,
    layout: list[tuple[int, int]],
    have0: int,
    open_part: Callable[[int, int], Any],
    chunk_size: int,
    on_chunk: Callable[[int], Any],
) -> None:
    """Download every part concurrently. `open_part(start, end)` returns an async context manager
    with the answer for that range; the answer is checked by the caller-supplied opener."""
    # The engine reads 10 MB at a time; with up to 4 parts per file that is 40 MB of buffers per
    # file in flight, so parts read in smaller pieces.
    chunk_size = min(chunk_size, PART_CHUNK)

    async def part0() -> None:
        want = layout[0][1] + 1 - have0
        got = await write_body(first_resp, partial_file, "ab", want, chunk_size, on_chunk)
        if got != want:
            raise SegmentError(f"Part 1 ended after {have0 + got:,} of {layout[0][1] + 1:,} bytes")

    async def part(index: int) -> None:
        start, end = layout[index]
        path = seg_file(partial_file, index)
        try:
            have = min(path.stat().st_size, end - start + 1)
        except OSError:
            have = 0
        if have >= end - start + 1:
            return
        async with open_part(start + have, end) as resp:
            want = end - start + 1 - have
            got = await write_body(resp, path, "ab", want, chunk_size, on_chunk)
            if got != want:
                raise SegmentError(f"Part {index + 1} ended after {have + got:,} of {end - start + 1:,} bytes")

    tasks = [asyncio.ensure_future(part0())] + [asyncio.ensure_future(part(i)) for i in range(1, len(layout))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
