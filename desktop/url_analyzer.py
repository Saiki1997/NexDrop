"""Tell what each link is before downloading it (3.1 URL analyzer and link preview).

    url_analyzer.py analyze            stdin: {"urls": [...]}             offline, from the engine's handlers
    url_analyzer.py preview            stdin: {"urls": [...]}             also reads each page or file header

Prints one JSON object. For each link:

- `site` and `handler`: "builtin" (a site handler matches), "generic" (a handler for a family of
  sites, such as forum software), "direct" (a plain file link), or "none";
- `kind`: direct file, album, gallery, profile, forum thread, forum post, post, playlist, video,
  audio, cloud folder, archive, search, tag, or "page" when the handler's link formats don't say;
- `pattern`: the handler's own name for the link format that matched (for example "Album");
- `signIn`: "required", "optional" or "" from the sign-in catalog;
- `key` and `duplicateOf`: the link without tracking parameters, and the index of an earlier link
  that is the same.

`preview` adds what one request shows: the page title, site name, author, the number of file links
seen on the page, or a file's size and type from its headers. Nothing is guessed: what a page does
not say stays empty, and a page that needs a browser check or a sign-in says so.
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path(__file__).resolve().parent / "auth-catalog.json"

TRACKING = re.compile(r"^(utm_[a-z]+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|ref_src|ref_url|_ga|yclid|share_id)$", re.I)
# Share trackers that mean nothing else only on these sites ("s" is a real search on others). Same list as format.js.
SITE_TRACKING = {"x.com": {"s", "t"}, "twitter.com": {"s", "t"}, "youtube.com": {"si", "pp", "feature"},
                 "youtu.be": {"si", "feature"}, "open.spotify.com": {"si"}, "instagram.com": {"igsh", "img_index"}}
SHORTENERS = {"bit.ly", "t.co", "tinyurl.com", "goo.gl", "is.gd", "ow.ly", "buff.ly", "rebrand.ly", "cutt.ly",
              "shorturl.at", "tiny.cc", "rb.gy", "shorturl.gg", "lnkd.in"}
MEDIA = {
    "image": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".heic", ".jxl"),
    "video": (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ts", ".m3u8", ".wmv", ".flv"),
    "audio": (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"),
    "archive": (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"),
}
KINDS = (  # first match wins, on the handler's own name for the link format
    ("post", ("tweet", "individual post", "story")),
    ("search", ("search",)),
    ("tag", ("tag", "category", "categories", "genre", "hashtag")),
    ("playlist", ("playlist",)),
    ("forum thread", ("thread",)),
    ("forum post", ("forum post",)),
    ("album", ("album", "collection", "set")),
    ("gallery", ("gallery", "galleries", "subreddit", "board", "pools")),
    ("cloud folder", ("folder", "share", "drive", "directory")),
    ("profile", ("profile", "user", "model", "channel", "creator", "account", "artist", "author", "star", "pornstar",
                 "performer", "member", "celeb", "series", "show", "maker", "label")),
    ("audio", ("audio", "track", "episode", "song", "podcast", "music")),
    ("video", ("video", "watch", "embed", "clip", "movie", "stream", "reel")),
    ("post", ("post", "status", "tweet", "message")),
    ("direct file", ("file", "image", "direct", "download", "photo", "picture", "media", "attachment", "gif", "thumbnail", "apk")),
)


def normalize(url: str) -> str:
    """The same link without tracking parameters, fragment, `www.` or a trailing slash."""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return url
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port:
        host = f"{host}:{parts.port}"
    drop = SITE_TRACKING.get(host, set())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k) and k not in drop]
    path = parts.path.rstrip("/") or "/"
    fragment = parts.fragment if parts.fragment.startswith("!") else ""  # "#!" links are real addresses
    return urlunparse((parts.scheme.lower(), host, path, "", urlencode(query), fragment))


def media_type(path: str) -> str:
    lower = path.lower()
    for kind, suffixes in MEDIA.items():
        if lower.endswith(suffixes):
            return kind
    return ""


def _pattern_regex(pattern: str, prefix: bool = False) -> re.Pattern[str] | None:
    """A handler link format such as "/a/<album_id>", "/album/..." or "/thread|threads/<id>" as a
    regex on the path. `prefix` also accepts longer paths (a post inside a thread, for example)."""
    text = pattern.strip()
    if text.startswith("http"):
        text = urlparse(text).path or "/"
    if text.startswith("?"):
        return None  # query-only formats say nothing about the kind
    text = text.split("?", 1)[0]
    if not text.startswith("/"):
        return None
    out = ""
    for piece in re.split(r"(<[^>]+>|\.\.\.)", text):
        if piece == "...":
            out += ".*"
        elif piece.startswith("<") and piece.endswith(">"):
            out += "[^/]+"
        else:  # "a|b" inside one path segment means either word
            out += "/".join(
                "(?:" + "|".join(re.escape(word) for word in segment.split("|")) + ")" if "|" in segment else re.escape(segment)
                for segment in piece.split("/")
            )
    return re.compile(f"^{out}" + ("(?:/.*)?$" if prefix else "/?$"), re.I)


def _kind_from_name(name: str) -> str:
    lower = name.lower()
    for kind, words in KINDS:
        if any(re.search(rf"\b{re.escape(word)}", lower) for word in words):
            return kind
    return ""


class Handlers:
    def __init__(self) -> None:
        vendor = str(ROOT / "engine" / "vendor")
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        try:
            from cyberdrop_dl.scrape_mapper import get_crawlers_mapping

            self.mapping = get_crawlers_mapping()
        except Exception:  # noqa: BLE001 - without the engine every link is "unknown"
            self.mapping = {}
        self.patterns: dict[type, list[tuple[str, re.Pattern[str]]]] = {}

    def find(self, host: str):
        if not host:
            return None
        if host in self.mapping:
            return self.mapping[host]
        matches = [key for key, cls in self.mapping.items() if key and key in host and _host_ok(cls, host)]
        return self.mapping[max(matches, key=len)] if matches else None

    def match(self, crawler, path: str) -> str:
        if crawler not in self.patterns:
            exact, loose = [], []
            for name, values in (getattr(crawler, "SUPPORTED_PATHS", {}) or {}).items():
                if str(name).startswith("**"):
                    continue  # notes, not link formats
                for value in values if isinstance(values, tuple) else (values,):
                    for bucket, prefix in ((exact, False), (loose, True)):
                        regex = _pattern_regex(str(value), prefix)
                        if regex is not None:
                            bucket.append((str(name), regex))
            self.patterns[crawler] = exact + loose
        for name, regex in self.patterns[crawler]:
            if regex.match(path or "/"):
                return name
        return ""


def _host_ok(cls, host: str) -> bool:
    try:
        return bool(cls.check_host_match(host))
    except Exception:  # noqa: BLE001
        return True


def _sign_in(host: str, site: str) -> str:
    try:
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    site = site.lower()
    for entry in catalog:
        hosts = [h.lower() for h in entry.get("hosts") or []]
        ident = str(entry.get("id") or "").lower().replace("_", "")
        if any(host == h or host.endswith("." + h) for h in hosts) or (ident and ident in (site.replace(".", ""), host.split(".")[0])):
            methods = entry.get("methods") or []
            return "optional" if "none" in methods else "required"
    return ""


def analyze(urls: list[str], handlers: Handlers | None = None) -> list[dict]:
    handlers = handlers or Handlers()
    seen: dict[str, int] = {}
    out = []
    for index, url in enumerate(urls):
        url = str(url).strip()
        key = normalize(url)
        parts = urlparse(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
        item = {"url": url, "key": key, "host": host, "site": "", "handler": "none", "kind": "", "pattern": "",
                "media": media_type(parts.path), "signIn": "", "duplicateOf": None, "shortened": host in SHORTENERS}
        if key in seen:
            item["duplicateOf"] = seen[key]
        else:
            seen[key] = index
        if url.lower().startswith("magnet:") or parts.path.lower().endswith(".torrent"):
            item.update(kind="torrent", handler="none")
            out.append(item)
            continue
        crawler = handlers.find(host)
        if crawler is not None:
            name = getattr(crawler, "NAME", "") or getattr(crawler, "DOMAIN", "")
            generic = bool(getattr(crawler, "IS_GENERIC", False)) or "generic" in str(name).lower()
            item["site"] = str(getattr(crawler, "FOLDER_DOMAIN", "") or name)
            item["handler"] = "generic" if generic else "builtin"
            item["pattern"] = handlers.match(crawler, parts.path)
            item["kind"] = _kind_from_name(item["pattern"]) if item["pattern"] else ""
            if item["kind"] == "forum thread" and re.search(r"/(?:posts?|goto)/|/post-\d+|#post-\d+", parts.path + "#" + parts.fragment):
                item["kind"] = "forum post"
        if item["media"]:
            if item["handler"] == "none":
                item["handler"] = "direct"
            if not item["kind"]:
                item["kind"] = "archive" if item["media"] == "archive" else "direct file"
        if not item["kind"]:
            item["kind"] = "page"
        item["signIn"] = _sign_in(host, item["site"])
        out.append(item)
    return out


# ─── preview: one request per link ─────────────────────────────────────────────

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
MAX_PAGE = 2 * 1024 * 1024
FILE_LINK = re.compile(r"""(?:href|src|data-src)\s*=\s*["']([^"'#]+?\.(?:jpe?g|png|gif|webp|avif|mp4|webm|mkv|mov|m4v|mp3|m4a|flac|zip|rar|7z))(?:\?[^"']*)?["']""", re.I)


