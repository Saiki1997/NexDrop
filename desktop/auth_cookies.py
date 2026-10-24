"""Host-scoped Netscape cookies. Cookie values are not logged."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

MAX_FILE_BYTES = 5_000_000
HTTPONLY = "#HttpOnly_"

CHROME_LIMIT = (
    "Chrome encrypts cookies with Windows, and this app does not decrypt that database. "
    "Export a Netscape cookies.txt for this site from your browser and use Import cookie file. "
    "NEXDROP will keep only that site. It will not read your passwords, payment data, or other sites."
)


def host_matches(cookie_domain: str, wanted: str) -> bool:
    domain = cookie_domain.lower().lstrip(".").removeprefix("www.")
    host = wanted.lower().lstrip(".").removeprefix("www.")
    return domain == host or domain.endswith("." + host) or host.endswith("." + domain)


def _expiry(value: object) -> int:
    try:
        return max(0, int(float(str(value).strip() or 0)))
    except (TypeError, ValueError):
        return 0


def _json_cookie_lines(text: str) -> list[str]:
    """Rows from a Playwright storage_state.json or a browser-extension JSON export."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    cookies = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(cookies, list):
        return []
    rows = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        if not name or not domain or any(ch in name for ch in "\t\r\n"):
            continue
        value = str(cookie.get("value") or "").replace("\t", " ").replace("\n", " ")
        prefix = HTTPONLY if cookie.get("httpOnly") else ""
        rows.append("\t".join((
            prefix + domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(cookie.get("path") or "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(_expiry(cookie.get("expires", cookie.get("expirationDate", 0)))),
            name,
            value,
        )))
    return rows


def _cookie_rows(text: str) -> list[list[str]]:
    text = text.lstrip("\ufeff")
    if text.lstrip().startswith(("{", "[")):
        lines = _json_cookie_lines(text)
    else:
        lines = text.splitlines()
    rows = []
    for line in lines:
        raw = line.strip("\r\n")
        if not raw.strip():
            continue
        # Session cookies are often written as "#HttpOnly_.example.com"; they are data, not comments.
        if raw.startswith("#") and not raw.startswith(HTTPONLY):
            continue
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        rows.append(parts[:7])
    return rows


def filter_netscape(text: str, hosts: list[str]) -> str:
    kept = ["# Netscape HTTP Cookie File"]
    for parts in _cookie_rows(text):
        if any(host_matches(parts[0].removeprefix(HTTPONLY), host) for host in hosts):
            kept.append("\t".join(parts))
    return "\n".join(kept) + "\n"


def convert_any(text: str) -> str:
    """Normalise a cookie file (Netscape or JSON) without filtering hosts."""
    rows = ["# Netscape HTTP Cookie File", *("\t".join(parts) for parts in _cookie_rows(text))]
    return "\n".join(rows) + "\n"


def cookie_status(text: str, hosts: list[str]) -> dict:
    filtered = filter_netscape(text, hosts)
    rows = [line for line in filtered.splitlines() if line and not line.startswith("#")]
    if not rows:
        return {"state": "missing", "message": "No cookies for this site were found in that file."}
    now = time.time()
    live = 0
    expired = 0
    for line in rows:
        expiry = _expiry(line.split("\t")[4])
        if expiry and expiry < now:
            expired += 1
        else:
            live += 1
    if live:
        return {"state": "present", "message": f"{live} cookie(s) for this site are still in date. Remote login was not checked."}
    return {"state": "expired", "message": f"All {expired} cookie(s) for this site are expired. Import a new file."}


def chrome_probe(cookie_db: str) -> dict:
    path = Path(cookie_db)
    if not path.is_file():
        return {"ok": False, "code": "missing", "message": "That Chrome profile has no cookie database. Use a Netscape cookie file instead."}
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return {
            "ok": False,
            "code": "locked",
            "message": "Chrome is using that profile, so the cookie database could not be opened. Close Chrome or export a Netscape cookie file.",
        }
    try:
        row = connection.execute("SELECT value FROM cookies LIMIT 1").fetchone()
    except sqlite3.Error:
        return {"ok": False, "code": "unreadable", "message": CHROME_LIMIT}
    finally:
        connection.close()
    if row is None:
        return {"ok": False, "code": "empty", "message": "That Chrome profile has no cookies. Use a Netscape cookie file."}
    value = row[0]
    if isinstance(value, bytes) and (value.startswith(b"v10") or value.startswith(b"v20")):
        return {"ok": False, "code": "encrypted", "message": CHROME_LIMIT}
    return {"ok": False, "code": "unsupported", "message": CHROME_LIMIT}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default="")
    parser.add_argument("--hosts", default="")
    parser.add_argument("--status", default="")
    parser.add_argument("--probe", default="")
    parser.add_argument("--convert", default="")
    args = parser.parse_args()
    hosts = [host.strip().lower() for host in args.hosts.split(",") if host.strip()]
    for value in (args.status, args.filter, args.convert):
        if value and (not Path(value).is_file() or Path(value).stat().st_size > MAX_FILE_BYTES):
            print(json.dumps({"state": "missing", "message": "That cookie file is missing or larger than 5 MB."}))
            return 1
    if args.status:
        print(json.dumps(cookie_status(_read(args.status), hosts)))
        return 0
    if args.probe:
        print(json.dumps(chrome_probe(args.probe)))
        return 0
    if args.filter:
        sys.stdout.write(filter_netscape(_read(args.filter), hosts))
        return 0
    if args.convert:
        sys.stdout.write(convert_any(_read(args.convert)))
        return 0
    return 2


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
