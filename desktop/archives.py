"""Archive inspection, integrity check and safe extraction (3.1, Library > archive files).

    archives.py inspect   stdin: {"path": archive}
    archives.py test      stdin: {"path": archive}
    archives.py extract   stdin: {"path": archive, "dest": empty or new folder}

ZIP and TAR (.tar, .tar.gz/.tgz, .tar.bz2, .tar.xz) are read with Python's own modules. Nothing
inside an archive is ever run. Extraction only happens when the person asks for it, into a folder
they chose, and refuses:

- names that leave the folder (../, absolute paths, drive letters, links pointing outside);
- links and device files (only plain files and folders are written);
- archives that would expand past MAX_TOTAL bytes or MAX_FILES files, or whose expansion ratio
  looks like a decompression bomb;
- existing files: nothing is overwritten, a clash stops the extraction before anything is written.

Encrypted ZIP entries are listed but not extracted. Other formats (7z, RAR) are reported as not
supported instead of guessed at.
"""

from __future__ import annotations

import json
import shutil
import stat
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_FILES = 100_000
MAX_TOTAL = 64 * 1024**3  # 64 GB expanded
MAX_RATIO = 200  # expanded / compressed, over the whole archive
CHUNK = 1024 * 1024
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


class Refused(Exception):
    pass


def kind_of(path: Path) -> str:
    name = path.name.lower()
    if zipfile.is_zipfile(path):
        return "zip"
    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        try:
            if tarfile.is_tarfile(path):
                return "tar"
        except OSError:
            return ""
    if name.endswith((".7z", ".rar")):
        return "unsupported"
    return ""


def safe_name(name: str) -> str | None:
    """The entry's relative path with forward slashes, or None when it would leave the folder."""
    text = name.replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None
    parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    for part in parts:
        if part.split(".")[0].lower() in WINDOWS_RESERVED or any(ch in part for ch in '<>:"|?*') or part.endswith((" ", ".")):
            return None
    return "/".join(parts)


def _entries(path: Path, kind: str) -> list[dict]:
    out: list[dict] = []
    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                mode = (info.external_attr >> 16) & 0xFFFF
                link = stat.S_ISLNK(mode)
                out.append({"name": info.filename, "size": info.file_size, "packed": info.compress_size,
                            "dir": info.is_dir(), "link": link, "encrypted": bool(info.flag_bits & 0x1),
                            "safe": safe_name(info.filename) is not None})
    else:
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                out.append({"name": member.name, "size": member.size if member.isfile() else 0, "packed": 0,
                            "dir": member.isdir(), "link": member.issym() or member.islnk(),
                            "special": not (member.isfile() or member.isdir() or member.issym() or member.islnk()),
                            "encrypted": False, "safe": safe_name(member.name) is not None})
    return out


