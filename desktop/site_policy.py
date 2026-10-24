"""NEXDROP's site policy: supported, review required, unsupported.

Two sources, merged:
- desktop/site-policy.json: NEXDROP's own assessments. Each entry records the
  state, the reason, the date and what would trigger a reassessment. Changed
  only by a new assessment (docs/site-assessments/).
- engine/appdata/approved-sites.json: sites the user added in Settings > Sites.
  A new site is "review": its pages can be test-crawled (desktop/site_check.py)
  but nothing downloads. It becomes "supported" only when the user confirms they
  own the content or are authorized to download it. A user entry can't override
  a built-in "unsupported" assessment.

Sites in neither file keep the engine's normal behaviour.

States during a run:
- supported    downloads; user sites get a generic handler (see KINDS)
- review       pasted links are refused with the outstanding requirement;
               links found on pages are skipped
- unsupported  same, with the documented reason

Every refused or skipped link is appended to engine/appdata/logs/blocked.jsonl.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
POLICY_FILE = HERE / "site-policy.json"
APPDATA = ROOT / "engine" / "appdata"
APPROVED_FILE = APPDATA / "approved-sites.json"
AUDIT_FILE = APPDATA / "logs" / "blocked.jsonl"
AUDIT_MAX_BYTES = 2_000_000
STATES = ("supported", "review", "unsupported")
REVIEW_REASON = "Review required: test a page and confirm you're authorized to download from this site in Settings > Sites."
_LOCK = threading.Lock()


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


_POLICY = _read(POLICY_FILE, {})
KINDS: dict[str, str] = dict(_POLICY.get("kinds") or {"media": "Media pages"})


def _clean_host(value: str) -> str:
    value = value.strip().lower()
    if "://" in value:
        value = urlsplit(value).hostname or ""
    return value.removeprefix("www.")


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def builtin_sites() -> list[dict]:
    sites = []
    for item in _POLICY.get("sites", []):
        if isinstance(item, dict) and item.get("host") and item.get("state") in STATES:
            sites.append({**item, "host": _clean_host(str(item["host"])), "source": "nexdrop"})
    return sites


def user_sites(path: Path | None = None) -> list[dict]:
    data = _read(path or APPROVED_FILE, [])
    sites = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").rstrip("/")
        host = host_of(url)
        if not url.startswith(("https://", "http://")) or not host or item.get("kind") not in KINDS:
            continue
        authorized = bool(item.get("authorizedAt"))
        sites.append({
            "url": url, "host": host, "kind": item["kind"],
            "items": str(item.get("items") or "")[:300], "next": str(item.get("next") or "")[:300],
            "state": "supported" if authorized else "review",
            "authorizedAt": item.get("authorizedAt") or "", "testedAt": item.get("testedAt") or "",
            "reason": "" if authorized else REVIEW_REASON, "source": "user",
        })
    return sites


def _match(host: str, entries: list[dict]) -> dict | None:
    best = None
    for entry in entries:
        domain = entry["host"]
        if host == domain or host.endswith("." + domain):
            if best is None or len(domain) > len(best["host"]):
                best = entry
    return best


def assessment(url_or_host: str, approved_path: Path | None = None) -> dict | None:
    """The policy entry that applies to a link, or None for sites NEXDROP has no entry for.

    A built-in "unsupported" assessment always wins; otherwise the user's entry, then a built-in one.
    """
    host = host_of(url_or_host) if "://" in url_or_host else _clean_host(url_or_host)
    if not host:
        return None
    builtin = _match(host, builtin_sites())
    if builtin and builtin["state"] == "unsupported":
        return builtin
    return _match(host, user_sites(approved_path)) or builtin


def blocked_reason(url_or_host: str, approved_path: Path | None = None) -> str:
    """Why a link may not be downloaded, or "" when it may."""
    entry = assessment(url_or_host, approved_path)
    if entry is None or entry["state"] == "supported":
        return ""
    if entry["state"] == "review":
        return entry.get("reason") or REVIEW_REASON
    return f"Unsupported site: {entry.get('reason') or 'see Settings > Sites'}"


def blocked_hosts(approved_path: Path | None = None) -> list[str]:
    """Hosts the engine must not download from in this run (review and unsupported)."""
    hosts = {e["host"] for e in builtin_sites() if e["state"] != "supported"}
    hosts |= {e["host"] for e in user_sites(approved_path) if e["state"] != "supported"}
    return sorted(hosts)


def approved_sites(approved_path: Path | None = None) -> list[dict]:
    """User sites that are supported: authorized and not overruled by a built-in assessment."""
    return [s for s in user_sites(approved_path) if s["state"] == "supported" and not blocked_reason(s["url"], approved_path)]


def audit(url: str, reason: str, source: str) -> None:
    """Append one line to engine/appdata/logs/blocked.jsonl (kept under ~2 MB)."""
    line = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "url": url[:500], "reason": reason[:300], "source": source})
    with _LOCK:
        try:
            AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
            if AUDIT_FILE.is_file() and AUDIT_FILE.stat().st_size > AUDIT_MAX_BYTES:
                keep = AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
                AUDIT_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")
            with AUDIT_FILE.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


if __name__ == "__main__":  # python site_policy.py <url> ...: prints the decision
    import sys

    for arg in sys.argv[1:]:
        print(arg, "->", blocked_reason(arg) or "allowed")