def _meta(text: str, *names: str) -> str:
    for name in names:
        pattern = rf"""<meta[^>]+(?:property|name)\s*=\s*["']{re.escape(name)}["'][^>]*content\s*=\s*["']([^"']+)["']"""
        match = re.search(pattern, text, re.I) or re.search(
            rf"""<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]*(?:property|name)\s*=\s*["']{re.escape(name)}["']""", text, re.I)
        if match:
            return html.unescape(match.group(1)).strip()[:200]
    return ""


def _bot_check(text: str, status: int) -> str:
    lower = text[:20000].lower()
    if status in (403, 503) and ("cf-chl" in lower or "challenge-platform" in lower or "just a moment" in lower):
        return "The site shows a browser check first (Cloudflare). The download uses FlareSolverr for it."
    if "ddos-guard" in lower:
        return "The site shows a browser check first (DDoS-Guard)."
    return ""


def preview_one(item: dict, opener=None, safe=None) -> dict:
    """Fill a preview for one analysed link. `safe(host)` returns a reason when the host is not allowed."""
    url = item["url"]
    result = {"title": "", "siteName": "", "author": "", "fileLinks": None, "size": None, "type": "", "finalUrl": "",
              "status": None, "note": ""}
    if item.get("kind") == "torrent":
        result["note"] = "Torrent links need a BitTorrent client."
        return result
    host = urlparse(url).hostname or ""
    reason = safe(host) if safe else None
    if reason:
        result["note"] = reason
        return result
    opener = opener or urllib.request.build_opener()
    direct = item.get("handler") == "direct" or item.get("kind") in ("direct file", "archive")
    request = urllib.request.Request(url, method="HEAD" if direct else "GET",
                                     headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    try:
        with opener.open(request, timeout=20) as response:
            result["status"] = response.status
            result["finalUrl"] = response.geturl() if response.geturl() != url else ""
            length = response.headers.get("Content-Length")
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            result["type"] = ctype
            if direct or not ctype.startswith(("text/html", "application/xhtml")):
                result["size"] = int(length) if length and length.isdigit() else None
                return result
            body = response.read(MAX_PAGE).decode(response.headers.get_content_charset() or "utf-8", "replace")
    except urllib.error.HTTPError as error:
        result["status"] = error.code
        body = ""
        try:
            body = error.read(200_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        result["note"] = _bot_check(body, error.code) or {
            401: "The site asks for a sign-in.", 403: "The site refused the request (sign-in or a browser check may be needed).",
            404: "The page is not there (404).", 410: "The page was removed (410).", 429: "The site asked to slow down; try the preview later.",
        }.get(error.code, f"The site answered HTTP {error.code}.")
        return result
    except Exception as error:  # noqa: BLE001
        result["note"] = f"No answer: {str(error)[:120]}"
        return result
    note = _bot_check(body, result["status"] or 200)
    if note:
        result["note"] = note
        return result
    title = _meta(body, "og:title", "twitter:title")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:200] if match else ""
    result["title"] = title
    result["siteName"] = _meta(body, "og:site_name", "application-name")
    result["author"] = _meta(body, "author", "article:author", "twitter:creator")
    result["fileLinks"] = len({m.group(1) for m in FILE_LINK.finditer(body)})
    return result


def safe_opener(proxy: str, safe) -> urllib.request.OpenerDirector:
    """Follows redirects, but never to a private or local address (each hop is checked)."""

    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            reason = safe(urlparse(newurl).hostname or "")
            if reason:
                raise urllib.error.URLError(f"redirect blocked: {reason}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    handlers: list = [SafeRedirect]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def resolve_short(url: str, opener=None) -> str:
    """Where a link shortener points, from one HEAD request without downloading anything."""
    opener = opener or urllib.request.build_opener()
    try:
        with opener.open(urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT}), timeout=15) as resp:
            return resp.geturl()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        payload = {}
    urls = [str(u) for u in (payload.get("urls") or []) if isinstance(u, str)][:500]
    if command not in ("analyze", "preview"):
        print(json.dumps({"ok": False, "error": "usage: url_analyzer.py analyze|preview"}))
        return 2
    handlers = Handlers()
    items = analyze(urls, handlers)
    if command == "preview":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from concurrent.futures import ThreadPoolExecutor

        from cairn_session import blocked_reason, proxy_url

        opener = safe_opener(proxy_url(payload.get("proxy")), blocked_reason)
        # a shortened link is followed to where it points, then analysed again for its kind and duplicates
        for item in items:
            if item["shortened"] and not blocked_reason(item["host"]):
                target = resolve_short(item["url"], opener)
                if target and target != item["url"]:
                    item["resolved"] = target
        resolved = [item.get("resolved") or item["url"] for item in items]
        again = analyze(resolved, handlers)
        for item, fresh in zip(items, again):
            if item.get("resolved"):
                fresh.update(url=item["url"], resolved=item["resolved"], shortened=True)
        items = again
        with ThreadPoolExecutor(max_workers=6) as pool:
            previews = list(pool.map(lambda it: preview_one({**it, "url": it.get("resolved") or it["url"]}, opener, blocked_reason), items[:60]))
        for item, preview in zip(items, previews):
            item["preview"] = preview
    print(json.dumps({"ok": True, "items": items}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
