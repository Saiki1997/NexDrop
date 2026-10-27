"""Organize and rename downloaded files, with a preview and undo (3.1, Library > Storage).

    organizer.py plan    <library.sqlite>   stdin: {"filters": {...}, "root": folder, "folders": pattern,
                                                    "names": pattern, "roots": [allowed folders]}
    organizer.py apply   <library.sqlite>   stdin: {"plan": plan from `plan`, "jobs": jobs.sqlite, "journal": file}
    organizer.py undo    <library.sqlite>   stdin: {"batch": id, "jobs": jobs.sqlite, "journal": file}
    organizer.py batches <library.sqlite>   stdin: {"journal": file}

Patterns use {creator} {album} {site} {type} {year} {month} {date} {title} {name} {number} {ext}.
A folder pattern such as "{creator}/{album}" puts each file in <root>/<creator>/<album>/; a name
pattern such as "{creator} - {album} {number}" renames it ({ext} is always kept). An empty value
drops out with the separator in front of it; a folder level that ends up empty is left out.

Nothing is overwritten and nothing is deleted:

- the plan marks files whose target already exists, or that two files would share, as conflicts,
  and apply skips them;
- apply checks again that the source is unchanged and the target still free right before each move;
- every move is written to the journal first, so a batch can be undone even after a crash.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dup_actions  # noqa: E402
import library  # noqa: E402

TOKEN = re.compile(r"\{([a-z]+)\}")
VARS = {"creator", "album", "site", "type", "year", "month", "date", "title", "name", "number", "ext"}
EDGE = " -_.,"
BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)
TYPE_NAMES = {"image": "Images", "video": "Videos", "audio": "Audio", "archive": "Archives", "other": "Other"}
MAX_PLAN = 20_000


def pattern_problem(pattern: str, *, folders: bool) -> str:
    text = (pattern or "").strip()
    if not text:
        return ""
    if len(text) > 200:
        return "longer than 200 characters"
    unknown = [name for name in TOKEN.findall(text) if name not in VARS]
    if unknown:
        return f"unknown variable {{{unknown[0]}}}"
    rest = TOKEN.sub("", text)
    if re.search(r"[{}]", rest):
        return "a { or } that is not part of a variable"
    if re.search(r'[<>:"|?*]', rest) or (not folders and re.search(r"[/\\]", rest)):
        return "a character Windows does not allow in names"
    if ".." in rest:
        return "'..' is not allowed"
    if not TOKEN.search(text):
        return "no variable: every file would get the same name" if not folders else ""
    return ""


def clean_part(text: str, limit: int = 120) -> str:
    text = BAD.sub("-", " ".join(str(text or "").split()))
    text = re.sub(r"-{2,}", "-", text).strip(EDGE)[:limit].strip(EDGE)
    if RESERVED.match(text):
        text = f"_{text}"
    return text


def values_for(row: dict, number: int, width: int) -> dict[str, str]:
    saved = float(row.get("saved_at") or 0) or time.time()
    stamp = time.localtime(saved)
    stem = Path(row["name"]).stem
    return {
        "creator": row.get("creator") or "",
        "album": row.get("album") or "",
        "site": (row.get("host") or "").removeprefix("www."),
        "type": TYPE_NAMES.get(row.get("kind") or "other", "Other"),
        "year": time.strftime("%Y", stamp),
        "month": time.strftime("%m", stamp),
        "date": time.strftime("%Y-%m-%d", stamp),
        "title": row.get("title") or "",
        "name": stem,
        "number": str(number).zfill(width),
        "ext": "",
    }


def render(pattern: str, values: dict[str, str]) -> str:
    pieces: list[str] = []
    pos = 0
    for match in TOKEN.finditer(pattern):
        literal = pattern[pos:match.start()]
        value = clean_part(values.get(match.group(1), ""))
        if value:
            pieces.append(literal)
            pieces.append(value)
        elif not pieces and literal.strip(EDGE):
            pieces.append(literal)
        pos = match.end()
    pieces.append(pattern[pos:])
    return re.sub(r"([ \-_.,])\1+", r"\1", "".join(pieces)).strip(EDGE)


def render_folders(pattern: str, values: dict[str, str]) -> list[str]:
    parts = []
    for level in re.split(r"[/\\]", pattern):
        text = clean_part(render(level, values))
        if text and text not in (".", ".."):
            parts.append(text)
    return parts


def _inside(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def plan(conn: sqlite3.Connection, body: dict) -> dict:
    folders = str(body.get("folders") or "").strip()
    names = str(body.get("names") or "").strip()
    for pattern, is_folder in ((folders, True), (names, False)):
        problem = pattern_problem(pattern, folders=is_folder)
        if problem:
            return {"ok": False, "error": f"The {'folder' if is_folder else 'name'} pattern can't be used: {problem}."}
    if not folders and not names:
        return {"ok": False, "error": "Enter a folder pattern, a name pattern, or both."}
    roots = [Path(str(r)).resolve() for r in body.get("roots") or [] if str(r)]
    root = Path(str(body.get("root") or "")).resolve() if body.get("root") else None
    if folders and (root is None or not _inside(root, roots)):
        return {"ok": False, "error": "Choose one of the library folders as the place to organize into."}
    filters = body.get("filters") if isinstance(body.get("filters"), dict) else {}
    where, args = library._filters({**filters, "missing": ""})
    rows = [dict(r) for r in conn.execute(
        f"SELECT id, path, name, kind, size, mtime, host, creator, album, title, saved_at FROM files{where} "
        "ORDER BY folder, saved_at, name LIMIT ?", (*args, MAX_PLAN + 1))]
    truncated = len(rows) > MAX_PLAN
    rows = rows[:MAX_PLAN]
    width = max(3, len(str(len(rows))))
    targets: dict[str, int] = {}
    moves = []
    counters: dict[str, int] = {}
    for row in rows:
        source = Path(row["path"])
        if not _inside(source.resolve(), roots):
            continue
        folder = root.joinpath(*render_folders(folders, values_for(row, 0, width))) if folders else source.parent
        key = str(folder).lower() if os.name == "nt" else str(folder)
        counters[key] = counters.get(key, 0) + 1
        values = values_for(row, counters[key], width)
        name = row["name"]
        if names:
            stem = clean_part(render(names, values), 180)
            name = f"{stem}{source.suffix}" if stem else row["name"]
        target = folder / name
        move = {"id": row["id"], "from": str(source), "to": str(target), "size": row["size"]}
        if target == source:
            continue
        target_key = str(target).lower() if os.name == "nt" else str(target)
        if target_key in targets:
            move["conflict"] = f"another file in this plan gets the same name ({Path(moves[targets[target_key]]['from']).name})"
        elif target.exists() and not _same_file(target, source):
            move["conflict"] = "a file with this name is already there"
        elif not source.is_file():
            move["conflict"] = "the file is missing"
        targets.setdefault(target_key, len(moves))
        moves.append(move)
    conflicts = sum(1 for m in moves if m.get("conflict"))
    return {"ok": True, "moves": moves, "count": len(moves), "conflicts": conflicts, "truncated": truncated,
            "id": uuid.uuid4().hex[:12]}


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)  # case-only rename on Windows
    except OSError:
        return False


def _journal(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _move(source: Path, target: Path) -> None:
    """Move without overwriting. Same drive: a rename. Another drive: copy, check the size, then
    remove the source (the move itself is what the person asked for)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not _same_file(target, source):
        raise FileExistsError(f"{target.name} is already there")
    try:
        os.rename(source, target)  # on Windows rename never replaces an existing file
        return
    except OSError as error:
        if getattr(error, "errno", None) != 18 and getattr(error, "winerror", None) != 17:  # EXDEV / not same device
            raise
    temp = target.with_name(f".{target.name}.nexdrop-move")
    shutil.copy2(source, temp)
    if temp.stat().st_size != source.stat().st_size:
        temp.unlink()
        raise OSError("the copy has a different size")
    if target.exists():
        temp.unlink()
        raise FileExistsError(f"{target.name} is already there")
    os.rename(temp, target)
    source.unlink()


