# Frozen copy of the 2.5 (V1) smart naming engine, kept only to measure V2 against it.
"""NEXDROP smart naming: one place that turns crawl metadata into folder and file names.

Crawlers only describe what they found (album, thread, post, caption, page title). This
module decides the names, so every site handler, and every future one, names files the
same way. Nothing here sends a request: page titles come from pages the crawler already
fetched (see `note_page`).

Settings come from the bridge through `SETTINGS`. The default is the old behaviour, so
code and tests that never set it keep their names.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import unicodedata
from collections import OrderedDict
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from cyberdrop_dl import source_order

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

logger = logging.getLogger("cyberdrop_dl")

STRATEGIES = ("auto", "original", "media", "post", "album")
NUMBERING = ("suffix", "prefix", "off")
FOLDER_MODES = ("auto", "album", "post", "original")


@dataclasses.dataclass(frozen=True, slots=True)
class NamingSettings:
    smart: bool = False  # Enable Smart Filename Detection
    strategy: str = "auto"  # Filename Strategy
    numbering: str = "prefix"  # Sequence Numbering
    folders: str = "original"  # Folder Naming
    site_in_folder: bool = True  # Include Website Name in Folder
    replace_generic: bool = False  # Replace Generic Folder Names
    keep_meaningful: bool = True  # Preserve Meaningful Original Filenames
    max_len: int = 120  # Maximum Filename Length, without the extension

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> NamingSettings:
        """The app's settings, with the 2.5 defaults for anything missing."""

        def pick(key: str, allowed: tuple[str, ...], default: str) -> str:
            value = str(payload.get(key) or default).lower()
            return value if value in allowed else default

        def flag(key: str, default: bool) -> bool:
            value = payload.get(key)
            return default if value is None else bool(value)

        try:
            max_len = int(payload.get("maxNameLength") or 120)
        except (TypeError, ValueError):
            max_len = 120
        numbering = pick("numbering", NUMBERING, "suffix")
        if payload.get("numbering") is None and payload.get("sequencePrefix") is False:
            numbering = "off"  # "Numbered filenames" was turned off before 2.5
        return cls(
            smart=flag("smartNames", True),
            strategy=pick("nameStrategy", STRATEGIES, "auto"),
            numbering=numbering,
            folders=pick("folderNaming", FOLDER_MODES, "auto"),
            site_in_folder=flag("siteInFolder", False),
            replace_generic=flag("replaceGenericFolders", False),
            keep_meaningful=flag("keepMeaningfulNames", True),
            max_len=max(20, min(200, max_len)),
        )

    def fingerprint(self) -> str:
        """Names kept from an earlier run are only reused while the settings that made them match."""
        return ".".join(
            str(v) for v in (int(self.smart), self.strategy, self.numbering, int(self.keep_meaningful), self.max_len)
        )

    def describe(self) -> str:
        names = f"smart names ({self.strategy})" if self.smart else "original names"
        number = {"suffix": "number at the end", "prefix": "number in front", "off": "no numbers"}[self.numbering]
        folders = {"auto": "content-title folders", "album": "album folders", "post": "post/thread folders",
                   "original": "original folders"}[self.folders]
        site = ", site name in folders" if self.site_in_folder else ""
        return f"{names}, {number}, {folders}{site}"


SETTINGS: ContextVar[NamingSettings] = ContextVar("NEXDROP_NAMING", default=NamingSettings())


# ─── what a name is worth ────────────────────────────────────────────────────

_GENERIC_WORDS = frozenset(
    """img image images dsc dscn dscf dcim pxl vid video videos mov movie photo photos pic pics picture
    pictures screenshot screen shot capture snapshot file files download downloads untitled unnamed
    unknown noname media clip audio track document doc scan tmp temp copy new default original
    thumb thumbnail preview mmexport wp whatsapp signal telegram camera cam gopr gp att attachment
    upload uploaded post content export output frame part sample test img_ vid_ photo_ hd sd hq uhd
    fhd 4k 1080p 720p 480p 360p 2160p full final edit edited image0 out""".split()
)
_JUNK_TITLES = frozenset(
    """pics pic video videos vids more here link links enjoy thanks thank you thx ty source sauce
    credit credits update updates new part bump repost reupload mirror mirrors album gallery set
    sets download downloads file files leak leaks leaked enjoy! thanks! see more click here
    untitled loose files no title media image images""".split()
)
_HEX = re.compile(r"^[0-9a-f]+$", re.I)
_UUID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.I)
_DATE_STAMP = re.compile(r"^\d{4}[-_.]?\d{2}[-_.]?\d{2}([-_ t.]?\d{2}[-_.:]?\d{2}([-_.:]?\d{2})?)?$", re.I)
_VOWELS = set("aeiouyAEIOUY")
_MULTIPART = re.compile(r"(\.part\d+\.rar|\.r\d{2}|\.(7z|zip|rar|tar)\.\d{3}|\.z\d{2})$", re.I)
_SEPARATORS = re.compile(r"[\s_\-.+~()\[\]{},;]+")


