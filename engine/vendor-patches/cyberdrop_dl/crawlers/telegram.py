"""Public Telegram channels through t.me's web preview (no account needed).

NEXDROP addition. Telegram shows the newest posts of every public channel at
https://t.me/s/<channel>. That page carries direct CDN links for photos,
videos, round videos and voice notes. Files over Telegram's preview limit
and documents (zip, pdf, ...) are not on the page; NEXDROP reports those as
skipped with a pointer to the Telegram sign-in, which downloads everything.

Supported links:
    t.me/<channel>              whole channel, oldest post first
    t.me/s/<channel>            same
    t.me/<channel>/<post>       one post (all files of an album)
    t.me/<channel>/<post>?single  only that one file of an album
    telegram.me / telegram.dog  same as t.me

Private links (t.me/c/..., t.me/+invite, t.me/joinchat/...) need the sign-in.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import TYPE_CHECKING, ClassVar

from cyberdrop_dl.clients.http import HTTPConfig
from cyberdrop_dl.crawlers.crawler import Crawler, SupportedDomains, SupportedPaths
from cyberdrop_dl.exceptions import ScrapeError
from cyberdrop_dl.url_objects import AbsoluteHttpURL
from cyberdrop_dl.utils.errors import error_handling_wrapper

if TYPE_CHECKING:
    from bs4 import BeautifulSoup, Tag

    from cyberdrop_dl.url_objects import ScrapeItem

_BG_URL = re.compile(r"background-image:\s*url\(['\"]?([^'\")]+)['\"]?\)")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
_RESERVED = frozenset({"s", "c", "joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "iv", "login", "setlanguage", "contact", "boost", "addlist", "m", "i", "k", "z", "a"})
SIGN_IN_HINT = "needs the Telegram sign-in (Settings > Sign-ins)"
_logger = logging.getLogger("cyberdrop_dl")


@dataclasses.dataclass(slots=True)
class Media:
    post_id: int  # the message that holds this file (album items have their own id)
    kind: str  # photo, video, round, voice, missing
    url: str = ""
    reason: str = ""


@dataclasses.dataclass(slots=True)
class Post:
    channel: str
    post_id: int
    date: str = ""
    media: list[Media] = dataclasses.field(default_factory=list)


def _style_url(tag: Tag | None) -> str:
    if tag is None:
        return ""
    match = _BG_URL.search(str(tag.get("style") or ""))
    return match.group(1) if match else ""


def _href_id(tag: Tag, fallback: int) -> int:
    href = str(tag.get("href") or "")
    match = re.search(r"/(\d+)(?:\?|$)", href)
    return int(match.group(1)) if match else fallback


def parse_posts(soup: BeautifulSoup) -> list[Post]:
    """Every message on a t.me/s page or a t.me/<c>/<id>?embed=1 page, in page order."""
    posts: list[Post] = []
    for message in soup.select("div.tgme_widget_message[data-post]"):
        channel, _, number = str(message["data-post"]).rpartition("/")
        if not number.isdigit():
            continue
        post = Post(channel=channel, post_id=int(number))
        time_tag = message.select_one("a.tgme_widget_message_date time[datetime]") or message.select_one("time[datetime]")
        if time_tag is not None:
            post.date = str(time_tag["datetime"])

        for node in message.select(
            "a.tgme_widget_message_photo_wrap, a.tgme_widget_message_video_player, "
            "div.tgme_widget_message_roundvideo_player, div.tgme_widget_message_roundvideo_wrap, "
            "audio.tgme_widget_message_voice, a.tgme_widget_message_document_wrap"
            # Every message also carries a hidden "Please open Telegram to view this post" box,
            # and every player a hidden "not supported in your browser" box: neither means anything.
        ):
            classes = set(node.get("class") or [])
            own_id = _href_id(node, post.post_id)
            if "tgme_widget_message_photo_wrap" in classes:
                url = _style_url(node)
                if url:
                    post.media.append(Media(own_id, "photo", url))
            elif "tgme_widget_message_video_player" in classes:
                video = node.select_one("video[src]")
                if video is not None and "not_supported" not in classes:
                    post.media.append(Media(own_id, "video", str(video["src"])))
                else:
                    post.media.append(Media(own_id, "missing", reason="video too large for the public preview"))
            elif classes & {"tgme_widget_message_roundvideo_player", "tgme_widget_message_roundvideo_wrap"}:
                video = node.select_one("video[src]")
                if video is not None and not any(m.url == video["src"] for m in post.media):
                    post.media.append(Media(own_id, "round", str(video["src"])))
            elif "tgme_widget_message_voice" in classes:
                if node.get("src"):
                    post.media.append(Media(own_id, "voice", str(node["src"])))
            elif "tgme_widget_message_document_wrap" in classes:
                title = node.select_one(".tgme_widget_message_document_title")
                name = title.get_text(" ", strip=True) if title is not None else "file"
                post.media.append(Media(own_id, "missing", reason=f"document '{name[:80]}' is not in the public preview"))
        posts.append(post)
    return posts


def media_ext(name: str, default: str) -> str:
    _, dot, ext = name.rpartition(".")
    ext = ext.lower()
    return f".{ext}" if dot and ext.isalnum() and 2 <= len(ext) <= 5 else default


def older_page(soup: BeautifulSoup) -> str | None:
    more = soup.select_one("a.tme_messages_more[data-before]")
    return str(more["href"]) if more is not None and more.get("href") else None


def channel_title(soup: BeautifulSoup, fallback: str) -> str:
    for selector in (".tgme_channel_info_header_title", ".tgme_widget_message_owner_name", ".tgme_page_title"):
        tag = soup.select_one(selector)
        if tag is not None and tag.get_text(strip=True):
            return tag.get_text(" ", strip=True)
    return fallback


def classify(url: AbsoluteHttpURL) -> tuple[str, str, int | None]:
    """('channel'|'post'|'private'|'other', channel, post id)"""
    parts = [p for p in url.parts[1:] if p]
    if parts and parts[0] == "s":
        parts = parts[1:]
        if len(parts) == 1 and _NAME_RE.match(parts[0]):
            return "channel", parts[0], None
        if len(parts) == 2 and _NAME_RE.match(parts[0]) and parts[1].isdigit():
            return "post", parts[0], int(parts[1])
        return "other", "", None
    if not parts:
        return "other", "", None
    first = parts[0]
    if first in {"c", "joinchat"} or first.startswith("+"):
        return "private", "", None
    if first.lower() in _RESERVED or not _NAME_RE.match(first):
        return "other", "", None
    if len(parts) == 1:
        return "channel", first, None
    if parts[1].isdigit():
        return "post", first, int(parts[1])
    return "other", first, None


@HTTPConfig(rate_limit=(3, 1))
class TelegramCrawler(Crawler):
    SUPPORTED_DOMAINS: ClassVar[SupportedDomains] = ("t.me", "telegram.me", "telegram.dog")
    SUPPORTED_PATHS: ClassVar[SupportedPaths] = {
        "Channel": ("/<channel>", "/s/<channel>"),
        "Post": "/<channel>/<post_id>",
    }
    PRIMARY_URL: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://t.me")
    DOMAIN: ClassVar[str] = "telegram"
    FOLDER_DOMAIN: ClassVar[str] = "Telegram"

    @classmethod
    def check_host_match(cls, host: str) -> bool:
        # The scrape mapper matches by substring ("t.me" is inside "art.media.com").
        return host.removeprefix("www.") in {"t.me", "telegram.me", "telegram.dog"}

    async def fetch(self, scrape_item: ScrapeItem) -> None:
        kind, channel, post_id = classify(scrape_item.url)
        if kind == "private":
            raise ScrapeError(401, f"Private Telegram link, {SIGN_IN_HINT}")
        if kind == "channel":
            if scrape_item.parents:
                # A channel advertised inside a forum post or album: never pull a whole channel from a link found on a page.
                _logger.info("Telegram channel link found on %s skipped: paste t.me/%s to download the channel", scrape_item.parents[-1], channel)
                return
            return await self.channel(scrape_item, channel)
        if kind == "post" and post_id is not None:
            return await self.post(scrape_item, channel, post_id)
        raise ValueError

    @error_handling_wrapper
    async def channel(self, scrape_item: ScrapeItem, channel: str) -> None:
        start = self.PRIMARY_URL / "s" / channel
        if before := scrape_item.url.query.get("before"):
            start = start.with_query(before=before)
        collected: dict[int, Post] = {}
        title = channel
        first = True
        async for soup in self.web_pager(start, older_page):
            if first:
                title = channel_title(soup, channel)
                first = False
            for post in parse_posts(soup):
                collected.setdefault(post.post_id, post)
        if not collected:
            raise ScrapeError(404, f"t.me/{channel} has no public preview (a private channel, a group, a bot or a user); it {SIGN_IN_HINT}")
        _logger.info("Telegram result\t%s\t%d posts", channel, len(collected))
        scrape_item.setup_as_profile(self.create_title(title, channel))
        for post_id in sorted(collected):  # oldest first, so numbering follows the channel
            await self._post_files(scrape_item, collected[post_id])

    @error_handling_wrapper
    async def post(self, scrape_item: ScrapeItem, channel: str, post_id: int) -> None:
        single = "single" in scrape_item.url.query
        page = (self.PRIMARY_URL / channel / str(post_id)).with_query({"embed": "1", "mode": "tme", **({"single": ""} if single else {})})
        soup = await self.request_soup(page)
        posts = parse_posts(soup)
        posts = [p for p in posts if p.post_id == post_id or any(m.post_id == post_id for m in p.media)] or posts[:1]
        if not posts:
            error = soup.select_one(".tgme_widget_message_error")
            if error is not None and error.get_text(strip=True):
                raise ScrapeError(404, f"t.me/{channel}/{post_id}: {error.get_text(' ', strip=True)}")
            raise ScrapeError(404, f"Telegram post t.me/{channel}/{post_id} is not public; it {SIGN_IN_HINT}")
        if not scrape_item.parents:
            scrape_item.setup_as_album(self.create_title(channel_title(soup, channel), channel))
        for post in posts:
            if single:
                post.media = [m for m in post.media if m.post_id == post_id] or post.media[:1]
            await self._post_files(scrape_item, post)

    async def _post_files(self, scrape_item: ScrapeItem, post: Post) -> None:
        shared = [m.post_id for m in post.media]
        for index, media in enumerate(post.media, 1):
            link = self.PRIMARY_URL / post.channel / str(media.post_id)
            if media.kind == "missing":
                _logger.warning("Telegram needs sign-in\t%s\t%s", link, media.reason)
                continue
            # Telegram's CDN links change every visit; history is kept per message instead.
            # Album items have their own message id; a message holding several files gets #1, #2...
            referer = link.with_fragment(str(index)) if shared.count(media.post_id) > 1 else link
            if await self.check_complete_from_referer(referer):
                continue
            url = self.parse_url(media.url)
            default_ext = {"photo": ".jpg", "voice": ".ogg"}.get(media.kind, ".mp4")
            ext = media_ext(url.name, default_ext)
            name = f"{post.channel}_{media.post_id}" + (f"_{index}" if referer.fragment else "") + ext
            item = scrape_item.create_new(link, add_parent=scrape_item.url)
            if post.date:
                try:
                    item.uploaded_at = self.parse_iso_date(post.date)
                except Exception:  # noqa: BLE001 - a date is only used for file times
                    pass
            await self.handle_file(url, item, name, ext, custom_filename=name, referer=referer)
            scrape_item.add_children()