def apply(conn: sqlite3.Connection, body: dict) -> dict:
    planned = body.get("plan") if isinstance(body.get("plan"), dict) else {}
    journal = Path(str(body.get("journal") or ""))
    jobs = Path(str(body["jobs"])) if body.get("jobs") else None
    roots = [Path(str(r)).resolve() for r in body.get("roots") or [] if str(r)]
    batch = str(planned.get("id") or uuid.uuid4().hex[:12])
    done = skipped = 0
    errors: list[str] = []
    _journal(journal, {"batch": batch, "start": time.time(), "label": str(body.get("label") or "")[:200]})
    for move in planned.get("moves") or []:
        if move.get("conflict"):
            skipped += 1
            continue
        source, target = Path(str(move.get("from") or "")), Path(str(move.get("to") or ""))
        if not (_inside(source.resolve(), roots) and _inside(target.parent.resolve() if target.parent.exists() else target.parent, roots)):
            skipped += 1
            errors.append(f"{source.name}: outside the library folders")
            continue
        try:
            if not source.is_file() or source.stat().st_size != int(move.get("size") or -1):
                raise OSError("the file changed or is gone since the preview")
            made = _missing_folders(target.parent)
            _journal(journal, {"batch": batch, "from": str(source), "to": str(target), "id": move.get("id"), "made": made})
            _move(source, target)
        except OSError as error:
            skipped += 1
            errors.append(f"{source.name}: {getattr(error, 'strerror', None) or error}")
            _journal(journal, {"batch": batch, "failed": str(source)})
            continue
        library.moved(conn, int(move.get("id") or 0), str(target))
        dup_actions.record(jobs, source, target)
        done += 1
    _journal(journal, {"batch": batch, "end": time.time(), "moved": done})
    return {"ok": True, "batch": batch, "moved": done, "skipped": skipped, "errors": errors[:50]}


