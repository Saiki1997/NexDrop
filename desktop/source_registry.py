"""Local website registry and unsupported-source log. Nothing here is uploaded."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
ENGINE = "10.10.0"
SENSITIVE = {
    "password",
    "pass",
    "token",
    "auth",
    "cookie",
    "session",
    "sig",
    "signature",
    "key",
    "access_token",
    "api_key",
    "jwt",
    "secret",
}
MAX_BYTES = 1_000_000


def log_dir(path: str | None = None) -> Path:
    folder = Path(path) if path else ROOT / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sanitize_url(url: str) -> dict:
    raw = str(url or "").strip()
    if raw.startswith("magnet:"):
        return {"host": "magnet", "pattern": "magnet:?xt=urn:btih:{hash}", "sanitized": "magnet:?xt=urn:btih:{hash}"}
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().removeprefix("www.")
    segments = []
    for segment in parts.path.split("/"):
        if not segment:
            continue
        if segment.isdigit() or len(segment) >= 16 or re.fullmatch(r"[A-Za-z0-9_-]{12,}", segment):
            segments.append("{id}")
        else:
            segments.append(segment[:80])
    path = "/" + "/".join(segments) if segments else "/"
    if parts.query:
        kept = []
        for item in parts.query.split("&"):
            name = item.split("=", 1)[0].lower()
            if name in SENSITIVE or any(token in name for token in SENSITIVE):
                continue
            if "=" in item and len(item.split("=", 1)[1]) > 24:
                kept.append(name + "={redacted}")
            else:
                kept.append(item[:80])
        query = "&".join(kept)
    else:
        query = ""
    sanitized = urlunsplit((parts.scheme or "https", host, path, query, ""))
    return {"host": host or "unknown", "pattern": f"{parts.scheme or 'https'}://{host}{path}", "sanitized": sanitized}


def classify_failure(message: str) -> str:
    text = str(message or "")
    lowered = text.lower()
    if any(token in lowered for token in ("401", "login", "unauthorized", "cookies")):
        return "authentication_required"
    if "404" in lowered or "not found" in lowered:
        return "unavailable"
    if "403" in lowered or "forbidden" in lowered or "access denied" in lowered:
        return "access_denied"
    if "429" in lowered or "too many" in lowered or "rate" in lowered:
        return "rate_limited"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "unsupported url" in lowered or "no crawler" in lowered:
        return "missing_handler"
    return "other"


def _store_path(folder: Path) -> Path:
    return folder / "unsupported-sources.json"


def _read_store(folder: Path) -> dict:
    path = _store_path(folder)
    if not path.is_file():
        return {"entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": []}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return {"entries": []}
    return data


def _write_store(folder: Path, data: dict) -> None:
    raw = json.dumps(data, indent=2)
    if len(raw.encode()) > MAX_BYTES:
        data["entries"] = sorted(data["entries"], key=lambda item: item.get("last_seen") or "")[-200:]
        raw = json.dumps(data, indent=2)
    path = _store_path(folder)
    path.write_text(raw, encoding="utf-8")
    lines = [json.dumps(item, ensure_ascii=True) for item in data["entries"]]
    (folder / "unsupported-sources.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    csv_rows = ["host,pattern,category,count,first_seen,last_seen,handler,retried"]
    for item in data["entries"]:
        csv_rows.append(
            ",".join(
                str(item.get(key) or "").replace(",", " ")
                for key in ("host", "pattern", "category", "count", "first_seen", "last_seen", "handler", "retried")
            )
        )
    (folder / "unsupported-sources.csv").write_text("\n".join(csv_rows) + "\n", encoding="utf-8")


def record_event(
    folder: Path,
    url: str,
    *,
    reason: str,
    category: str,
    handler: str = "",
    retried: bool = False,
) -> dict:
    clean = sanitize_url(url)
    data = _read_store(folder)
    key = (clean["host"], clean["pattern"], category)
    now = _now()
    for item in data["entries"]:
        if (item.get("host"), item.get("pattern"), item.get("category")) == key:
            item["count"] = int(item.get("count") or 1) + 1
            item["last_seen"] = now
            item["retried"] = bool(item.get("retried")) or retried
            _write_store(folder, data)
            _write_backlog(folder, data)
            return item
    item = {
        "host": clean["host"],
        "pattern": clean["pattern"],
        "sanitized": clean["sanitized"],
        "category": category,
        "reason": reason[:240],
        "handler": handler,
        "retried": retried,
        "count": 1,
        "first_seen": now,
        "last_seen": now,
    }
    data["entries"].append(item)
    _write_store(folder, data)
    _write_backlog(folder, data)
    return item


def _priority(item: dict) -> str:
    count = int(item.get("count") or 1)
    if item.get("category") == "missing_handler" and count >= 3:
        return "high"
    if count >= 2:
        return "normal"
    return "low"


def _write_backlog(folder: Path, data: dict) -> dict:
    candidates = []
    for item in data["entries"]:
        category = item.get("category")
        if category not in {"missing_handler", "unsupported_pattern"}:
            continue
        candidates.append(
            {
                "source": item.get("host"),
                "count": item.get("count"),
                "first_seen": item.get("first_seen"),
                "last_seen": item.get("last_seen"),
                "patterns": [item.get("pattern")],
                "reason": item.get("reason"),
                "known_interface": False,
                "next_action": "Review in a later release. Do not generate a crawler from this URL.",
                "priority": _priority(item),
                "status": "logged",
            }
        )
    candidates.sort(key=lambda item: (-int(item["count"] or 0), item["source"] or ""))
    payload = {"generated": _now(), "candidates": candidates}
    (folder / "source-update-backlog.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def clear_log(folder: Path) -> None:
    _write_store(folder, {"entries": []})
    _write_backlog(folder, {"entries": []})


def load_registry(folder: Path) -> dict:
    path = folder / "source-registry.json"
    if not path.is_file():
        return {"engine": ENGINE, "handlers": [], "checked_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"engine": ENGINE, "handlers": [], "checked_at": None}


def handler_for_host(registry: dict, host: str) -> str:
    wanted = host.lower().removeprefix("www.")
    for item in registry.get("handlers") or []:
        domains = [str(domain).lower() for domain in item.get("domains") or []]
        for domain in domains:
            if wanted == domain or wanted.endswith("." + domain) or wanted.startswith(domain + "."):
                return str(item.get("handler") or "")
    return ""


def build_registry(folder: Path) -> dict:
    sys.path.insert(0, str(ROOT / "engine" / "vendor"))
    from cyberdrop_dl.scrape_mapper import get_crawlers_mapping

    mapping = get_crawlers_mapping()
    grouped: dict[type, set[str]] = {}
    for host, cls in mapping.items():
        grouped.setdefault(cls, set()).add(host)
    handlers = []
    for cls, hosts in sorted(grouped.items(), key=lambda item: item[0].DOMAIN):
        official = cls.DOMAIN != "simpcity"
        handlers.append(
            {
                "name": cls.FOLDER_DOMAIN or cls.DOMAIN,
                "domains": sorted(hosts),
                "handler": cls.__name__,
                "engine": ENGINE,
                "primary_url": str(cls.PRIMARY_URL),
                "checked_at": _now(),
                "last_success": None,
                "status": "supported_not_recently_tested" if official else "separate_from_official_list",
                "auth": "cookies" if cls.DOMAIN in {"simpcity", "mega.nz"} else "",
                "limitations": "Not in the official 10.11.0 site list. Nested thread depth defaults to 0."
                if cls.DOMAIN == "simpcity"
                else "Registered only. No live download was recorded.",
                "update_available": False,
            }
        )
    payload = {
        "engine": ENGINE,
        "checked_at": _now(),
        "latest_seen": None,
        "offline": True,
        "handlers": handlers,
        "note": "A handler entry is not a successful download.",
    }
    (folder / "source-registry.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def detect_latest() -> str | None:
    import urllib.request

    try:
        with urllib.request.urlopen("https://pypi.org/pypi/cyberdrop-dl-patched/json", timeout=8) as response:
            payload = json.loads(response.read().decode())
        version = str(payload["info"]["version"])
        return version or None
    except Exception:
        return None


def compatibility_check(folder: Path, *, offline: bool) -> dict:
    registry = build_registry(folder)
    latest = None if offline else detect_latest()
    registry["latest_seen"] = latest
    registry["offline"] = offline or latest is None
    registry["update_available"] = bool(latest and latest != ENGINE)
    registry["runtime_update"] = "disabled"
    (folder / "source-registry.json").write_text(json.dumps(registry), encoding="utf-8")
    store = _read_store(folder)
    return {
        "engine": ENGINE,
        "latest": latest,
        "offline": registry["offline"],
        "checked_at": registry["checked_at"],
        "handlers": len(registry["handlers"]),
        "updates": 1 if registry["update_available"] else 0,
        "unsupported": len(store.get("entries") or []),
        "applied": False,
        "kept": ENGINE,
    }


def refuse_update() -> dict:
    return {"applied": False, "kept": ENGINE, "reason": "NEXDROP does not install crawler code at runtime."}


def status(folder: Path) -> dict:
    registry = load_registry(folder)
    store = _read_store(folder)
    return {
        "engine": registry.get("engine") or ENGINE,
        "latest": registry.get("latest_seen"),
        "offline": registry.get("offline", True),
        "checked_at": registry.get("checked_at"),
        "handlers": len(registry.get("handlers") or []),
        "updates": 1 if registry.get("update_available") else 0,
        "unsupported": len(store.get("entries") or []),
        "applied": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--backlog", action="store_true")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--record", default="")
    parser.add_argument("--category", default="missing_handler")
    parser.add_argument("--reason", default="No registered handler")
    args = parser.parse_args()
    folder = log_dir(args.dir or None)
    if args.clear:
        clear_log(folder)
        print(json.dumps({"cleared": True}))
        return 0
    if args.record:
        print(json.dumps(record_event(folder, args.record, reason=args.reason, category=args.category)))
        return 0
    if args.backlog:
        print((folder / "source-update-backlog.json").read_text(encoding="utf-8") if (folder / "source-update-backlog.json").is_file() else "{}")
        return 0
    if args.check:
        print(json.dumps(compatibility_check(folder, offline=args.offline)))
        return 0
    print(json.dumps(status(folder)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
