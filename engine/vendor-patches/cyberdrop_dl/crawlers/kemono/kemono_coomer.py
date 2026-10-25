"""NEXDROP patch: Kemono (kemono.cr) and Coomer (coomer.st).

Cyberdrop-DL 10.10.0 ships the shared Kemono code but no crawler for these two sites, so creator,
post and search links were logged as "Unsupported URL". Both sites answer their API only to
requests that send `Accept: text/css`; any other Accept header gets 403. Both rate limit hard,
so API calls stay at 2 a second.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from cyberdrop_dl.clients.http import HTTPConfig
from cyberdrop_dl.config.crawlers import KemonoConfig
from cyberdrop_dl.crawlers.crawler import DownloadConfig
from cyberdrop_dl.crawlers.kemono.api import KemonoAPI
from cyberdrop_dl.crawlers.kemono.kemono import KemonoBaseCrawler
from cyberdrop_dl.crawlers.kemono.models import UserPostModel
from cyberdrop_dl.url_objects import AbsoluteHttpURL

if TYPE_CHECKING:
    from cyberdrop_dl.crawlers.crawler import SupportedPaths

_API_HEADERS = {"Accept": "text/css"}


@HTTPConfig(headers=_API_HEADERS, rate_limit=(2, 1))
class KemonoPartyAPI(KemonoAPI[UserPostModel]):
    ENTRYPOINT: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://kemono.cr/api/v1")
    CDN: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://kemono.cr")


@HTTPConfig(headers=_API_HEADERS, rate_limit=(2, 1))
class CoomerAPI(KemonoAPI[UserPostModel]):
    ENTRYPOINT: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://coomer.st/api/v1")
    CDN: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://coomer.st")


class _KemonoSiteCrawler[T: KemonoAPI[UserPostModel]](KemonoBaseCrawler[T], is_abc=True):
    SUPPORTED_PATHS: ClassVar[SupportedPaths] = KemonoBaseCrawler.SUPPORTED_PATHS | {
        "Search": "/posts?q=...",
    }

    @property
    @override
    def __kemono_config__(self) -> KemonoConfig:
        # The engine config has no kemono/coomer section; use the same defaults as Pawchive.
        return getattr(self.config.crawlers, self.DOMAIN, None) or _DEFAULT_CONFIG

    @classmethod
    @override
    def transform_url(cls, url: AbsoluteHttpURL) -> AbsoluteHttpURL:
        # Old hosts and their file servers (c1.coomer.party ...) no longer resolve. The main
        # domain serves the same /data/... paths and redirects to the right file server.
        host = url.host.removeprefix("www.")
        if any(host == old or host.endswith("." + old) for old in cls.OLD_DOMAINS):
            return url.with_host(cls.PRIMARY_URL.host)
        return url

    @override
    async def __async_post_init__(self) -> None:
        # The full creator list is tens of MB on Coomer. Names are looked up per creator instead.
        return


_DEFAULT_CONFIG = KemonoConfig()


@HTTPConfig(rate_limit=(2, 1))
@DownloadConfig(slots=4)
class KemonoCrawler(_KemonoSiteCrawler[KemonoPartyAPI]):
    PRIMARY_URL: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://kemono.cr")
    DOMAIN: ClassVar[str] = "kemono"
    FOLDER_DOMAIN: ClassVar[str] = "Kemono"
    OLD_DOMAINS: ClassVar[tuple[str, ...]] = ("kemono.su", "kemono.party")

    def __post_init__(self) -> None:
        self.api: KemonoPartyAPI = KemonoPartyAPI.from_crawler(self)


@HTTPConfig(rate_limit=(2, 1))
@DownloadConfig(slots=4)
class CoomerCrawler(_KemonoSiteCrawler[CoomerAPI]):
    PRIMARY_URL: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://coomer.st")
    DOMAIN: ClassVar[str] = "coomer"
    FOLDER_DOMAIN: ClassVar[str] = "Coomer"
    OLD_DOMAINS: ClassVar[tuple[str, ...]] = ("coomer.su", "coomer.party")

    def __post_init__(self) -> None:
        self.api: CoomerAPI = CoomerAPI.from_crawler(self)