def is_multipart_archive(name: str) -> bool:
    """"x.part1.rar", "x.7z.001": every part must keep the same base name to extract."""
    return bool(_MULTIPART.search(name))


def _wordlike(token: str) -> bool:
    letters = [c for c in token if c.isalpha()]
    if len(letters) < 2 or len(letters) != len(token):
        return False
    if not token.isascii():
        return True  # CJK, Cyrillic, ...: real words, we can't judge their spelling
    if len(token) < 3:
        return False
    if not (token.islower() or token.isupper() or token.istitle()):
        return False  # aBcDeF is how random ids look
    vowels = sum(c in _VOWELS for c in token)
    if vowels == 0:
        return False
    ratio = vowels / len(token)
    return 0.15 <= ratio <= 0.8 and not re.search(r"[^aeiouy]{5,}", token.lower())


def meaningfulness(stem: str) -> float:
    """0 for hashes, ids and camera names; 1 for a real title. Only the name, no extension."""
    text = unicodedata.normalize("NFKC", stem or "").strip()
    if not text:
        return 0.0
    compact = re.sub(r"[\s_\-.]", "", text)
    if not compact:
        return 0.0
    if _UUID.match(text) or (_HEX.match(compact) and len(compact) >= 6):
        return 0.0
    if compact.isdigit():
        return 0.0
    if _DATE_STAMP.match(text):
        return 0.05
    # split "IMG_8403", "camelCaseWords", "summer-beach-2024"
    tokens = [t for t in _SEPARATORS.split(re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", text)) if t]
    words = [t for t in tokens if _wordlike(t)]
    generic = [t for t in words if t.lower() in _GENERIC_WORDS]
    real = [t for t in words if t.lower() not in _GENERIC_WORDS]
    if not real:
        if generic:
            return 0.15  # IMG_8403, video_183726, Screenshot_20240101
        if len(tokens) == 1 and len(compact) >= 6:
            return 0.0  # Xk9aB2qP, GaB12xYW0AAbc9z
        return 0.2
    covered = sum(len(t) for t in real) / max(1, len(compact))
    if len(real) == 1 and covered < 0.35 and len(compact) >= 10:
        return 0.35  # one short word inside an id
    return min(1.0, 0.55 + 0.15 * len(real) + 0.3 * covered)


# ─── turning post text into a title ──────────────────────────────────────────

_URL = re.compile(r"(https?://|www\.)\S+", re.I)
_MENTION_LEAD = re.compile(r"^(?:@\w[\w.]*[\s,:]*)+")
_HASHTAG = re.compile(r"#(\w+)", re.UNICODE)
_TRAILING_TAGS = re.compile(r"(\s*)((?:#\w+\s*)+)$", re.UNICODE)


def _drop_trailing_tags(line: str) -> str:
    """"Summer shoot  #summer #beach" -> "Summer shoot"; "Beach day with #friends" keeps the word."""
    match = _TRAILING_TAGS.search(line)
    if not match or match.start() == 0:
        return line
    before = line[: match.start()]
    tags = len(_HASHTAG.findall(match.group(2)))
    if tags >= 2 or match.group(1) != " " or before.rstrip()[-1:] in ".!?,;:":
        return before
    return line
_PROMO = re.compile(
    r"\b(link in (my )?bio|links? below|check (out )?my \w+|subscribe( now)?|follow (me|for more)|"
    r"dm (me )?for \w+|only ?fans|fansly|patreon\.com|use code \w+|swipe( up)?|click (the )?link)\b",
    re.I,
)
_EDGE = " \t-_.,:;|/\\~*+=&^'\"`!?<>[](){}"
_FORUM_NOISE = re.compile(r"^(click to expand|spoiler|show hidden|hidden content|reply|quote|attachments?)\b", re.I)


def _strip_symbols(text: str) -> str:
    keep = []
    for char in text:
        category = unicodedata.category(char)
        if category[0] in "LNM" or char in " -_.,!?'&()[]#@+:;":
            keep.append(char)
        elif category[0] == "Z" or char in "\t\n":
            keep.append(" ")
        # emoji, symbols and control characters are dropped
    return "".join(keep)


def clean_title(text: str | None, limit: int = 120, *, post_text: bool = True) -> str:
    """A short, file-safe title from a caption or page title. Empty if nothing usable is left.

    `post_text` also drops what only post text has: leading @replies, hashtag runs, promotion.
    Album and account names ("@allecakes") are left as they are.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    lines = [line.strip() for line in re.split(r"[\r\n]+", text)]
    lines = [_URL.sub(" ", line) for line in lines if line and not _FORUM_NOISE.match(line.strip())]
    line = next((line for line in lines if sum(c.isalpha() for c in line) >= 2), "")
    if post_text:
        line = _MENTION_LEAD.sub("", line.strip())
        line = _drop_trailing_tags(line)
        if len(_HASHTAG.findall(line)) > 3:
            line = _HASHTAG.sub(" ", line)
        else:
            line = _HASHTAG.sub(r"\1", line)
        line = _PROMO.sub(" ", line)
    line = _strip_symbols(line)
    line = re.sub(r"\s+", " ", line).strip(_EDGE)
    # one sentence is a title; a paragraph is not
    if len(line) > limit:
        first = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
        line = first if 3 <= len(first) <= limit else line
    if len(line) > limit:
        cut = line[:limit]
        line = cut.rsplit(" ", 1)[0] if " " in cut[limit // 2 :] else cut
    line = line.strip(_EDGE)
    if sum(c.isalpha() for c in line) < 2 or line.casefold() in _JUNK_TITLES:
        return ""
    return line


_NOT_CAPTION = frozenset({"a", "blockquote", "script", "style", "noscript", "button", "code", "pre"})


def forum_caption(content: Any) -> str:
    """The title-like first line of a forum post, without quotes, links or signatures.

    Link labels ("Discussion thread", "Bunkr", a file name) and quoted replies are skipped: they
    describe somewhere else, not the files in this post. The page is not changed.
    """
    try:
        pieces = []
        for node in content.find_all(string=True):
            if any(getattr(parent, "name", None) in _NOT_CAPTION for parent in node.parents):
                continue
            pieces.append(str(node))
        text = "\n".join(pieces)
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _FORUM_NOISE.match(line) or _URL.fullmatch(line):
            continue
        lines.append(line)
        if len(lines) >= 3:
            break
    caption = clean_title(lines[0] if lines else "", 100)
    if caption and (len(caption) < 4 or meaningfulness(caption) < 0.5):
        return ""
    return caption


# ─── page titles the crawler already fetched ─────────────────────────────────

_PAGES: OrderedDict[str, dict[str, str]] = OrderedDict()
_PAGES_MAX = 4096


def _page_key(url: object) -> str:
    parts = urlsplit(str(url))
    return f"{parts.netloc.lower().removeprefix('www.')}{parts.path.rstrip('/')}"


def note_page(url: object, soup: BeautifulSoup) -> None:
    """Remember a fetched page's own title (Open Graph, Twitter card, JSON-LD, <title>)."""
    try:
        meta: dict[str, str] = {}
        head = soup.head or soup
        for tag in head.find_all("meta", limit=80):
            key = (tag.get("property") or tag.get("name") or "").lower()
            if key in {"og:title", "twitter:title", "og:site_name", "og:description", "title"}:
                value = " ".join(str(tag.get("content") or "").split())
                if value and key not in meta:
                    meta[key] = value[:300]
        for script in head.find_all("script", attrs={"type": "application/ld+json"}, limit=3):
            raw = script.string or ""
            if len(raw) > 200_000:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for node in data if isinstance(data, list) else [data]:
                if isinstance(node, dict):
                    name = node.get("headline") or node.get("name")
                    if isinstance(name, str) and name.strip():
                        meta.setdefault("ld:name", " ".join(name.split())[:300])
        if soup.title and soup.title.string:
            meta.setdefault("html:title", " ".join(soup.title.string.split())[:300])
        if not meta:
            return
        key = _page_key(url)
        _PAGES[key] = meta
        _PAGES.move_to_end(key)
        while len(_PAGES) > _PAGES_MAX:
            _PAGES.popitem(last=False)
    except Exception:  # noqa: BLE001 - names are never worth a failed crawl
        return


def _strip_site(title: str, site: str, host: str) -> str:
    """"Beach Day | Bunkr" -> "Beach Day". Only whole segments that are the site's own name go."""
    labels = {label for label in (site.casefold(), host.casefold()) if label}
    for part in host.split("."):
        if len(part) > 2:
            labels.add(part.casefold())
    segments = [s.strip() for s in re.split(r"\s+[|–—-]\s+|\s+::\s+", title) if s.strip()]
    kept = [s for s in segments if s.casefold() not in labels and s.casefold().removeprefix("www.") not in labels]
    return " - ".join(kept) if kept else ""


_DOWNLOAD_WORDS = re.compile(r"^(free\s+)?download(ing)?\s*[:\-]?\s+|\s+[-|]\s+(free\s+)?download$", re.I)
_FILE_LIKE = re.compile(r"\.[a-z0-9]{2,5}$", re.I)


def page_title(url: object) -> tuple[str, str]:
    """(title, source) for a page this run fetched, or ("", "")."""
    meta = _PAGES.get(_page_key(url))
    if not meta:
        return "", ""
    host = urlsplit(str(url)).netloc.lower().removeprefix("www.")
    site = meta.get("og:site_name", "")
    for key, source in (("og:title", "open graph"), ("twitter:title", "twitter card"), ("ld:name", "json-ld"),
                        ("html:title", "page title")):
        if value := meta.get(key):
            title = _strip_site(value, site, host)
            title = _DOWNLOAD_WORDS.sub("", title).strip()
            if _FILE_LIKE.search(title):
                # "Download 7b72bde7c9c9.mp4": a file name, judged like one
                stem = _FILE_LIKE.sub("", title).strip()
                title = stem if meaningfulness(stem) >= 0.6 else ""
            if title and title.casefold() != site.casefold():
                return title, source
    return "", ""


def url_title(url: object) -> str:
    """Words from a page slug ("/videos/summer-beach-day-48213" -> "summer beach day")."""
    path = urlsplit(str(url)).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    slug = re.sub(r"\.[a-z0-9]{2,5}$", "", slug, flags=re.I)
    words = [w for w in re.split(r"[-_+]+", slug) if w]
    while words and (words[-1].isdigit() or _HEX.match(words[-1]) and len(words[-1]) >= 6):
        words.pop()
    title = " ".join(words)
    return title if len(words) >= 2 and meaningfulness(title) >= 0.7 else ""


# ─── post titles carry their caption to the scrape item ──────────────────────


class PostTitle(str):
    """The folder name `create_separate_post_title` returns, plus what the post said.

    It is still the plain folder string everywhere (often "" when post folders are off), so
    crawlers don't change; `ScrapeItem` reads the extra fields when it is appended.
    """

    raw: str
    caption: str
    post_id: str
    source: str
    confidence: float

    def __new__(  # noqa: PLR0913
        cls,
        folder: str,
        raw: str | None = None,
        caption: str | None = None,
        post_id: object = None,
        source: str = "caption",
        confidence: float = 0.75,
    ):
        me = super().__new__(cls, folder)
        me.raw = " ".join(str(raw or "").split())
        me.caption = str(caption or "")
        me.post_id = "" if post_id is None else str(post_id)
        me.source = source
        me.confidence = confidence
        return me


# ─── folders ─────────────────────────────────────────────────────────────────

_GENERIC_FOLDERS = frozenset(
    {"loose files", "untitled", "images", "videos", "audio", "other", "photos", "pictures", "files", "media"}
)
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.I)
_TITLES: dict[str, tuple[str, str, str]] = {}  # composed folder title -> (title, domain, id)


def note_title(composed: str, title: str, domain: str, ident: object = None) -> None:
    _TITLES[composed] = (" ".join((title or "").split()), domain, "" if ident is None else str(ident))
    if len(_TITLES) > 50_000:
        _TITLES.clear()


def title_info(composed: str) -> tuple[str, str, str]:
    return _TITLES.get(composed, (composed, "", ""))


def is_generic_folder(name: str) -> bool:
    base = re.sub(r"\s*\([^)]*\)$", "", name).strip().casefold()
    return base in _GENERIC_FOLDERS


def windows_safe(name: str, fallback: str = "Untitled") -> str:
    """No reserved device names, no trailing dots or spaces, no control characters."""
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C")
    name = re.sub(r'[<>:"/\\|?*]', "-", name).strip().rstrip(". ")
    if not name:
        return fallback
    if _RESERVED.match(name):
        name = f"{name}_"
    return name


def owner_key(*, domain: str, url: object, title: str, ident: str = "", post_id: str = "") -> str:
    """Who a folder belongs to. Same site and same album/thread/post id, or same site and title."""
    host = domain or urlsplit(str(url)).netloc.lower().removeprefix("www.")
    if post_id:
        return f"{host}|post:{post_id}"
    if ident:
        return f"{host}|id:{ident}"
    return f"{host}|title:{title.casefold()}"


class FolderRegistry:
    """Stable folder names for collections that share a title.

    The first collection keeps "allecakes"; a different one with the same title in the same
    place gets "allecakes (2)". Assignments are written to a JSONL file, so retries, restarts
    and resumed jobs land in the same folder.
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self.root: Path | None = None
        self.by_owner: dict[tuple[str, str], str] = {}
        self.by_name: dict[tuple[str, str], str] = {}
        self.cache: dict[tuple, Path] = {}

    def load(self, path: str | Path | None, root: Path | None) -> None:
        self.by_owner.clear()
        self.by_name.clear()
        self.cache.clear()
        self.root = root
        self.path = Path(path) if path else None
        if not self.path or not self.path.is_file():
            return
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                self._set(str(row["parent"]), str(row["name"]), str(row["key"]))
            except (ValueError, KeyError, TypeError):
                continue

    def _set(self, parent: str, name: str, key: str) -> None:
        self.by_owner[(parent, key)] = name
        self.by_name[(parent, name.casefold())] = key

    def claim(self, parent: str, desired: str, key: str, base: Path | None) -> str:
        parent_key = parent.casefold()
        if not key or is_generic_folder(desired):
            return desired  # "Loose Files" is shared, never numbered
        if (name := self.by_owner.get((parent_key, key))) is not None:
            return name
        candidate, number = desired, 2
        while True:
            owner = self.by_name.get((parent_key, candidate.casefold()))
            if owner == key:
                break
            if owner is None and not (base is not None and (base / candidate).exists()):
                break  # a folder already on disk that no collection claimed is left alone
            candidate = f"{desired} ({number})"
            number += 1
        self._set(parent_key, candidate, key)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"parent": parent_key, "name": candidate, "key": key}) + "\n")
            except OSError:
                pass
        return candidate

    def resolve(self, root: Path, levels: list[tuple[str, str, str]]) -> Path:
        """`levels` are (name, kind, owner key) from the outermost folder in."""
        cache_key = (str(root), tuple(levels))
        if (hit := self.cache.get(cache_key)) is not None:
            return hit
        relative = Path()
        for name, _kind, key in levels:
            name = windows_safe(name)
            chosen = self.claim(relative.as_posix(), name, key, root / relative)
            relative = relative / chosen
        if len(self.cache) > 20_000:
            self.cache.clear()
        self.cache[cache_key] = relative
        return relative


REGISTRY = FolderRegistry()

_KIND_ALBUM = {"album", "profile"}
_KIND_POST = {"forum", "post"}


def pick_levels(levels: list[tuple[str, str, str]], mode: str, replace_generic: bool) -> list[tuple[str, str, str]]:
    chosen = levels
    if mode == "album":
        chosen = [lv for lv in levels if lv[1] in _KIND_ALBUM] or levels
    elif mode == "post":
        chosen = [lv for lv in levels if lv[1] in _KIND_POST] or levels
    if replace_generic:
        # "Thread/Loose Files/x.jpg" -> "Thread/x.jpg"; a generic folder on its own stays
        meaningful = [lv for lv in chosen if not is_generic_folder(lv[0])]
        if meaningful:
            chosen = meaningful
    return chosen


# ─── file names ──────────────────────────────────────────────────────────────


@dataclasses.dataclass(slots=True)
class Decision:
    filename: str
    number: int
    title: str
    source: str
    confidence: float
    original: str
    strategy: str


def _candidates(naming: dict[str, str], page_url: object, collection_titles: set[str]) -> list[tuple[str, str, float]]:
    """(title, source, confidence) in the order of the naming spec."""
    out: list[tuple[str, str, float]] = []

    def add(value: str | None, source: str, confidence: float, limit: int = 150, *, post_text: bool = False) -> None:
        title = clean_title(value, limit, post_text=post_text)
        if title:
            out.append((title, source, confidence))

    add(naming.get("media_title"), "media title", 0.9)
    page, page_source = page_title(page_url)
    own_page = bool(page) and page.casefold() not in collection_titles
    if own_page:
        add(page, f"media page ({page_source})", 0.6)
    add(naming.get("caption"), naming.get("caption_source") or "caption", float(naming.get("caption_confidence") or 0.75), 80,
        post_text=True)
    add(naming.get("post_title"), "post title", 0.85)
    add(naming.get("description"), "post description", 0.55, 80, post_text=True)
    add(naming.get("album"), "album title", 0.75)
    add(naming.get("profile"), "gallery/creator name", 0.6)
    add(naming.get("thread"), "thread title", 0.5)
    if page and not own_page:
        add(page, f"page ({page_source})", 0.45)
    add(url_title(page_url), "link text", 0.4)
    return out


_STRATEGY_SOURCES = {
    "media": ("media title", "media page"),
    "post": ("caption", "post title", "post description", "forum post", "tweet", "thread title"),
    "album": ("album title", "gallery/creator name", "thread title"),
}


def choose_title(
    stem: str, naming: dict[str, str], page_url: object, settings: NamingSettings
) -> tuple[str, str, float]:
    """The base name for a file (no number, no extension), where it came from, and how sure."""
    worth = meaningfulness(stem)
    if settings.strategy == "original":
        return stem, "original filename", worth
    if settings.keep_meaningful and worth >= 0.6 and settings.strategy == "auto":
        return stem, "original filename", worth
    collection = {naming.get(k, "").casefold() for k in ("album", "thread", "profile") if naming.get(k)}
    candidates = _candidates(naming, page_url, collection)
    preferred = _STRATEGY_SOURCES.get(settings.strategy)
    if preferred:
        for title, source, confidence in candidates:
            if source.startswith(preferred):
                return title, source, confidence
        if settings.keep_meaningful and worth >= 0.6:
            return stem, "original filename", worth
    for title, source, confidence in candidates:
        if confidence >= 0.6:
            return title, source, confidence
    if candidates:
        best = max(candidates, key=lambda c: c[2])
        if best[2] >= 0.4:
            return best
    return stem, "original filename", worth


def _fit(title: str, tail: str, limit: int) -> str:
    room = max(8, limit - len(tail))
    if len(title) <= room:
        return title
    cut = title[:room]
    if " " in cut[room // 2 :]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip(_EDGE) or title[:room]


def name_file(
    url: object,
    chosen: str,
    ext: str | None,
    naming: dict[str, str],
    page_url: object,
    folder: Path,
    sanitize: Any,
    *,
    thumbnail: bool = False,
) -> Decision:
    """The saved filename for one file, numbered in source order for this job."""
    settings = SETTINGS.get()
    number = source_order.assign(str(url))
    row = source_order.stored(str(url))
    if row and row.get("naming") == settings.fingerprint():
        name = str(row.get("name"))
        return Decision(name, number, str(row.get("title") or ""), "kept from this job", float(row.get("confidence") or 1),
                        chosen, str(row.get("strategy") or settings.strategy))
    if thumbnail or is_multipart_archive(chosen):
        return Decision(chosen, number, chosen, "original filename", 1.0, chosen, "original")
    stem, extension = source_order.split_name(chosen, ext)
    extension = extension or (ext or "")
    if settings.smart:
        title, source, confidence = choose_title(stem, naming, page_url, settings)
    else:
        title, source, confidence = stem, "original filename", meaningfulness(stem)
    mode = settings.numbering
    tail = f"-{number:03d}" if mode == "suffix" else f"{number:03d}_" if mode == "prefix" else ""
    limit = settings.max_len
    # Windows paths: keep the whole path under 240 characters when the folder allows it
    room = 240 - len(str(folder)) - len(extension) - 1
    if room < limit:
        limit = max(16, room)
    title = _fit(windows_safe(sanitize(title + extension).removesuffix(extension) or stem, stem or "file"), tail, limit)
    name = source_order.format_number(number, title + extension, mode, extension)
    name = windows_safe(name, f"file{extension}")
    return Decision(name, number, title, source, round(confidence, 2), chosen, settings.strategy if settings.smart else "off")
