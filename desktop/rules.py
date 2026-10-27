"""Download rules (3.1, Settings > Downloads > Rules): local "if this file ..., then ..." rules.

One rule is {"id", "enabled", "name", "when": [conditions], "then": action, "folder"}.

Conditions (all must hold):
    {"field": "type",    "op": "is",       "value": "image" | "video" | "audio" | "other"}
    {"field": "ext",     "op": "in",       "value": "jpg, png"}
    {"field": "site",    "op": "is",       "value": "bunkr.cr"}          sub-domains included
    {"field": "creator", "op": "is" | "contains", "value": "alice"}
    {"field": "name",    "op": "matches" | "not-matches", "value": "*preview*"}   * and ? wildcards
    {"field": "url",     "op": "contains" | "not-contains", "value": "/videos/"}
    {"field": "size",    "op": "over" | "under", "value": megabytes}
    {"field": "date",    "op": "before" | "after", "value": "2024-01-31"}   upload date, when the site gives one

Actions:
    "skip"      do not download (an exclusion rule)
    "only"      an inclusion rule: when any enabled "only" rule exists, a file must match one of them
    "folder"    save into `folder` (chosen in a folder dialog); the file keeps its album folders
    "single"    files like this download one at a time, so they don't hold up the rest

The engine checks rules where the facts are known: site, type, name, URL, creator and date when a
file is found; size once the server has answered. A "folder" or "single" rule can't use size,
because the folder and the download slot are chosen before the size is known; validate() says so.
Nothing here touches files: it only answers questions.

    rules.py preview <library.sqlite>     stdin: {"rules": [...]}
        what the rules would do to the files already in the Library (read only)
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import re
import sqlite3
import sys
from pathlib import PurePosixPath
from urllib.parse import urlsplit

FIELDS = {
    "type": ("is",),
    "ext": ("in",),
    "site": ("is",),
    "creator": ("is", "contains"),
    "name": ("matches", "not-matches"),
    "url": ("contains", "not-contains"),
    "size": ("over", "under"),
    "date": ("before", "after"),
}
ACTIONS = ("skip", "only", "folder", "single")
TYPES = ("image", "video", "audio", "other")
SIZE_ACTIONS = ("skip", "only")
MAX_RULES = 50

IMAGE = {"jpg", "jpeg", "png", "gif", "webp", "avif", "heic", "heif", "bmp", "tif", "tiff", "jxl", "svg"}
VIDEO = {"mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "ts", "m2ts", "3gp", "mpg", "mpeg", "m3u8"}
AUDIO = {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma", "alac"}


def kind_of(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return "image" if ext in IMAGE else "video" if ext in VIDEO else "audio" if ext in AUDIO else "other"


def _site(text: str) -> str:
    text = str(text or "").strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    return text.removeprefix("www.").strip("/")


def problems(rule: dict) -> list[str]:
    """What is wrong with one rule; [] when it can be used."""
    out = []
    when = rule.get("when") if isinstance(rule.get("when"), list) else []
    action = rule.get("then")
    if action not in ACTIONS:
        out.append("choose what the rule does")
    if not when:
        out.append("add at least one condition")
    for cond in when:
        field, op, value = cond.get("field"), cond.get("op"), str(cond.get("value") or "").strip()
        if field not in FIELDS or op not in FIELDS[field]:
            out.append("a condition is incomplete")
            continue
        if not value:
            out.append(f"the {field} condition has no value")
        elif field == "type" and value not in TYPES:
            out.append("the type must be images, videos, audio or other files")
        elif field == "site" and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", _site(value)):
            out.append(f"{value!r} is not a site such as bunkr.cr")
        elif field == "size":
            try:
                if float(value) <= 0:
                    raise ValueError
            except ValueError:
                out.append("the size must be a number of MB above 0")
            if action not in SIZE_ACTIONS:
                out.append("size can only be used to skip or to only download files (the size is known after the folder is chosen)")
        elif field == "date":
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                out.append("the date must look like 2024-01-31")
        elif field == "name" and len(value) > 200:
            out.append("the name pattern is too long")
    if action == "folder" and not str(rule.get("folder") or "").strip():
        out.append("choose the folder")
    return out


def clean(raw: object) -> list[dict]:
    """The enabled rules that can be used, in order."""
    rules = []
    for rule in raw if isinstance(raw, list) else []:
        if isinstance(rule, dict) and rule.get("enabled", True) and not problems(rule):
            rules.append(rule)
    return rules[:MAX_RULES]


def facts(url: str, filename: str, *, creator: str = "", uploaded: object = None, size: int | None = None) -> dict:
    ext = PurePosixPath(filename or urlsplit(url).path).suffix.lower().lstrip(".")
    date = None
    if isinstance(uploaded, (int, float)) and uploaded > 0:
        date = dt.datetime.fromtimestamp(uploaded, tz=dt.timezone.utc).date()
    elif isinstance(uploaded, dt.datetime):
        date = uploaded.date()
    elif isinstance(uploaded, dt.date):
        date = uploaded
    return {"url": str(url), "site": _site(url), "name": filename or "", "ext": ext, "type": kind_of(ext),
            "creator": creator or "", "date": date, "size": size}


def _holds(cond: dict, fact: dict) -> bool | None:
    """True or False, or None when the fact isn't known yet (size before the server answered)."""
    field, op, value = cond["field"], cond["op"], str(cond.get("value") or "").strip()
    if field == "type":
        return fact["type"] == value
    if field == "ext":
        wanted = {v.strip().lower().lstrip(".") for v in value.split(",") if v.strip()}
        return fact["ext"] in wanted
    if field == "site":
        site = _site(value)
        return fact["site"] == site or fact["site"].endswith("." + site)
    if field == "creator":
        have = fact["creator"].lower()
        return have == value.lower() if op == "is" else value.lower() in have
    if field == "name":
        hit = fnmatch.fnmatch(fact["name"].lower(), value.lower())
        return hit if op == "matches" else not hit
    if field == "url":
        hit = value.lower() in fact["url"].lower()
        return hit if op == "contains" else not hit
    if field == "size":
        if fact.get("size") is None:
            return None
        limit = float(value) * 1024 * 1024
        return fact["size"] > limit if op == "over" else fact["size"] < limit
    if field == "date":
        if fact.get("date") is None:
            return False  # no upload date: a date rule doesn't apply
        day = dt.date.fromisoformat(value)
        return fact["date"] < day if op == "before" else fact["date"] > day
    return False


