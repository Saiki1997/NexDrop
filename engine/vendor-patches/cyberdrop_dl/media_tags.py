"""NEXDROP smart naming V2: titles written inside a downloaded file by whoever made it.

Optional ("Read titles inside files", Settings > Smart naming). Only small, known parts of a
file are read: the tag block of a video, audio file or image, or the file list of a zip. Nothing
is decoded, extracted, executed or sent anywhere. Each reader gives up quietly on anything it
does not expect.
"""

from __future__ import annotations

import re
import struct
import zipfile
from pathlib import Path, PurePosixPath

_MAX_BOX = 16 * 1024 * 1024  # an MP4 'moov' larger than this is not read
_HEAD = 2 * 1024 * 1024
_TOOLS = re.compile(r"^(lavf|lavc|libav|handbrake|isom|mp42|ffmpeg|created (with|by)|encoded (with|by)|made with|"
                    r"adobe|photoshop|gimp|canon|nikon|sony|samsung|apple|iphone|android|camera|olympus|"
                    r"fujifilm|panasonic|gopro|dji|xiaomi|huawei|oneplus|google|pixel|screenshot)(?=\W|\d|$)", re.I)


def read_title(path: str | Path) -> tuple[str, str]:
    """(title, where it was found) or ("", "")."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return "", ""
    readers = []
    suffix = path.suffix.lower()
    if head[4:8] == b"ftyp" or suffix in {".mp4", ".m4v", ".mov", ".m4a", ".3gp"}:
        readers.append((_mp4_title, "media tag"))
    if head[:4] == b"\x1a\x45\xdf\xa3":
        readers.append((_mkv_title, "video tag"))
    if head[:3] == b"ID3":
        readers.append((_id3_title, "audio tag"))
    if head[:4] == b"fLaC":
        readers.append((_flac_title, "audio tag"))
    if head[:2] == b"\xff\xd8":
        readers.append((_jpeg_title, "image tag"))
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        readers.append((_png_title, "image tag"))
    if head[:4] == b"PK\x03\x04":
        readers.append((_zip_title, "archive folder"))
    for reader, where in readers:
        try:
            title = reader(path)
        except (OSError, ValueError, struct.error, zipfile.BadZipFile, UnicodeError, IndexError, KeyError):
            continue
        title = " ".join((title or "").replace("\x00", " ").split())[:200]
        if title and not _TOOLS.match(title):
            return title, where
    return "", ""


# ─── MP4 / MOV / M4A: moov/udta/meta/ilst/©nam ───────────────────────────────


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def _mp4_title(path: Path) -> str:
    with path.open("rb") as handle:
        size_total = path.stat().st_size
        pos = 0
        while pos + 8 <= size_total:
            handle.seek(pos)
            header = handle.read(16)
            size, kind = struct.unpack(">I4s", header[:8])
            skip = 8
            if size == 1:
                size = struct.unpack(">Q", header[8:16])[0]
                skip = 16
            elif size == 0:
                size = size_total - pos
            if size < skip:
                return ""
            if kind == b"moov":
                if size > _MAX_BOX:
                    return ""
                handle.seek(pos + skip)
                moov = handle.read(size - skip)
                return _ilst_title(moov)
            pos += size
    return ""


def _ilst_title(moov: bytes) -> str:
    for kind, start, end in _boxes(moov):
        if kind != b"udta":
            continue
        for kind2, start2, end2 in _boxes(moov, start, end):
            if kind2 == b"\xa9nam":  # QuickTime text atom: 2 bytes length, 2 bytes language
                length = struct.unpack(">H", moov[start2 : start2 + 2])[0]
                return moov[start2 + 4 : start2 + 4 + length].decode("utf-8", "replace")
            if kind2 != b"meta":
                continue
            for kind3, start3, end3 in _boxes(moov, start2 + 4, end2):  # 'meta' is a full box
                if kind3 != b"ilst":
                    continue
                for kind4, start4, end4 in _boxes(moov, start3, end3):
                    if kind4 != b"\xa9nam":
                        continue
                    for kind5, start5, end5 in _boxes(moov, start4, end4):
                        if kind5 == b"data":
                            return moov[start5 + 8 : end5].decode("utf-8", "replace")
    return ""


# ─── Matroska / WebM: Segment > Info > Title ─────────────────────────────────


def _vint(data: bytes, pos: int, *, keep_marker: bool) -> tuple[int, int]:
    first = data[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError("bad EBML length")
    value = first if keep_marker else first & (mask - 1)
    for byte in data[pos + 1 : pos + length]:
        value = (value << 8) | byte
    return value, length


def _mkv_title(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(_HEAD)
    pos = 0
    stack_end = len(data)
    containers = {0x18538067, 0x1549A966}  # Segment, Info
    while pos < stack_end:
        ident, n = _vint(data, pos, keep_marker=True)
        size, m = _vint(data, pos + n, keep_marker=False)
        body = pos + n + m
        unknown = size == (1 << (7 * m)) - 1
        if ident in containers:
            pos = body  # step inside
            continue
        if ident == 0x7BA9:  # Title
            return data[body : body + size].decode("utf-8", "replace")
        if unknown:
            return ""
        if ident == 0x1F43B675:  # Cluster: the video itself starts, no title came first
            return ""
        pos = body + size
    return ""


# ─── MP3 (ID3v2 TIT2) and FLAC (Vorbis TITLE) ────────────────────────────────


def _id3_text(frame: bytes) -> str:
    encoding, text = frame[0], frame[1:]
    if encoding == 0:
        return text.decode("latin-1")
    if encoding == 1:
        return text.decode("utf-16")
    if encoding == 2:
        return text.decode("utf-16-be")
    return text.decode("utf-8", "replace")


def _id3_title(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(10)
        version = header[3]
        size = _synchsafe(header[6:10])
        data = handle.read(min(size, _HEAD))
    pos = 0
    while pos + 10 <= len(data):
        frame_id = data[pos : pos + 4]
        if not frame_id.strip(b"\x00"):
            break
        raw = data[pos + 4 : pos + 8]
        frame_size = _synchsafe(raw) if version >= 4 else struct.unpack(">I", raw)[0]
        if frame_size <= 0:
            break
        if frame_id == b"TIT2":
            return _id3_text(data[pos + 10 : pos + 10 + frame_size])
        pos += 10 + frame_size
    return ""


def _synchsafe(raw: bytes) -> int:
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def _flac_title(path: Path) -> str:
    with path.open("rb") as handle:
        handle.read(4)
        while True:
            header = handle.read(4)
            if len(header) < 4:
                return ""
            last, kind = header[0] & 0x80, header[0] & 0x7F
            size = int.from_bytes(header[1:4], "big")
            block = handle.read(size)
            if kind == 4:
                return _vorbis_title(block)
            if last:
                return ""


def _vorbis_title(block: bytes) -> str:
    vendor = struct.unpack("<I", block[:4])[0]
    pos = 4 + vendor
    count = struct.unpack("<I", block[pos : pos + 4])[0]
    pos += 4
    for _ in range(min(count, 500)):
        length = struct.unpack("<I", block[pos : pos + 4])[0]
        entry = block[pos + 4 : pos + 4 + length].decode("utf-8", "replace")
        pos += 4 + length
        key, _, value = entry.partition("=")
        if key.upper() == "TITLE":
            return value
    return ""


# ─── JPEG (EXIF XPTitle / ImageDescription, XMP dc:title) and PNG (tEXt Title) ─

_XMP_TITLE = re.compile(rb"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.S)


def _jpeg_title(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(256 * 1024)
    pos = 2
    found = ""
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker in (0xDA, 0xD9):  # image data starts
            break
        size = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        segment = data[pos + 4 : pos + 2 + size]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            found = found or _exif_title(segment[6:])
        elif marker == 0xE1 and b"ns.adobe.com/xap" in segment[:64]:
            if match := _XMP_TITLE.search(segment):
                return match.group(1).decode("utf-8", "replace")
        pos += 2 + size
    return found


def _exif_title(tiff: bytes) -> str:
    order = "<" if tiff[:2] == b"II" else ">"
    offset = struct.unpack(order + "I", tiff[4:8])[0]
    count = struct.unpack(order + "H", tiff[offset : offset + 2])[0]
    values: dict[int, str] = {}
    for index in range(min(count, 200)):
        entry = tiff[offset + 2 + index * 12 : offset + 14 + index * 12]
        tag, kind, number = struct.unpack(order + "HHI", entry[:8])
        if tag not in (0x9C9B, 0x010E):
            continue
        size = number * (1 if kind in (1, 2, 7) else 2 if kind == 3 else 4)
        raw = entry[8 : 8 + size] if size <= 4 else tiff[struct.unpack(order + "I", entry[8:12])[0] :][:size]
        values[tag] = raw.decode("utf-16-le", "replace") if tag == 0x9C9B else raw.decode("utf-8", "replace")
    return values.get(0x9C9B) or values.get(0x010E) or ""


def _png_title(path: Path) -> str:
    with path.open("rb") as handle:
        handle.read(8)
        while True:
            header = handle.read(8)
            if len(header) < 8:
                return ""
            size, kind = struct.unpack(">I4s", header)
            if kind == b"IDAT" or size > 1024 * 1024:
                return ""
            body = handle.read(size)
            handle.read(4)  # CRC
            if kind == b"tEXt":
                key, _, value = body.partition(b"\x00")
                if key in (b"Title", b"Description"):
                    return value.decode("latin-1")
            elif kind == b"iTXt":
                key, _, rest = body.partition(b"\x00")
                if key == b"Title" and rest[:1] == b"\x00":  # not compressed
                    _lang, _, rest = rest[2:].partition(b"\x00")
                    _translated, _, value = rest.partition(b"\x00")
                    return value.decode("utf-8", "replace")


# ─── ZIP: one top folder names the archive ───────────────────────────────────


def _zip_title(path: Path) -> str:
    """The single folder every entry sits in ("Summer Set/01.jpg", "Summer Set/02.jpg").
    Only the central directory is read; nothing is extracted."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()[:5000]
    tops = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    if len(tops) == 1 and any("/" in name for name in names):
        top = next(iter(tops))
        if top not in {"__MACOSX", "."}:
            return top
    return ""
