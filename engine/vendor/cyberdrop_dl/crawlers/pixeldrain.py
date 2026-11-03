"""https://pixeldrain.com/api"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from typing import TYPE_CHECKING, Any, ClassVar, Literal, final, override

from cyberdrop_dl.clients.http import HTTPConfig
from cyberdrop_dl.crawlers.crawler import API, Crawler, DownloadConfig, SupportedDomains, SupportedPaths, auto_task_id
from cyberdrop_dl.exceptions import ScrapeError
from cyberdrop_dl.models import type_adapter
from cyberdrop_dl.url_objects import AbsoluteHttpURL
from cyberdrop_dl.utils import basic_auth
from cyberdrop_dl.utils.errors import error_handling_wrapper

if TYPE_CHECKING:
    from cyberdrop_dl.clients.response import AbstractResponse
    from cyberdrop_dl.url_objects import ScrapeItem


@final
@dataclasses.dataclass(slots=True)
class File:
    id: str
    name: str
    date_upload: str
    mime_type: str
    hash_sha256: str
    availability: str = ""  # NEXDROP patch: "file_rate_limited_captcha_required" etc.
    size: int = 0  # NEXDROP patch (3.2): checked against the free transfer limit


@dataclasses.dataclass(slots=True)
class Folder:
    id: str
    title: str
    files: list[File]


@final
@dataclasses.dataclass(slots=True)
class Node:
    type: Literal["file", "dir"]
    path: str
    name: str
    modified: str
    sha256_sum: str
    id: str | None = None
    file_type: str | None = None

    # Properties so we can process a Node as a normal File
    @property
    def mime_type(self) -> str:
        return self.file_type or ""

    @property
    def date_upload(self) -> str:
        return self.modified

    @property
    def hash_sha256(self) -> str:
        return self.sha256_sum


@dataclasses.dataclass(slots=True)
class FileSystem:
    children: list[Node]
    base_index: int
    path: list[Node]


class PixelDrainProxyCrawler(Crawler):
    SUPPORTED_PATHS: ClassVar[SupportedPaths] = {"File": "/<file_id>"}
    SUPPORTED_DOMAINS: ClassVar[SupportedDomains] = "pd.cybar.xyz", "pd.1drv.eu.org"
    PRIMARY_URL: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://pd.1drv.eu.org")
    DOMAIN: ClassVar[str] = "pixeldrain-proxy"

    @override
    async def fetch(self, scrape_item: ScrapeItem) -> None:
        match scrape_item.url.parts[1:]:
            case ["u", _]:
                self.handle_external_links(scrape_item)
            case _:
                raise ValueError

    @classmethod
    @override
    def transform_url(cls, url: AbsoluteHttpURL) -> AbsoluteHttpURL:
        url = super().transform_url(url).with_host("pixeldrain.com")
        match url.parts[1:]:
            case [file_id]:
                return url.origin() / "u" / file_id
            case _:
                return url


@HTTPConfig(rate_limit=(10, 1))
@DownloadConfig(slots=2)
class PixelDrainCrawler(Crawler):
    SUPPORTED_DOMAINS: ClassVar[SupportedDomains] = (
        "pixeldrain.com",
        "pixeldrain.net",
        "pixeldra.in",
        "pixeldrain.nl",
        "pixeldrain.biz",
        "pixeldrain.tech",
        "pixeldrain.dev",
    )
    SUPPORTED_PATHS: ClassVar[SupportedPaths] = {
        "File": (
            "/u/<file_id>",
            "/l/<list_id>#item=<file_index>",
            "/api/file/<file_id>",
        ),
        "Folder": (
            "/l/<list_id>",
            "/api/list/<list_id>",
        ),
        "Filesystem": (
            "/d/<id>",
            "/api/filesystem/<path>...",
        ),
        "**NOTE**": "text files will not be downloaded but their content will be parsed for URLs",
    }
    PRIMARY_URL: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://pixeldrain.com")
    DOMAIN: ClassVar[str] = "pixeldrain"
    FOLDER_DOMAIN: ClassVar[str] = "PixelDrain"

    def __post_init__(self) -> None:
        self.api: PixelDrainAPI = PixelDrainAPI.from_crawler(self)
        if self.api.logged_in:
            self.downloader.slots = None

    @override
    def __json_resp_check__(self, json_resp: dict[str, Any], resp: AbstractResponse[Any]) -> None:
        if not json_resp["success"]:
            msg = f"{json_resp['message']} ({json_resp['value']})"
            raise ScrapeError(resp.status, msg)

    @override
    def _prepare_headers(self, scrape_item: ScrapeItem) -> dict[str, str]:
        return super()._prepare_headers(scrape_item) | self.api.headers

    @override
    async def fetch(self, scrape_item: ScrapeItem) -> None:
        if self.origin.host not in self.SUPPORTED_DOMAINS:
            raise ValueError
        match scrape_item.url.parts[1:]:
            case ["u", file_id]:
                return await self.file(scrape_item, file_id)
            case ["l", folder_id]:
                return await self.folder(scrape_item, folder_id)
            case ["d", *path] if path:
                return await self.filesystem(scrape_item, "/".join(path))
            case _:
                raise ValueError

    @classmethod
    @override
    def transform_url(cls, url: AbsoluteHttpURL) -> AbsoluteHttpURL:
        url = super().transform_url(url)
        match url.parts[1:]:
            case ["api", "file", file_id]:
                return url.origin() / "u" / file_id
            case ["api", "list", list_id]:
                return url.origin() / "l" / list_id
            case ["api", "filesystem", *rest] if rest:
                return (url.origin() / "d").joinpath(*rest)
            case _:
                return url

    @error_handling_wrapper
    async def folder(self, scrape_item: ScrapeItem, list_id: str) -> None:
        folder = await self.api.folder(list_id)
        title = self.create_title(folder.title, list_id)
        scrape_item.setup_as_album(title, album_id=list_id)

        try:
            files = _filter_files(folder.files, scrape_item.url.fragment)
        except (ValueError, IndexError):
            msg = f"Unable to parse item index in {scrape_item.url}. Falling back to downloading the entire folder"
            self.log.warning(msg)
            files = folder.files

        await self._files(scrape_item, files)

    async def _files(self, scrape_item: ScrapeItem, files: list[File]) -> None:
        assert scrape_item.album_id
        results = await self.get_album_results(scrape_item.album_id)
        for file in files:
            if self.check_album_results(self.api.download(file), results):
                continue

            url = self.origin / "u" / file.id
            new_scrape_item = scrape_item.create_child(url)
            self.create_task(self._file_task(new_scrape_item, file))
            scrape_item.add_children()

    @error_handling_wrapper
    async def filesystem(self, scrape_item: ScrapeItem, path: str) -> None:
        # https://github.com/Fornaxian/pixeldrain_web/blob/8e5ecfc5ce44c0b2b4fafdf9e8201dfc98395e88/svelte/src/filesystem/FilesystemAPI.ts
        fs = await self.api.filesystem(path)
        base_node = fs.path[fs.base_index]
        root = fs.path[0]
        assert root.id
        title = self.create_title(root.name, root.id)
        scrape_item.setup_as_album(title, album_id=root.id)

        if base_node.type == "file":
            fs.children = [base_node]

        await self._filesystem(scrape_item, fs)

    async def _filesystem(self, scrape_item: ScrapeItem, fs: FileSystem) -> None:
        assert scrape_item.album_id
        results = await self.get_album_results(scrape_item.album_id)

        async def subfolder(new_item: ScrapeItem, path: str) -> None:
            with self.catch_errors(new_item):
                fs = await self.api.filesystem(path)
                scrape_item.add_children(0)
                walk_filesystem(fs)

        def walk_filesystem(fs: FileSystem) -> None:
            for node in fs.children:
                if node.name == ".search_index.gz":
                    continue

                url = self.origin / "d" / node.path.removeprefix("/")
                new_scrape_item = scrape_item.create_child(url)

                if node.type == "file":
                    if self.check_album_results(self.api.download(node), results):
                        continue

                    subfolders = node.path.split("/")[2:-1]
                    new_scrape_item.append_folders(*subfolders)
                    tg.create_task(self._file_task(new_scrape_item, node))

                elif node.type == "dir":
                    tg.create_task(subfolder(new_scrape_item, node.path))

                else:
                    self.raise_exc(new_scrape_item, f"Unknown node type: {node.type}")

                scrape_item.add_children()

        async with asyncio.TaskGroup() as tg:
            walk_filesystem(fs)

    @error_handling_wrapper
    async def file(self, scrape_item: ScrapeItem, file_id: str) -> None:
        if await self.check_complete_from_referer(scrape_item.url):
            return

        file = await self.api.file(file_id)
        await self._file(scrape_item, file)

    async def _file(self, scrape_item: ScrapeItem, file: File | Node) -> None:
        src = self.api.download(file).with_host(self.origin.host)

        # NEXDROP patch: Pixeldrain locks popular files behind a captcha for free downloads.
        # Say so up front instead of downloading into a 403 and retrying it.
        if type(file) is File and file.availability.endswith("captcha_required") and not self.api.logged_in:
            raise ScrapeError(
                403,
                "Pixeldrain wants a captcha for this file. Open the link in a browser to download it, "
                "or add a Pixeldrain API key from a paid account in Settings > Sign-ins",
            )

        if "text/plain" in file.mime_type:
            scrape_item.setup_as_album(self.create_title(file.name, file.id))
            text = await self.api.text(src)
            return self._text(scrape_item, text)

        if await self.check_complete_by_hash(src, "sha256", file.hash_sha256):
            return None

        # NEXDROP patch (3.2): without an API key Pixeldrain allows a free amount of transfer per day
        # (6 GB). A file that no longer fits fails now with the reason, instead of after a long download.
        if type(file) is File and file.size and not self.api.logged_in:
            left = await self.api.transfer_left()
            if left is not None and file.size > left:
                raise ScrapeError(
                    509,
                    f"Pixeldrain's free daily bandwidth limit is used up ({_gb(left)} left, this file is "
                    f"{_gb(file.size)}). Try again later, or add a Pixeldrain API key in Settings > Sign-ins",
                )
            if left is not None:
                self.api.reserve(file.size)

        filename, ext = self.get_filename_and_ext(file.name, mime_type=file.mime_type)
        scrape_item.uploaded_at = self.parse_iso_date(file.date_upload)
        await self.handle_file(
            src,
            scrape_item,
            file.name,
            ext,
            custom_filename=filename,
            thumbnail=src.with_query("thumbnail") if type(file) is Node else src / "thumbnail",
        )

    def _text(self, scrape_item: ScrapeItem, text: str) -> None:
        for line in text.splitlines():
            try:
                link = self.parse_url(line)
            except Exception:  # noqa: BLE001, S112
                continue
            new_item = scrape_item.create_child(link)
            self.handle_external_links(new_item)
            scrape_item.add_children()

    _file_task = auto_task_id(error_handling_wrapper(_file))


class PixelDrainAPI(API):
    def __post_init__(self) -> None:
        self.headers: dict[str, str] = {}
        self._quota: tuple[float, int | None, int] | None = None
        if api_key := self.config.auth.pixeldrain.api_key:
            self.headers["Authorization"] = basic_auth("Cyberdrop-DL", api_key)

    @property
    def logged_in(self) -> bool:
        return bool(self.headers)

    async def transfer_left(self) -> int | None:
        """NEXDROP patch (3.2): bytes left of the free transfer limit, from /api/misc/rate_limits.

        Asked at most once a minute; None when Pixeldrain does not say (the download then just runs)."""
        now = time.monotonic()
        cached = self._quota
        if cached is not None and now - cached[0] < 60:
            left, reserved = cached[1], cached[2]
            return None if left is None else max(0, left - reserved)
        try:
            data = json.loads(await self.text(self.origin / "api/misc/rate_limits"))
            left = int(data["transfer_limit"]) - int(data["transfer_limit_used"]) if data.get("transfer_limit") else None
        except Exception:  # noqa: BLE001
            left = None
        self._quota = (now, left, 0)
        return left

    def reserve(self, n_bytes: int) -> None:
        """Count a file that is about to download against the cached limit, so a batch is not all let through."""
        cached = self._quota
        if cached is not None and cached[1] is not None:
            self._quota = (cached[0], cached[1], cached[2] + n_bytes)

    async def file(self, file_id: str) -> File:
        api_url = self.origin / "api/file" / file_id / "info"
        resp = await self.text(api_url)
        return type_adapter(File).validate_json(resp)

    async def folder(self, list_id: str) -> Folder:
        api_url = self.origin / "api/list" / list_id
        resp = await self.text(api_url)
        return type_adapter(Folder).validate_json(resp)

    async def filesystem(self, path: str) -> FileSystem:
        api_url = (self.origin / "api/filesystem" / path.removeprefix("/")).with_query("stat")
        resp = await self.text(api_url)
        return type_adapter(FileSystem).validate_json(resp)

    async def text(self, api_url: AbsoluteHttpURL) -> str:
        return await self.request_text(api_url, headers=self.headers)

    def download(self, file: File | Node) -> AbsoluteHttpURL:
        if type(file) is File:
            return (self.origin / "api/file" / file.id).with_query("download")
        return (self.origin / "api/filesystem" / file.path.removeprefix("/")).with_query("attach")


def _gb(n_bytes: int) -> str:
    return f"{n_bytes / 1_000_000_000:.2f} GB"


def _filter_files(files: list[File], fragment: str) -> list[File]:
    if fragment.startswith(prefix := "item="):
        item_idx = int(fragment.removeprefix(prefix))
        return [files[item_idx]]
    return files
