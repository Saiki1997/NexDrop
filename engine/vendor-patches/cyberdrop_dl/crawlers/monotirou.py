# NEXDROP patch: new crawler for Monotirou profiles, videos, searches and tags (see engine/vendor-patches/README.md).
from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar

from cyberdrop_dl.crawlers.crawler import Crawler, SupportedPaths
from cyberdrop_dl.exceptions import ScrapeError
from cyberdrop_dl.url_objects import AbsoluteHttpURL
from cyberdrop_dl.utils.errors import error_handling_wrapper

if TYPE_CHECKING:
    from cyberdrop_dl.url_objects import ScrapeItem


class MonotirouCrawler(Crawler):
    SUPPORTED_PATHS: ClassVar[SupportedPaths] = {
        "Video": "/video/<video_id>",
        "Profile": "/users/<username>",
        "Search": "/?q=<query>",
        "Tag": "/tags/<tag>",
    }
    PRIMARY_URL: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://monotirou.com")
    DOMAIN: ClassVar[str] = "monotirou"
    FOLDER_DOMAIN: ClassVar[str] = "Monotirou"

    API_USER_VIDEOS = "https://api.monotirou.com/api/users/{username}/videos"
    API_VIDEO = "https://api.monotirou.com/api/videos/{public_id}"
    API_SEARCH = "https://api.monotirou.com/api/videos/search"

    async def fetch(self, scrape_item: ScrapeItem) -> None:
        # Absolute URL parts begin with '/', not an empty string.
        parts = list(scrape_item.url.parts[1:])
        if parts and parts[-1] == "":
            parts.pop()
        match parts:
            case ["video", video_id]:
                await self.video(scrape_item, video_id)
            case ["users", username]:
                await self.profile(scrape_item, username)
            case [] | ["search"] if scrape_item.url.query.get("q", "").strip():
                query = scrape_item.url.query["q"].strip()
                await self.search(scrape_item, query, f"{query} [search]")
            case ["tags", tag]:
                # The site's tag page lists the search results for the tag name.
                await self.search(scrape_item, tag, f"{tag} [tag]")
            case _:
                raise ValueError(f"Unsupported Monotirou path: {scrape_item.url}")

    @error_handling_wrapper
    async def video(self, scrape_item: ScrapeItem, video_id: str) -> None:
        api_url = AbsoluteHttpURL(self.API_VIDEO.format(public_id=video_id))
        data = await self.request_json(api_url)
        item = data.get("data") if isinstance(data, dict) else None
        if not isinstance(item, dict):
            raise ScrapeError(422, message="Monotirou returned invalid video metadata")

        # Use the supplied media location, never a guessed numeric CDN filename.
        public_id = item.get("public_id")
        media = item.get("video_url")
        if not public_id or not isinstance(media, str) or not media:
            raise ScrapeError(422, message="Monotirou video metadata has no public ID or media URL")
        try:
            media_url = AbsoluteHttpURL(media)
            if media_url.scheme not in {"http", "https"} or not media_url.host:
                raise ValueError("Expected an absolute HTTP media URL")
        except (TypeError, ValueError) as exc:
            raise ScrapeError(422, message="Monotirou returned an invalid media URL") from exc

        scrape_item.setup_as_album(item.get("title") or str(public_id), album_id=str(public_id))
        self.create_eager_task(self.direct_file(scrape_item, media_url))
        scrape_item.add_children()

    @error_handling_wrapper
    async def profile(self, scrape_item: ScrapeItem, username: str) -> None:
        scrape_item.setup_as_profile(self.create_title(username))
        limit = 24
        cursor = None
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        base_url = AbsoluteHttpURL(self.API_USER_VIDEOS.format(username=username))

        while True:
            query = {"limit": str(limit), "sort": "newest"}
            if cursor is not None:
                query["cursor"] = cursor
            data = await self.request_json(base_url.with_query(query))
            videos = data.get("data") if isinstance(data, dict) else None
            if not isinstance(videos, list) or any(not isinstance(item, dict) for item in videos):
                raise ScrapeError(422, message="Monotirou returned an invalid video listing")
            if not videos:
                return

            new_ids = 0
            for item in videos:
                public_id = item.get("public_id")
                if not public_id:
                    raise ScrapeError(422, message="Monotirou listing is missing a public video ID")
                public_id = str(public_id)
                if public_id in seen_ids:
                    continue
                seen_ids.add(public_id)
                new_ids += 1
                new_item = scrape_item.create_child(self.PRIMARY_URL / "video" / public_id)
                self.create_task(self.run(new_item))

            if not new_ids:
                raise ScrapeError(422, message="Monotirou pagination repeated a page; stopped to avoid an endless loop")
            if len(videos) < limit:
                return

            # Profiles use a JSON keyset cursor, not offset or page.
            last = videos[-1]
            if not last.get("created_at") or last.get("id") is None:
                raise ScrapeError(422, message="Monotirou listing is missing pagination metadata")
            cursor = json.dumps({"v": last["created_at"], "id": last["id"]}, separators=(",", ":"))
            if cursor in seen_cursors:
                raise ScrapeError(422, message="Monotirou pagination cursor did not advance")
            seen_cursors.add(cursor)

    @error_handling_wrapper
    async def search(self, scrape_item: ScrapeItem, query: str, title: str) -> None:
        scrape_item.setup_as_profile(self.create_title(title))
        # The API returns at most 50 per page; a short page is the last one.
        limit = 48
        offset = 0
        seen_ids: set[str] = set()

        while True:
            api_url = AbsoluteHttpURL(self.API_SEARCH).with_query({"q": query, "limit": str(limit), "offset": str(offset)})
            data = await self.request_json(api_url)
            videos = data.get("data") if isinstance(data, dict) else None
            if not isinstance(videos, list) or any(not isinstance(item, dict) for item in videos):
                raise ScrapeError(422, message="Monotirou returned an invalid search listing")
            if not videos:
                return

            new_ids = 0
            for item in videos:
                public_id = item.get("public_id")
                if not public_id:
                    raise ScrapeError(422, message="Monotirou listing is missing a public video ID")
                public_id = str(public_id)
                if public_id in seen_ids:
                    continue
                seen_ids.add(public_id)
                new_ids += 1
                new_item = scrape_item.create_child(self.PRIMARY_URL / "video" / public_id)
                self.create_task(self.run(new_item))

            if not new_ids:
                raise ScrapeError(422, message="Monotirou pagination repeated a page; stopped to avoid an endless loop")
            if len(videos) < limit:
                return
            offset += len(videos)
