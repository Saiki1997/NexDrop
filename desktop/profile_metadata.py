"""Offline profile/gallery metadata discovery for approved site adapters.

This module deliberately never fetches a URL or resolves downloadable media.
Site adapters may supply public HTML obtained through their authorized transport.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
import re


@dataclass(frozen=True)
class ProfileItem:
    url: str
    title: str = ""
    thumbnail: str = ""
    published: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _same_site_url(base: str, href: str) -> str | None:
    """Accept http(s) references on the *same host*, excluding media URLs."""
    try:
        target = urljoin(base, href.strip())
        source, parsed = urlsplit(base), urlsplit(target)
        if (parsed.scheme not in ("http", "https") or
                parsed.hostname != source.hostname or
                parsed.username is not None or parsed.password is not None):
            return None
        path = parsed.path or "/"
        if re.search(r"\.(?:mp4|m3u8|mp3|webm|mov|jpg|jpeg|png|gif|webp|zip)(?:$)", path, re.I):
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
    except (ValueError, AttributeError):
        return None


class _ListingParser(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.items: list[ProfileItem] = []
        self.next_page: str | None = None
        self._anchor: dict | None = None
        self._text: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "link" and "next" in (attr.get("rel") or "").lower().split():
            self.next_page = _same_site_url(self.base, attr.get("href") or "")
        if tag == "a":
            url = _same_site_url(self.base, attr.get("href") or "")
            self._anchor = {"url": url, "title": attr.get("title") or "", "thumbnail": "", "published": ""}
            self._text = []
            if "next" in (attr.get("rel") or "").lower().split() and url:
                self.next_page = url
        elif self._anchor is not None:
            if tag == "img":
                self._anchor["thumbnail"] = attr.get("src") or attr.get("data-src") or ""
                self._anchor["title"] = self._anchor["title"] or attr.get("alt") or ""
            elif tag == "time":
                self._anchor["published"] = attr.get("datetime") or ""

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._anchor is None:
            return
        entry = self._anchor
        url = entry["url"]
        if url and url not in self._seen and url != _same_site_url(self.base, self.base):
            title = entry["title"] or " ".join(" ".join(self._text).split())
            # Generic parser includes only likely listing entries. The site adapter
            # should provide stricter post URL patterns for production.
            if title or entry["thumbnail"]:
                self.items.append(ProfileItem(url=url, title=title[:300],
                                              thumbnail=entry["thumbnail"][:1000],
                                              published=entry["published"][:100]))
                self._seen.add(url)
        self._anchor = None
        self._text = []


def parse_listing(html: str, page_url: str, *, item_path_pattern: str) -> dict:
    """Extract ordered post metadata and a next-page reference from HTML.

    An explicit site-specific URL path regular expression is REQUIRED.
    Nothing is downloaded, and thumbnail URLs are metadata only.
    """
    if not item_path_pattern:
        raise ValueError("Explicit item_path_pattern required")
    pattern = re.compile(item_path_pattern)
    parser = _ListingParser(page_url)
    parser.feed(html)
    results = [entry.to_dict() for entry in parser.items
               if pattern.fullmatch(urlsplit(entry.url).path)]
    return {"page": page_url, "items": results, "next_page": parser.next_page}


def classify_monotirou_url(url: str) -> dict[str, str]:
    """Classify a Monotirou page without fetching HTML or media.

    Canonical URLs use HTTPS and the bare host, omit fragments and incidental
    rank/surface context, and preserve other query parameters (e.g. pagination).
    Unsupported hosts, paths, credentials, and non-default ports raise ValueError.
    """
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or
            parsed.hostname not in ("monotirou.com", "www.monotirou.com")):
        raise ValueError("Unrecognized Monotirou host")
    if (parsed.username is not None or parsed.password is not None or
            parsed.port not in (None, 80 if parsed.scheme == "http" else 443)):
        raise ValueError("Unexpected credentials or port")

    path = parsed.path or "/"
    info = {"host": parsed.hostname}
    search = dict(parse_qsl(parsed.query)).get("q", "").strip()
    if path == "/" and search:
        info["type"] = "search"
        info["query"] = search
    elif path == "/":
        info["type"] = "home"
    elif tag := re.fullmatch(r"/tags/([^/]{1,80})/?", path):
        info["type"] = "tag"
        info["tag"] = unquote(tag.group(1))
        path = f"/tags/{tag.group(1)}"
    else:
        match = re.fullmatch(r"/(users|video)/([A-Za-z0-9_-]{1,80})/?", path)
        if not match:
            raise ValueError("Expected home, a search, /tags/{tag}, /users/{username}, or /video/{id}")
        section, identifier = match.groups()
        info["type"] = "profile" if section == "users" else "video"
        info["username" if section == "users" else "video_id"] = identifier
        path = f"/{section}/{identifier}"

    query = urlencode([(key, value) for key, value in
                       parse_qsl(parsed.query, keep_blank_values=True)
                       if key not in ("rank", "surface")])
    info["canonical"] = urlunsplit(("https", "monotirou.com", path, query, ""))
    return info


def profile_url_parts(url: str, *, domain: str) -> dict[str, str]:
    """Validate a /users/{username} URL without fetching its content."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in (domain, "www." + domain):
        raise ValueError("Unrecognized profile host")
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError("Unexpected credentials or port")
    match = re.fullmatch(r"/users/([A-Za-z0-9_-]{1,80})/?", parsed.path)
    if not match:
        raise ValueError("Expected /users/{username}")
    return {"host": parsed.hostname, "username": match.group(1),
            "canonical": f"https://{domain}/users/{match.group(1)}"}
