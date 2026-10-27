"""What happens to a new download whose content matches a file you already had (3.1).

Settings > Downloads > Same content as an earlier file:

  keep     keep both (default, nothing is touched)
  rename   keep both, the new copy gets " (duplicate)" in its name
  skip     the new copy is moved to a `_duplicates` folder inside the download folder, so the
           library keeps one copy in place; nothing is deleted
  ask      after the run, NexDrop lists each pair and you choose
  replace  like ask, and also offers to move the older copy to the Recycle Bin, after confirmation

Nothing here deletes a file. The Recycle Bin step is done by the window, only after the person
confirms it in a dialog. Every move or rename is also written to History, so History and the
Library still find the file.

    dup_actions.py aside|rename <jobs.sqlite>     stdin: {"path": file, "root": download folder}
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ACTIONS = ("keep", "rename", "skip", "ask", "replace")
ASIDE = "_duplicates"


def _free(target: Path) -> Path:
    if not target.exists():
        return target
    for number in range(2, 10_000):
        candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError("no free name")


def set_aside(path: Path, root: Path) -> Path:
    """Move `path` to <root>/_duplicates/<its place under root>. Never overwrites."""
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = Path(path.name)
    if relative.parts and relative.parts[0] == ASIDE:
        return path
    target = _free(root / ASIDE / relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    path.rename(target)
    return target


def rename_copy(path: Path) -> Path:
    if path.stem.endswith("(duplicate)"):
        return path
    target = _free(path.with_name(f"{path.stem} (duplicate){path.suffix}"))
    path.rename(target)
    return target


def record(jobs_db: Path | None, old: Path, new: Path) -> None:
    if not jobs_db or not jobs_db.is_file():
        return
    try:
        conn = sqlite3.connect(jobs_db, timeout=10)
        conn.execute("UPDATE items SET destination = ?, filename = ?, updated_at = ? WHERE destination = ?",
                     (str(new), new.name, time.time(), str(old)))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def apply(action: str, pairs: list[tuple[str, str]], root: Path, jobs_db: Path | None) -> list[dict]:
    """Apply `rename` or `skip` to the new copies; other actions leave the files alone.
    Returns one row per pair: new path, earlier file, what was done."""
    out = []
    for new, other in pairs:
        row = {"path": new, "of": other, "done": "kept"}
        source = Path(new)
        if action in ("rename", "skip") and source.is_file() and Path(other).is_file():
            try:
                target = set_aside(source, root) if action == "skip" else rename_copy(source)
                if target != source:
                    record(jobs_db, source, target)
                    row.update(path=str(target), done="set aside" if action == "skip" else "renamed")
            except OSError as error:
                row["error"] = str(error.strerror or error)
        out.append(row)
    return out


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    jobs_db = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    try:
        body = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        body = {}
    path = Path(str(body.get("path") or ""))
    if not path.is_file():
        result = {"ok": False, "error": "The file is not there any more."}
    elif command in ("aside", "rename"):
        try:
            target = set_aside(path, Path(str(body.get("root") or path.parent))) if command == "aside" else rename_copy(path)
            record(jobs_db, path, target)
            result = {"ok": True, "path": str(target)}
        except OSError as error:
            result = {"ok": False, "error": f"Could not move the file: {error.strerror or error}"}
    else:
        result = {"ok": False, "error": "usage: dup_actions.py aside|rename <jobs.sqlite>"}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
