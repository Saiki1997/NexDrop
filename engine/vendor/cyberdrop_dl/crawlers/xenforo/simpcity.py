from __future__ import annotations

from typing import ClassVar

from cyberdrop_dl.clients.http import HTTPConfig
from cyberdrop_dl.url_objects import AbsoluteHttpURL

from .xenforo import XenforoCrawler


# is_debug=False: 10.10.0 ships this crawler disabled unless
# CDL_ENABLE_DEBUG_CRAWLERS is an exact token. This build always registers it.
@HTTPConfig(rate_limit=(1, 20))
class SimpCityCrawler(XenforoCrawler, is_debug=False):
    PRIMARY_URL: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://simpcity.cr")
    DOMAIN: ClassVar[str] = "simpcity"
    FOLDER_DOMAIN: ClassVar[str] = "SimpCity"
    LOGIN_USER_COOKIE_NAME: ClassVar[str] = "yMziCv8BrCZz1o7_user"
    login_required: bool = False
    IGNORE_EMBEDED_IMAGES_SRC: ClassVar[bool] = False
    OLD_DOMAINS: ClassVar[tuple[str, ...]] = ("simpcity.su",)
