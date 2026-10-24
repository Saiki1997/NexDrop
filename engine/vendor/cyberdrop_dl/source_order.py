"""Remember the order a page listed its files. This does not download anything."""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

ORDER_MODE: ContextVar[str] = ContextVar("ORDER_MODE", default="source")
SEQUENCE_PREFIX: ContextVar[bool] = ContextVar("SEQUENCE_PREFIX", default=True)
_SENSITIVE = ("password", "token", "auth", "cookie", "session", "sig", "key", "secret")
_sequences: dict[str, int] = {}
_next = 1


def reset(manifest: str | None = None) -> None:
    global _next
    _sequences.clear()
    _next = 1
    if not manifest:
        return
    path = Path(manifest)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = str(item.get("url") or "")
        number = int(item.get("sequence") or 0)
        if url and number > 0:
            _sequences[url] = number
            _next = max(_next, number + 1)


def public_url(url: str) -> str:
    parts = urlsplit(str(url))
    query = "&".join(
        f"{name}={value}"
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not any(token in name.lower() for token in _SENSITIVE)
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def assign(url: str) -> int:
    global _next
    key = public_url(url)
    if key in _sequences:
        return _sequences[key]
    _sequences[key] = _next
    _next += 1
    return _sequences[key]


def prefix_name(number: int, name: str) -> str:
    if re.match(r"^\d{3}_", name) or re.match(r"^\d{3} - ", name):
        return name
    return f"{number:03d}_{name}"


def numbered_filename(url: str, original: str, *, enabled: bool | None = None) -> tuple[int, str]:
    """Sequence from source order, then the saved filename. Same URL keeps its number."""
    number = assign(url)
    if enabled is None:
        enabled = SEQUENCE_PREFIX.get()
    name = prefix_name(number, original) if enabled else original
    return number, name


def order_urls(html: str, urls: list[str]) -> list[str]:
    """Document order. A URL that is not in the HTML stays after the ones that are."""
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for index, url in enumerate(urls):
        if not url or url in seen:
            continue
        seen.add(url)
        position = html.find(url)
        if position < 0:
            bare = url.split("?", 1)[0]
            position = html.find(bare) if bare else -1
        ranked.append((position if position >= 0 else 10**9, index, url))
    ranked.sort()
    return [url for _, _, url in ranked]


def remember(path: Path, url: str, sequence: int, name: str, method: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"url": public_url(url), "sequence": sequence, "name": name, "method": method}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
