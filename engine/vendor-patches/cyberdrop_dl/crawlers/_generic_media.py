"""Generic media pages for sites the user approved in NEXDROP (Settings > Sites).

NEXDROP addition. Not registered for any domain by itself: engine/bridge.py
creates one subclass per approved site, the same way the engine builds its own
generic handlers (chevereto, kvs, video ... in config crawlers.generic).

What it reads from a page, in this order:
  1. JSON-LD VideoObject / AudioObject / ImageObject / MediaObject contentUrl
  2. <video>, <audio> and their <source> elements (src or data-src)
  3. Open Graph og:video / og:audio when they point at a media file or stream
  4. links (<a href>) to media files, and images inside gallery/album containers
  5. og:image, only when nothing else was found (on video pages it is a thumbnail)
HLS (.m3u8) goes through the engine's HLS downloader. DASH (.mpd) and blob:/MSE
players (usually DRM) are reported, not downloaded.

On the page the user pasted it also follows:
  - next-page links (rel="next", or the site's own selector), same host only
  - item links (the site's own selector, for listing pages), same host only
Pages reached that way are read for media but not paginated again, so a run
never wanders across a whole site.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urljoin, urlsplit

from cyberdrop_dl.crawlers.crawler import Crawler, URLConfig
from cyberdrop_dl.exceptions import ScrapeError
from cyberdrop_dl.utils.errors import error_handling_wrapper

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

    from cyberdrop_dl.url_objects import ScrapeItem

_logger = logging.getLogger("cyberdrop_dl")

IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".heic", ".jxl"}
VIDEO = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}
AUDIO = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}
_LD_TYPES = {"VideoObject": "video", "AudioObject": "audio", "ImageObject": "image", "MediaObject": ""}
_GALLERY = re.compile(r"gallery|album|carousel|slides|lightbox|photos|images|post-content|entry-content", re.I)
MAX_PAGES = 500


@dataclasses.dataclass(slots=True)
class Media:
    url: str
    kind: str  # image, video, audio, hls
    origin: str  # jsonld, tag, og, link, gallery


def ext_of(url: str) -> str:
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    return path[dot:] if dot > path.rfind("/") else ""


def kind_of(url: str, hint: str = "") -> str:
    ext = ext_of(url)
    if ext == ".m3u8" or "mpegurl" in hint.lower():
        return "hls"
    if ext == ".mpd" or "dash+xml" in hint.lower():
        return "dash"
    if ext in VIDEO:
        return "video"
    if ext in AUDIO:
        return "audio"
    if ext in IMAGE:
        return "image"
    return hint.split("/")[0] if hint.split("/")[0] in {"video", "audio", "image"} else ""


def _ld_objects(soup: BeautifulSoup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(reversed(item))
            elif isinstance(item, dict):
                yield item
                for key in ("@graph", "video", "audio", "image", "associatedMedia", "hasPart", "mainEntity"):
                    if isinstance(item.get(key), (dict, list)):
                        stack.append(item[key])


def find_media(soup: BeautifulSoup, page_url: str) -> tuple[list[Media], list[str]]:
    """Every downloadable media URL on the page, in page order, and notes about what was left out."""
    found: dict[str, Media] = {}
    notes: list[str] = []

    def add(raw: str | None, origin: str, hint: str = "", *, require_kind: bool = False) -> None:
        if not raw:
            return
        raw = raw.strip()
        if raw.startswith("blob:"):
            notes.append("the player streams through blob: (Media Source, often DRM); not downloadable")
            return
        if raw.startswith("data:") or raw.startswith("javascript:"):
            return
        url = urljoin(page_url, raw)
        if not url.startswith(("http://", "https://")):
            return
        kind = kind_of(url, hint)
        if kind == "dash":
            notes.append(f"DASH stream {url[:120]} is not supported")
            return
        if require_kind and not kind:
            return
        if url not in found:
            found[url] = Media(url, kind or "video", origin)

    for obj in _ld_objects(soup):
        types = obj.get("@type")
        types = types if isinstance(types, list) else [types]
        hint = next((_LD_TYPES[t] for t in types if t in _LD_TYPES), None)
        if hint is None:
            continue
        content = obj.get("contentUrl")
        for value in content if isinstance(content, list) else [content]:
            if isinstance(value, str):
                add(value, "jsonld", f"{hint}/" if hint else str(obj.get("encodingFormat") or ""))

    for tag in soup.select("video, audio"):
        kind = tag.name
        for node in [tag, *tag.select("source")]:
            src = node.get("src") or node.get("data-src")
            add(src if isinstance(src, str) else None, "tag", str(node.get("type") or f"{kind}/"))

    for prop in ("og:video:secure_url", "og:video:url", "og:video", "og:audio:secure_url", "og:audio"):
        for meta in soup.select(f'meta[property="{prop}"], meta[name="{prop}"]'):
            hint = "audio/" if "audio" in prop else ""
            add(meta.get("content"), "og", hint, require_kind=True)

    for link in soup.select("a[href]"):
        href = link.get("href")
        if isinstance(href, str) and kind_of(urljoin(page_url, href)) in {"image", "video", "audio", "hls"}:
            add(href, "link")

    for img in soup.select("img"):
        container = img.find_parent(attrs={"class": _GALLERY}) or img.find_parent(attrs={"id": _GALLERY})
        if container is None:
            continue
        if img.find_parent("a", href=True) is not None and kind_of(urljoin(page_url, img.find_parent("a")["href"])) == "image":
            continue  # the link to the full-size image was taken above
        src = img.get("data-src") or img.get("data-original") or img.get("src")
        add(src if isinstance(src, str) else None, "gallery", "image/", require_kind=True)

    if not found:
        for meta in soup.select('meta[property="og:image"], meta[property="og:image:secure_url"]'):
            add(meta.get("content"), "og", "image/", require_kind=True)
            break

    return list(found.values()), list(dict.fromkeys(notes))


def page_title(soup: BeautifulSoup, fallback: str) -> str:
    meta = soup.select_one('meta[property="og:title"]')
    if meta is not None and str(meta.get("content") or "").strip():
        return str(meta["content"]).strip()[:150]
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(" ", strip=True)[:150]
    return fallback


def same_host_links(soup: BeautifulSoup, selector: str, page_url: str) -> list[str]:
    host = urlsplit(page_url).hostname
    links = []
    for tag in soup.select(selector):
        href = tag.get("href")
        if not isinstance(href, str):
            continue
        url = urljoin(page_url, href).split("#")[0]
        if urlsplit(url).hostname == host and url not in links:
            links.append(url)
    return links


@Crawler.db_path_builder("url")
@URLConfig(trim=False)
class GenericMediaCrawler(Crawler, is_generic=True):
    SUPPORTED_PATHS: ClassVar[dict[str, str | tuple[str, ...]]] = {
        "Media page": "/...",
        "Listing page (with an item selector)": "/...",
    }
    ITEM_SELECTOR: ClassVar[str] = ""
    NEXT_SELECTOR: ClassVar[str] = 'link[rel="next"], a[rel="next"]'

    async def fetch(self, scrape_item: ScrapeItem) -> None:
        await self.media_page(scrape_item)

    @error_handling_wrapper
    async def media_page(self, scrape_item: ScrapeItem) -> None:
        top = not scrape_item.parents  # the link the user pasted
        page_url = scrape_item.url
        seen_pages: set[str] = set()
        seen_media: set[str] = set()
        found = 0
        items = 0
        while page_url is not None and len(seen_pages) < MAX_PAGES:
            seen_pages.add(str(page_url))
            soup = await self.request_soup(page_url)
            if len(seen_pages) == 1 and top:
                scrape_item.setup_as_album(self.create_title(page_title(soup, page_url.path.strip("/") or self.DOMAIN)))
            media, notes = find_media(soup, str(page_url))
            for note in notes:
                _logger.warning("Generic media note\t%s\t%s", page_url, note)
            for entry in media:
                if entry.url in seen_media:
                    continue
                seen_media.add(entry.url)
                found += 1
                await self._save(scrape_item, entry, page_url)
            if not top:
                break
            if self.ITEM_SELECTOR:
                for link in same_host_links(soup, self.ITEM_SELECTOR, str(page_url)):
                    if link in seen_pages:
                        continue
                    items += 1
                    child = scrape_item.create_child(self.parse_url(link))
                    self.create_task(self.run(child))
                    scrape_item.add_children()
            next_links = [u for u in same_host_links(soup, self.NEXT_SELECTOR, str(page_url)) if u not in seen_pages]
            page_url = self.parse_url(next_links[0]) if next_links else None
        _logger.info("Generic media result\t%s\t%d pages, %d files, %d item pages", scrape_item.url, len(seen_pages), found, items)
        if top and not found and not items:
            raise ScrapeError(422, "No downloadable media on this page (no video, audio, image gallery or media links)")

    async def _save(self, scrape_item: ScrapeItem, entry: Media, page_url) -> None:
        url = self.parse_url(entry.url)
        if entry.kind == "hls":
            child = scrape_item.create_new(url)
            child.referer = page_url
            await self.generic_m3u8(child, url)
        else:
            default = {"image": ".jpg", "audio": ".mp3"}.get(entry.kind, ".mp4")
            with self.catch_errors(url):
                filename, ext = self.get_filename_and_ext(url.name or url.parent.name or "file", assume_ext=default)
                await self.handle_file(url, scrape_item, filename, ext, referer=page_url)
        scrape_item.add_children()