def batches(journal: Path) -> list[dict]:
    found: dict[str, dict] = {}
    if not journal.is_file():
        return []
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        batch = found.setdefault(entry.get("batch", ""), {"batch": entry.get("batch", ""), "moves": [], "failed": set(), "undone": False})
        if "start" in entry:
            batch.update(start=entry["start"], label=entry.get("label", ""))
        elif "from" in entry:
            batch["moves"].append(entry)
        elif "failed" in entry:
            batch["failed"].add(entry["failed"])
        elif entry.get("undone"):
            batch["undone"] = True
    out = []
    for batch in found.values():
        moves = [m for m in batch["moves"] if m["from"] not in batch["failed"]]
        out.append({"batch": batch["batch"], "start": batch.get("start", 0), "label": batch.get("label", ""),
                    "moves": moves, "count": len(moves), "undone": batch["undone"]})
    return sorted(out, key=lambda b: -b["start"])


def undo(conn: sqlite3.Connection, body: dict) -> dict:
    journal = Path(str(body.get("journal") or ""))
    jobs = Path(str(body["jobs"])) if body.get("jobs") else None
    wanted = str(body.get("batch") or "")
    batch = next((b for b in batches(journal) if b["batch"] == wanted), None)
    if batch is None:
        return {"ok": False, "error": "That change is not in the journal."}
    if batch["undone"]:
        return {"ok": False, "error": "That change was already undone."}
    restored = 0
    problems = []
    for move in reversed(batch["moves"]):
        source, target = Path(move["to"]), Path(move["from"])
        if not source.is_file():
            problems.append(f"{source.name} is not where it was moved to")
            continue
        if target.exists():
            problems.append(f"{target.name}: a file with that name is back in the old place")
            continue
        try:
            _move(source, target)
        except OSError as error:
            problems.append(f"{source.name}: {getattr(error, 'strerror', None) or error}")
            continue
        if move.get("id"):
            library.moved(conn, int(move["id"]), str(target))
        dup_actions.record(jobs, source, target)
        restored += 1
    # folders this batch created and that are empty again; folders that existed before always stay
    made = sorted({folder for move in batch["moves"] for folder in move.get("made") or []}, key=len, reverse=True)
    for folder in made:
        try:
            Path(folder).rmdir()  # only succeeds when empty
        except OSError:
            pass
    _journal(journal, {"batch": wanted, "undone": True, "at": time.time()})
    return {"ok": True, "restored": restored, "problems": problems[:50]}


def _missing_folders(folder: Path) -> list[str]:
    """The folders a move into `folder` is about to create, deepest first."""
    made = []
    while not folder.exists() and folder.parent != folder:
        made.append(str(folder))
        folder = folder.parent
    return made


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: organizer.py plan|apply|undo|batches <library.sqlite>")
    command, db = sys.argv[1], Path(sys.argv[2])
    try:
        body = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    conn = library.connect(db)
    try:
        if command == "plan":
            result = plan(conn, body)
        elif command == "apply":
            result = apply(conn, body)
        elif command == "undo":
            result = undo(conn, body)
        elif command == "batches":
            result = {"ok": True, "batches": [{k: v for k, v in b.items() if k != "moves"} for b in batches(Path(str(body.get("journal") or "")))][:30]}
        else:
            result = {"ok": False, "error": f"Unknown command {command}"}
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