def matches(rule: dict, fact: dict) -> bool | None:
    result = True
    for cond in rule["when"]:
        held = _holds(cond, fact)
        if held is False:
            return False
        if held is None:
            result = None
    return result


def decide(rules: list[dict], fact: dict) -> dict:
    """What the rules say about a file: {"skip": reason or "", "folder": path or "", "single": bool,
    "pending": True when a size rule still has to be checked}."""
    out = {"skip": "", "folder": "", "single": False, "pending": False, "rule": ""}
    includes = [r for r in rules if r["then"] == "only"]
    if includes:
        results = [matches(r, fact) for r in includes]
        if True not in results:
            if None in results:
                out["pending"] = True
            else:
                out["skip"] = "not matched by an \"only download\" rule"
                return out
    for rule in rules:
        if rule["then"] == "only":
            continue
        result = matches(rule, fact)
        if result is None:
            out["pending"] = True
            continue
        if not result:
            continue
        label = rule.get("name") or describe(rule)
        if rule["then"] == "skip":
            out.update(skip=f"rule: {label}", rule=label)
            return out
        if rule["then"] == "folder" and not out["folder"]:
            out.update(folder=str(rule["folder"]), rule=out["rule"] or label)
        if rule["then"] == "single":
            out["single"] = True
    return out


WORDS = {"image": "images", "video": "videos", "audio": "audio", "other": "other files"}


def describe(rule: dict) -> str:
    parts = []
    for cond in rule.get("when") or []:
        field, op, value = cond.get("field"), cond.get("op"), cond.get("value")
        if field == "type":
            parts.append(WORDS.get(str(value), str(value)))
        elif field == "size":
            parts.append(f"{op} {value} MB")
        else:
            parts.append(f"{field} {op.replace('-', ' ')} {value}")
    action = {"skip": "skip", "only": "only download", "folder": f"save to {rule.get('folder')}", "single": "one at a time"}.get(rule.get("then"), "")
    return f"{' and '.join(parts)}: {action}"


def preview(rules: list[dict], db_path: str, limit: int = 200_000) -> dict:
    """Apply `rules` to the Library's files: how many each rule would skip, move or slow down.
    Upload dates aren't in the Library, so date conditions don't match here."""
    out = {"ok": True, "total": 0, "skipped": 0, "folder": 0, "single": 0, "byRule": {}, "examples": {"skipped": [], "folder": [], "single": []}}
    if not rules:
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        rows = conn.execute("SELECT name, url, creator, size FROM files WHERE missing = 0 LIMIT ?", (limit,)).fetchall()
        conn.close()
    except sqlite3.Error as error:
        return {"ok": False, "error": f"The Library can't be read: {error}"}
    for name, url, creator, size in rows:
        out["total"] += 1
        verdict = decide(rules, facts(url or "", name or "", creator=creator or "", size=size))
        for key, hit in (("skipped", bool(verdict["skip"])), ("folder", bool(verdict["folder"]) and not verdict["skip"]),
                         ("single", verdict["single"] and not verdict["skip"])):
            if hit:
                out[key] += 1
                if len(out["examples"][key]) < 8:
                    out["examples"][key].append(name)
        label = verdict["skip"].removeprefix("rule: ") if verdict["skip"] else verdict["rule"]
        if label:
            out["byRule"][label] = out["byRule"].get(label, 0) + 1
    return out


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "preview":
        print(json.dumps({"ok": False, "error": "usage: rules.py preview <library.sqlite>"}))
        return 0
    try:
        body = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        body = {}
    print(json.dumps(preview(clean(body.get("rules")), sys.argv[2]), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