def inspect(path: Path) -> dict:
    kind = kind_of(path)
    if not kind:
        return {"ok": False, "error": "This is not a ZIP or TAR archive."}
    if kind == "unsupported":
        return {"ok": False, "error": "7z and RAR archives can't be opened by NexDrop. Use 7-Zip to open them."}
    try:
        entries = _entries(path, kind)
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as error:
        return {"ok": False, "error": f"The archive is damaged: {error}"}
    total = sum(e["size"] for e in entries)
    packed = path.stat().st_size
    problems = []
    unsafe = [e["name"] for e in entries if not e["safe"]]
    if unsafe:
        problems.append(f"{len(unsafe)} name(s) point outside the folder, for example {unsafe[0]!r}")
    links = [e["name"] for e in entries if e.get("link")]
    if links:
        problems.append(f"{len(links)} link(s) will not be extracted")
    if any(e.get("special") for e in entries):
        problems.append("device or special files will not be extracted")
    if any(e["encrypted"] for e in entries):
        problems.append("some files are password protected and will not be extracted")
    if len(entries) > MAX_FILES:
        problems.append(f"more than {MAX_FILES:,} files")
    if total > MAX_TOTAL or (packed and total / packed > MAX_RATIO and total > 100 * 1024**2):
        problems.append("it expands far more than its size (possible decompression bomb)")
    runnable = [e["name"] for e in entries if e["name"].lower().endswith((".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".scr", ".msi", ".lnk"))]
    return {"ok": True, "kind": kind, "files": sum(1 for e in entries if not e["dir"]), "bytes": total, "packed": packed,
            "entries": entries[:500], "more": max(0, len(entries) - 500), "problems": problems, "runnable": runnable[:20]}


def test(path: Path) -> dict:
    """Read every file in the archive and check its checksum (ZIP CRC; TAR reads every member)."""
    kind = kind_of(path)
    if kind not in ("zip", "tar"):
        return inspect(path)
    checked = 0
    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir() or info.flag_bits & 0x1:
                        continue
                    with archive.open(info) as handle:
                        while handle.read(CHUNK):
                            pass
                    checked += 1
        else:
            with tarfile.open(path) as archive:
                for member in archive:
                    if member.isfile():
                        handle = archive.extractfile(member)
                        if handle is not None:
                            while handle.read(CHUNK):
                                pass
                        checked += 1
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, RuntimeError) as error:
        return {"ok": True, "good": False, "checked": checked, "error": f"Damaged after {checked} file(s): {error}"}
    return {"ok": True, "good": True, "checked": checked}


def extract(path: Path, dest: Path) -> dict:
    report = inspect(path)
    if not report.get("ok"):
        return report
    if len(report["entries"]) + report["more"] > MAX_FILES:
        return {"ok": False, "error": f"The archive has more than {MAX_FILES:,} files."}
    if any("bomb" in p for p in report["problems"]):
        return {"ok": False, "error": "The archive expands far more than its size. It was not extracted."}
    if dest.exists() and any(dest.iterdir()):
        return {"ok": False, "error": "Choose an empty or new folder. Nothing is overwritten."}
    kind = report["kind"]
    try:
        free = shutil.disk_usage(dest.parent if not dest.exists() else dest).free
    except OSError:
        free = None
    if free is not None and report["bytes"] > free:
        return {"ok": False, "error": "There is not enough free space for the extracted files."}
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    written = skipped = 0
    budget = MAX_TOTAL

    def target_for(name: str) -> Path | None:
        clean = safe_name(name)
        if clean is None:
            return None
        target = (root / clean).resolve()
        if root not in target.parents and target != root:
            return None
        return target

    def copy(src, target: Path, declared: int) -> None:
        nonlocal budget
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise Refused(f"{target.name} exists twice in the archive")
        size = 0
        with open(target, "xb") as out:
            while chunk := src.read(CHUNK):
                size += len(chunk)
                budget -= len(chunk)
                if budget < 0 or size > max(declared, 0) + CHUNK:
                    raise Refused("The archive contains more data than it declares. Extraction stopped.")
                out.write(chunk)

    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    mode = (info.external_attr >> 16) & 0xFFFF
                    target = target_for(info.filename)
                    if target is None or stat.S_ISLNK(mode) or info.flag_bits & 0x1:
                        skipped += 1
                        continue
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    with archive.open(info) as src:
                        copy(src, target, info.file_size)
                    written += 1
        else:
            with tarfile.open(path) as archive:
                for member in archive:
                    target = target_for(member.name)
                    if target is None or not (member.isfile() or member.isdir()):
                        skipped += 1
                        continue
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    src = archive.extractfile(member)
                    if src is None:
                        skipped += 1
                        continue
                    copy(src, target, member.size)
                    written += 1
    except Refused as error:
        return {"ok": False, "error": str(error), "written": written, "dest": str(dest)}
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, RuntimeError) as error:
        return {"ok": False, "error": f"Extraction stopped: {error}", "written": written, "dest": str(dest)}
    return {"ok": True, "written": written, "skipped": skipped, "dest": str(dest)}


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        body = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        body = {}
    path = Path(str(body.get("path") or ""))
    if not path.is_file():
        result = {"ok": False, "error": "The archive is not there any more."}
    elif command == "inspect":
        result = inspect(path)
    elif command == "test":
        result = test(path)
    elif command == "extract" and body.get("dest"):
        result = extract(path, Path(str(body["dest"])))
    else:
        result = {"ok": False, "error": "usage: archives.py inspect|test|extract"}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
