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
from cyberdrop_dl.name_dates import strip_dates

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
    group_titles: bool = False  # Folder for each title: "Leaked set pics-193.jpg" in "Leaked set pics/"
    template: str = ""  # Filename template (3.0), "" for the automatic name
    width: int = 3  # digits in the sequence number (3.0)
    context_folders: bool = False  # Folder for each post or album (3.1.1), also for files that keep their own name
    strip_dates: bool = False  # Take dates out of file and folder names (3.1.1)

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
        try:
            width = int(payload.get("numberWidth") or 3)
        except (TypeError, ValueError):
            width = 3
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
            group_titles=flag("titleFolders", True),
            template=clean_template(payload.get("nameTemplate")),
            width=width if width in (3, 4, 5) else 3,
            context_folders=flag("contextFolders", True),
            strip_dates=flag("stripDates", True),
        )

    def fingerprint(self) -> str:
        """Names kept from an earlier run are only reused while the settings that made them match."""
        base = ".".join(
            str(v) for v in (int(self.smart), self.strategy, self.numbering, int(self.keep_meaningful), self.max_len)
        )
        # 2.5 fingerprints stay valid while the 3.0 settings are at their defaults
        if self.width != 3:
            base += f".w{self.width}"
        if self.template:
            base += ".t" + "".join(f"{ord(c):x}" for c in self.template)[:64]
        if self.strip_dates:
            base += ".nd"  # 3.1.1: names kept from an older run may still have their dates
        return base

    def describe(self) -> str:
        names = f"smart names ({self.strategy})" if self.smart else "original names"
        number = {"suffix": "number at the end", "prefix": "number in front", "off": "no numbers"}[self.numbering]
        folders = {"auto": "content-title folders", "album": "album folders", "post": "post/thread folders",
                   "original": "original folders"}[self.folders]
        site = ", site name in folders" if self.site_in_folder else ""
        template = f", template {self.template}" if self.template else ""
        extra = (", a folder for each post or album" if self.context_folders else "") + (", no dates in names" if self.strip_dates else "")
        return f"{names}, {number}, {folders}{site}{template}{extra}"


SETTINGS: ContextVar[NamingSettings] = ContextVar("NEXDROP_NAMING", default=NamingSettings())


# ─── filename templates (3.0) ────────────────────────────────────────────────

TEMPLATE_VARS = ("title", "original", "album", "thread", "post", "creator", "date", "site", "type", "number", "ext")
_TEMPLATE_TOKEN = re.compile(r"\{([a-z]+)\}")
_TEMPLATE_EDGE = " -_.,"
_NAME_VARS = frozenset({"title", "original", "album", "thread", "post", "creator"})


def template_problem(template: str) -> str:
    """Why a filename template can't be used, or "" when it can."""
    text = (template or "").strip()
    if not text:
        return ""
    if len(text) > 150:
        return "longer than 150 characters"
    unknown = [name for name in _TEMPLATE_TOKEN.findall(text) if name not in TEMPLATE_VARS]
    if unknown:
        return f"unknown variable {{{unknown[0]}}}"
    if re.search(r"[{}]", _TEMPLATE_TOKEN.sub("", text)):
        return "a { or } that is not part of a variable"
    if re.search(r'[<>:"/\\|?*]', text):
        return 'a character Windows does not allow in filenames (< > : " / \\ | ? *)'
    if not set(_TEMPLATE_TOKEN.findall(text)) & _NAME_VARS:
        return "no name variable ({title}, {original}, {album}, {thread}, {post} or {creator})"
    return ""


def clean_template(value: object) -> str:
    """The template from the app's settings, or "" (automatic names) when it can't be used."""
    text = " ".join(str(value or "").split())
    if text.lower().endswith(".{ext}"):
        text = text[: -len(".{ext}")]  # the extension is always added
    return "" if template_problem(text) else text


def render_template(template: str, values: dict[str, str], number: str, title_room: int) -> str:
    """The filename stem from a template. Empty variables drop out with the separator in front of them;
    {title} is shortened so the whole stem fits `title_room` plus the fixed parts."""

    def part(name: str) -> str:
        return number if name == "number" else "" if name == "ext" else values.get(name, "")

    pieces: list[str] = []
    pos = 0
    named = False
    for match in _TEMPLATE_TOKEN.finditer(template):
        literal = template[pos : match.start()]
        value = part(match.group(1))
        named = named or bool(value and match.group(1) in _NAME_VARS)
        if value:
            pieces.append(literal)
            pieces.append("\x00" if match.group(1) == "title" else value)
        elif pieces or not literal.strip(_TEMPLATE_EDGE):
            pass  # drop "-" in front of an empty variable
        else:
            pieces.append(literal)
        pos = match.end()
    if not named:
        return ""  # every name in the template is unknown for this file: the automatic name is used
    pieces.append(template[pos:])
    stem = "".join(pieces)
    if "\x00" in stem:
        fixed = len(stem) - stem.count("\x00")
        stem = stem.replace("\x00", _fit(values.get("title", ""), "", max(8, title_room - fixed)))
    stem = re.sub(r"([ \-_.,])\1+", r"\1", stem)
    return stem.strip(_TEMPLATE_EDGE)


def _site_label(page_url: object) -> str:
    host = (urlsplit(str(page_url or "")).hostname or "").removeprefix("www.")
    labels = [label for label in host.split(".") if label]
    return labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")


def template_values(title: str, stem: str, ext: str, naming: dict[str, str], page_url: object,
                    date: str) -> dict[str, str]:
    def clean(value: str | None, post_text: bool = False) -> str:
        return clean_title(value, 80, post_text=post_text) if value else ""

    return {
        "title": title,
        "original": stem,
        "album": clean(naming.get("album")),
        "thread": clean(naming.get("thread")),
        "post": clean(naming.get("post_title")) or clean(naming.get("caption"), True),
        "creator": clean(naming.get("profile")),
        "date": date,
        "site": _site_label(naming.get("post_url") or page_url),
        "type": media_type(ext),
    }


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
_SEPARATORS = re.compile(r"[\s_\-.+~()\[\]{},;@#]+")


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
    return 0.1 <= ratio <= 0.8 and not re.search(r"[^aeiouy]{5,}", token.lower())  # "Stretch" has one vowel in 7


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
# V2: a sentence that only asks the reader to do something is not a title
_CALL_TO_ACTION = re.compile(
    r"\b(check (out )?(my|our|the) (profile|page|bio|link|links|channel|account|onlyfans|of|fansly|patreon|telegram|"
    r"site|website|feed|story|stories)|link in (my |the )?bio|for more (updates|content|pics|photos|videos|vids|"
    r"of me|like this)|more (updates|content|pics|videos) (on|at|in)|follow (me|us|for)|subscribe|turn on (post )?"
    r"notifications|like (and|&) (share|subscribe|follow)|dm (me|for)|message me|available (now )?(on|at)|"
    r"(full |whole |complete )?(video|set|vid|album|gallery|pics|photos)s? (is |are )?(now )?(available|up|out|live|posted) "
    r"(on|at|in)|full (video|set|vid|album) (on|at|in)|use (my )?code|discount|promo code|join (my|our)|"
    r"don'?t forget to|let me know|what do you think|tag a friend|comment below)\b",
    re.I,
)
# "Here is my new ...", "Check out my latest ...": words before the title
_LEAD_IN = re.compile(
    r"^(?:(?:here(?:'s|’s| is| are)|this is|these are|presenting|introducing|sharing|posting|"
    r"just (?:posted|dropped|uploaded|finished|did)|check (?:out )?|enjoy|finally|new post|"
    r"some (?:more|new)|a few|another)(?:\s*:\s*|\s+))+(?:(?:my|our|the|a|an|some|this|these|me in)\s+)?"
    r"(?:(?:new|latest|brand new|recent)\s+)*",
    re.I,
)
_OWN_NEW = re.compile(r"^(?:my|our)\s+(?:new|latest|brand new|recent)\s+", re.I)
_TRAILING_FILLER = re.compile(
    r"(?:\s*[-–—,:;]\s*|\s+)(?:hope you (?:guys )?(?:like|love|enjoy)(?: it| them| this)?|enjoy(?: guys)?|"
    r"for you(?: guys| all)?|(?:what )?do you like it|lol|lmao|xoxo|xx+|♡+|"
    r"(?:thanks?|thank you|thx|ty)\b.*|[(\[]?\d+\s*(?:pics?|photos?|images?|imgs?|vids?|videos?|clips?|gifs?|files?|"
    r"mb|gb)[)\]]?)\W*$",
    re.I,
)
_CREDIT = re.compile(r"^(?:all )?(?:credits?|kudos|props|shout ?out)\b|^(?:source|sauce|via|from)\s*[:\-]", re.I)
_THANKS = re.compile(r"^(?:thanks?|thank you|thx|ty|tysm)\b", re.I)
_QUESTION = re.compile(r"\?\s*$|^(?:any(?:one|body)|does anyone|did anyone|who|where|can (?:someone|anyone|you)|is there|"
                       r"are there|has anyone|pls|please|plz|request(?:ing)?)\b", re.I)
# A whole caption that says nothing about the files
_BOILERPLATE = re.compile(
    r"^(?:(?:thanks?|thank you|thx|ty)(?: (?:so|very) much)?(?: for (?:watching|looking|viewing|the support|sharing|posting|"
    r"the (?:share|upload|post|links?)|uploading|this))?|enjoy|enjoy guys|here you go|as requested|"
    r"requested|by request|credits?(?: to \S+)?|via \S+|source|sauce|discussion thread|new post|update[ds]?|bump|"
    r"reupload(?:ed)?|re-?up|mirror(?:s| links?)?|links? (?:in|below|inside|fixed)|dl links?|download links?|"
    r"see attach(?:ed|ments?)|in (?:the )?spoiler|no pass(?:word)?|pass(?:word)?:? \S+|part \d+|vol(?:ume)?\.? \d+|"
    r"set \d+|pics?|vids?|videos?|photos?|more|here|link|links|good stuff|nice|hot|cute|wow|omg|"
    r"(?:new )?(?:pics?|vids?|videos?|photos?|content)(?: and | & | \+ )(?:pics?|vids?|videos?|photos?)|"
    r"(?:new|untitled|my|unnamed)(?: (?:folder|album|collection|gallery))(?: \d+)?|(?:folder|album) ?\d*)$",
    re.I,
)
_EDGE = " \t-_.,:;|/\\~*+=&^'\"`!?<>[](){}"
_FORUM_NOISE = re.compile(r"^(click to expand|spoiler|show hidden|hidden content|reply|quote|attachments?)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\s+[|•·]\s+|\s+[–—]\s+(?=[A-Z])")


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


def is_boilerplate(text: str) -> bool:
    plain = re.sub(r"\s+", " ", _strip_symbols(text or "")).strip(_EDGE)
    return bool(_BOILERPLATE.match(plain))


def _caption_sentence(sentence: str) -> str:
    """One sentence of a caption without its lead-in, tags, mentions and filler."""
    line = _MENTION_LEAD.sub("", sentence.strip())
    line = _drop_trailing_tags(line)
    if len(_HASHTAG.findall(line)) > 3:
        line = _HASHTAG.sub(" ", line)
    else:
        line = _HASHTAG.sub(r"\1", line)
    line = _PROMO.sub(" ", line)
    line = _strip_symbols(line)
    line = _edge(re.sub(r"\s+", " ", line))
    for _ in range(2):
        line = _edge(_TRAILING_FILLER.sub("", line))
    trimmed = _edge(_LEAD_IN.sub("", line))
    trimmed = _edge(_OWN_NEW.sub("", trimmed))
    if len(trimmed.split()) >= 1 and sum(c.isalpha() for c in trimmed) >= 3:
        line = trimmed
    return line


def _caption_title(lines: list[str]) -> str:
    """The first sentence of a caption that names something (V2).

    "Here is my new Summer Beach Photoshoot! Check my profile for more updates. #summer"
    -> "Summer Beach Photoshoot"
    """
    for raw in lines:
        for sentence in _SENTENCE.split(raw):
            if (cta := _CALL_TO_ACTION.search(sentence)) is not None:
                # "Pool Party Part 2 - link in bio": keep what comes before the request
                sentence = sentence[: cta.start()]
                if len(re.findall(r"\w{2,}", sentence)) < 2:
                    continue
            if not sentence.strip():
                continue
            line = _caption_sentence(sentence)
            if sum(c.isalpha() for c in line) < 2 or is_boilerplate(line):
                continue
            return line
    return ""


def clean_title(text: str | None, limit: int = 120, *, post_text: bool = True) -> str:
    """A short, file-safe title from a caption or page title. Empty if nothing usable is left.

    `post_text` also drops what only post text has: leading @replies, hashtag runs, promotion,
    calls to action and lead-ins ("Here is my new ..."). Album and account names ("@allecakes")
    are left as they are.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    lines = [line.strip() for line in re.split(r"[\r\n]+", text)]
    lines = [_URL.sub(" ", line) for line in lines if line and not _FORUM_NOISE.match(line.strip())]
    if post_text:
        line = _caption_title(lines)
    else:
        line = next((line for line in lines if sum(c.isalpha() for c in line) >= 2), "")
        line = _strip_symbols(line)
        line = _edge(re.sub(r"\s+", " ", line))
    # one sentence is a title; a paragraph is not
    if len(line) > limit:
        first = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
        line = first if 3 <= len(first) <= limit else line
    if len(line) > limit:
        cut = line[:limit]
        line = cut.rsplit(" ", 1)[0] if " " in cut[limit // 2 :] else cut
    line = _balance(line.strip(_EDGE), line)
    if sum(c.isalpha() for c in line) < 2 or line.casefold() in _JUNK_TITLES:
        return ""
    return line


_LEAD_TAG = re.compile(r"^\s*[\[(][^\[\]()]{1,12}[\])]\s*")  # "[HQ] Beach", "(NEW) Set"


def _edge(text: str) -> str:
    untagged = _LEAD_TAG.sub("", text)
    if sum(c.isalpha() for c in untagged) >= 2:
        text = untagged
    return _balance(text.strip(_EDGE), text)


def _balance(stripped: str, before: str) -> str:
    """Put back a closing bracket the edge strip took: "Red Dress Shoot (2024)" keeps its ")"."""
    for opening, closing in ("()", "[]"):
        if stripped.count(opening) > stripped.count(closing) and before.rstrip(_EDGE.replace(closing, "")).endswith(closing):
            stripped += closing
    return stripped


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


def _hosted_at(title: str, site: str, host: str) -> str:
    """"Beach Sunset hosted at ImgBB" -> "Beach Sunset"."""
    labels = [re.escape(label) for label in {site, host, *[p for p in host.split(".") if len(p) > 2]} if label]
    if not labels:
        return title
    pattern = rf"\s+(?:(?:hosted|uploaded|shared|posted|stored)\s+)?(?:at|on|via|by|from|@)\s+(?:{'|'.join(labels)})(?:\.\w+)?$"
    return re.sub(pattern, "", title, flags=re.I).strip()


_DOWNLOAD_WORDS = re.compile(
    r"^(free\s+)?(download(ing)?|watch|view|stream|play|open)\s*[:\-]?\s+|\s+[-|]\s+(free\s+)?download$", re.I)
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
            title = _hosted_at(title, site, host)
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

# V2: every candidate is scored, and the reasons are kept for the naming report.
#   base  how much this kind of metadata usually says about one file
#   × quality  what the text itself is worth (hash, generic, site name, advert, ...)
# The best score wins if it reaches ACCEPT; otherwise the original filename stays.
ACCEPT = 0.5
_BASE = {
    "media title": 0.92,  # a title the site gives this one file
    "media caption": 0.8,  # this one file's own caption or description
    "media page": 0.7,  # the title of the file's own page
    "post title": 0.85,
    "caption": 0.78,  # the post's text (tweet, Bluesky post, TikTok description)
    "forum post": 0.72,  # the first line of a forum post
    "album title": 0.68,
    "post description": 0.55,
    "thread title": 0.55,
    "gallery/creator name": 0.55,
    "page": 0.42,  # a page title that is also a collection's title
    "link text": 0.4,
    "embedded title": 0.7,
    "your correction": 1.0,
}
_FILE_SCOPE = {"media title", "media caption", "media page", "original filename", "embedded title"}
_SITE_NAMES = frozenset(
    """bunkr bunkrr cyberdrop gofile pixeldrain cyberfile erome imgbb ibb simpcity simpcity.su simpcity.cr
    socialmediagirls twitter x tiktok instagram reddit redgifs imgur imgbox pixhost jpg jpg.church jpg5 jpg6
    mega mega.nz mediafire google drive dropbox catbox fileditch saint turbo coomer kemono fapello onlyfans
    fansly patreon telegram bluesky discord youtube vimeo streamtape doodstream mixdrop voe filester buzzheavier
    sendvid streamable imagebam imagevenue postimg postimages chevereto xenforo""".split()
)
_HOSTING = re.compile(
    r"\b(file (hosting|sharing|host)|image (hosting|host|upload)|free (image|file|video) (hosting|upload|host)|"
    r"upload (and|&) share|share (your )?files|cloud storage|fast (and|&) secure|download (file|now|link)|"
    r"direct link|file (not found|deleted|removed)|album (not found|deleted)|video player)\b",
    re.I,
)
_ERROR_PAGE = re.compile(  # bot checks and error pages fetched instead of the real one
    r"^(?:\d{3}\b|just a moment|attention required|access denied|forbidden|bad gateway|service unavailable|"
    r"(?:page|file|video|image|album) (?:not found|unavailable|removed|deleted)|not found|error\b|ddos-guard|"
    r"security check|verify(?:ing)? you are (?:a )?human|are you (?:a )?human|captcha|checking your browser|"
    r"cloudflare|one more step|please wait|redirecting|sign (?:in|up)|log ?in|create an account|register|"
    r"you must be logged in|members only|age verification|are you (?:18|over 18))",
    re.I,
)
_LINK_TALK = re.compile(  # how to download, not what it is
    r"\b(?:links?|password|pass|pw|mirror|re-?up(?:load)?|download|dl|spoiler|folder)\b", re.I)
_NAVIGATION = re.compile(
    r"^(home|homepage|log ?in|sign ?in|sign ?up|register|forums?|threads?|latest|what'?s new|new posts|members|"
    r"search( results)?|page \d+|next|prev(ious)?|menu|404|not found|error|access denied|forbidden|just a moment|"
    r"attention required|ddos-guard|security check|are you human|verify you are human|redirecting|loading|"
    r"index of\b.*|untitled document|default)$",
    re.I,
)
_ADVERT = re.compile(
    r"\b(sponsored|advertisement|advertise here|casino|betting|crypto|bitcoin|vpn deal|hot (girls|singles) (near|in)|"
    r"meet (local )?singles|live (sex )?cams?|cam girls|webcam chat|porn games?|sex games?|play now|"
    r"click here|download (the )?app|install (the )?app|win (a|an|free)|free trial)\b",
    re.I,
)
_HOST_ID = re.compile(r"(?:[-_ .]+)([A-Za-z0-9]{6,16})$")


@dataclasses.dataclass(slots=True)
class Candidate:
    title: str
    source: str
    base: float
    score: float = 0.0
    verdict: str = ""  # "chosen", "lower", "rejected"
    reason: str = ""


@dataclasses.dataclass(slots=True)
class Decision:
    filename: str
    number: int
    title: str
    source: str
    confidence: float
    original: str
    strategy: str
    reason: str = ""
    candidates: list[Candidate] = dataclasses.field(default_factory=list)
    group: str = ""  # a folder for files that share this title, or ""


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", text or "").casefold()).strip()


def is_site_name(text: str, extra: set[str] | frozenset[str] = frozenset()) -> bool:
    plain = _norm(text)
    if not plain:
        return False
    if plain in _SITE_NAMES or plain in extra or plain.replace(" ", ".") in _SITE_NAMES:
        return True
    words = plain.split()
    # "Bunkr Album", "GoFile Folder", "Cyberdrop Download"
    return bool(len(words) <= 3 and words[0] in _SITE_NAMES | set(extra)
                and all(w in _SITE_NAMES or w in _GENERIC_WORDS or w in _JUNK_TITLES or w in {"album", "folder", "cdn"}
                        for w in words[1:]))


def assess(title: str, *, site_labels: set[str] | frozenset[str] = frozenset()) -> tuple[float, str]:
    """(quality 0..1, why). Quality 0 means the text is not a name for anything."""
    if not title:
        return 0.0, "nothing left after cleaning"
    plain = title.strip()
    folded = plain.casefold()
    if folded in _JUNK_TITLES or is_boilerplate(plain):
        return 0.0, "generic or boilerplate text"
    if _NAVIGATION.match(plain) or _ERROR_PAGE.match(plain):
        return 0.0, "site navigation or an error page"
    if is_generic_folder(plain):
        return 0.0, "generic folder name"
    words = re.findall(r"\w+", folded)
    if _LINK_TALK.search(plain) and (any(w in _SITE_NAMES or w in site_labels for w in words)
                                     or len(_LINK_TALK.findall(plain)) >= 2):
        return 0.0, "download instructions, not a title"
    if is_site_name(plain, site_labels):
        return 0.0, "the website's own name"
    if _HOSTING.search(plain):
        return 0.0, "file host boilerplate"
    if _ADVERT.search(plain):
        return 0.0, "advertising text"
    worth = meaningfulness(plain)
    if worth < 0.2:
        return 0.0, "hash, id or camera name"
    if worth < 0.5:
        return worth * 0.6, "looks more like an id than a title"
    quality = min(1.0, 0.35 + worth * 0.65)
    words = len(plain.split())
    if words > 14:
        return quality * 0.7, "long text, more description than title"
    return quality, ""


def original_base(stem: str) -> tuple[str, float]:
    """The original name with a host's random id taken off ("Beach Sunset-aB3dE9fG"), and its worth."""
    worth = meaningfulness(stem)
    match = _HOST_ID.search(stem or "")
    if match and worth >= 0.5:
        token = match.group(1)
        randomish = meaningfulness(token) == 0.0 and (
            any(c.isdigit() for c in token) or (not token.islower() and not token.isupper() and not token.istitle())
        )
        rest = stem[: match.start()].strip(_EDGE)
        if randomish and rest and meaningfulness(rest) >= 0.6:
            return rest, meaningfulness(rest)
    return stem, worth


def _site_labels(*urls: object) -> set[str]:
    labels: set[str] = set()
    for url in urls:
        host = urlsplit(str(url or "")).netloc.lower().removeprefix("www.")
        if host:
            labels.add(host)
            labels.update(p for p in host.split(".") if len(p) > 2)
    return labels


def _candidates(
    stem: str,
    naming: dict[str, str],
    page_url: object,
    settings: NamingSettings,
) -> list[Candidate]:
    """Everything that could name this file, scored, best first. Rejected ones are kept for the report."""
    out: list[Candidate] = []
    seen: dict[str, Candidate] = {}
    labels = _site_labels(page_url, naming.get("post_url"))
    collection = {_norm(naming.get(k, "")) for k in ("album", "thread", "profile") if naming.get(k)}
    upper = {_norm(naming.get(k, "")) for k in ("thread", "profile") if naming.get(k)}
    original, worth = original_base(stem)

    def add(value: str | None, source: str, *, limit: int = 150, post_text: bool = False, base: float | None = None,
            label: str | None = None) -> None:
        if not value:
            return
        title = clean_title(value, limit, post_text=post_text)
        kind = label or source
        cand = Candidate(title or " ".join(str(value).split())[:80], kind, base if base is not None else _BASE[source])
        if not title:
            cand.verdict, cand.reason = "rejected", "nothing usable after cleaning"
            out.append(cand)
            return
        quality, why = assess(title, site_labels=labels)
        first_line = str(value).strip().splitlines()[0] if str(value).strip() else ""
        if quality and source == "forum post" and _QUESTION.search(first_line):
            quality, why = 0.0, "a question to other members, not a title"
        elif quality and source == "forum post" and _CREDIT.match(first_line):
            quality, why = 0.0, "a credit line, not a title"
        elif quality and source == "forum post" and _THANKS.match(first_line) and len(first_line.split()) <= 10:
            quality, why = 0.0, "a thank-you reply, not a title"  # "Thank you so much for posting this"
        cand.score = round(cand.base * quality, 3)
        key = _norm(title)
        if quality and key and key == _norm(original):
            cand.verdict, cand.reason = "rejected", "same as the original filename"
        elif quality and source not in ("thread title", "gallery/creator name", "album title", "page") and key in upper:
            # a post or file "titled" with the creator's or thread's name says nothing more specific
            cand.score = round(min(cand.score, _BASE["thread title"] * quality), 3)
            why = why or "same as the thread or creator name"
        if not quality:
            cand.verdict, cand.reason = "rejected", why
        else:
            cand.reason = why
            if key in seen and not cand.verdict:
                # the same words from two places: keep the better-backed one
                other = seen[key]
                if cand.score <= other.score:
                    cand.verdict, cand.reason = "rejected", f"same text as the {other.source}"
                else:
                    other.verdict, other.reason = "rejected", f"same text as the {cand.source}"
                    seen[key] = cand
            elif not cand.verdict:
                seen[key] = cand
        out.append(cand)

    # the original filename is a candidate like the others
    orig = Candidate(original, "original filename", 0.0)
    if worth < 0.2:
        orig.verdict, orig.reason = "rejected", "hash, id or camera name"
    elif worth < 0.6:
        orig.score = round(0.35 * worth, 3)
        orig.reason = "partly an id"
    else:
        orig.base = 0.88 if settings.keep_meaningful else 0.45
        orig.score = round(orig.base * (0.75 + 0.25 * min(1.0, worth)), 3)
        if original != stem:
            orig.reason = "host's random id removed"
    out.append(orig)
    seen[_norm(original)] = orig

    add(naming.get("media_title"), "media title")
    add(naming.get("media_caption"), "media caption", limit=80, post_text=True)
    page, page_source = page_title(page_url)
    if page and _norm(page) not in collection:
        add(page, "media page", label=f"media page ({page_source})")
    caption_source = naming.get("caption_source") or "caption"
    caption_kind = "forum post" if caption_source == "forum post" else "caption"
    add(naming.get("caption"), caption_kind, limit=80, post_text=True, label=caption_source)
    add(naming.get("post_title"), "post title", post_text=True)
    add(naming.get("description"), "post description", limit=80, post_text=True)
    add(naming.get("album"), "album title")
    add(naming.get("profile"), "gallery/creator name")
    add(naming.get("thread"), "thread title")
    if page and _norm(page) in collection:
        add(page, "page", label=f"page ({page_source})")
    add(url_title(page_url), "link text")
    post_level = [c for c in out if c.source in ("caption", "forum post", "post title") and c.verdict != "rejected"
                  and c.score >= ACCEPT and c.reason != "same as the thread or creator name"]
    if post_level:
        # a file host's album name never outranks the linking post's own title (SimpCity post -> Bunkr album)
        for cand in out:
            if cand.source == "album title" and cand.verdict != "rejected":
                cand.score = round(cand.score * 0.9, 3)
                cand.reason = cand.reason or "the post's own title is preferred over the album's"
    out.sort(key=lambda c: (c.verdict == "rejected", -c.score))
    return out


_STRATEGY_SOURCES = {
    "media": ("media title", "media caption", "media page", "original filename", "embedded title"),
    "post": ("caption", "post title", "post description", "forum post", "tweet", "thread title"),
    "album": ("album title", "gallery/creator name", "thread title"),
}


def choose_title(
    stem: str, naming: dict[str, str], page_url: object, settings: NamingSettings
) -> tuple[str, str, float]:
    """The base name for a file (no number, no extension), where it came from, and how sure."""
    title, source, confidence, _reason, _cands = explain_title(stem, naming, page_url, settings)
    return title, source, confidence


def explain_title(
    stem: str, naming: dict[str, str], page_url: object, settings: NamingSettings
) -> tuple[str, str, float, str, list[Candidate]]:
    """`choose_title` with the reason and every candidate, for the naming report."""
    original, worth = original_base(stem)
    if settings.strategy == "original":
        return stem, "original filename", worth, "Filename strategy is Original", []
    candidates = _candidates(stem, naming, page_url, settings)
    usable = [c for c in candidates if c.verdict != "rejected" and c.score > 0]
    chosen: Candidate | None = None
    reason = ""
    preferred = _STRATEGY_SOURCES.get(settings.strategy)
    if preferred:
        chosen = next((c for c in usable if c.source.startswith(preferred) and c.score >= ACCEPT * 0.8), None)
        if chosen:
            reason = f"Filename strategy prefers {chosen.source}"
    if chosen is None and usable and usable[0].score >= ACCEPT:
        chosen = usable[0]
        reason = "highest score"
    if chosen is None:
        for cand in candidates:
            if cand.verdict != "rejected":
                cand.verdict = "lower"
                cand.reason = cand.reason or f"score {cand.score:.2f} is below {ACCEPT}"
        return stem, "original filename", worth, "no metadata was trustworthy enough, so the original name stays", candidates
    chosen.verdict = "chosen"
    for cand in candidates:
        if cand is not chosen and cand.verdict != "rejected":
            cand.verdict = "lower"
            cand.reason = cand.reason or f"lower score than the {chosen.source}"
    title = chosen.title
    if chosen.source == "original filename":
        title = original
    corrected = CORRECTIONS.lookup(naming, title)
    if corrected:
        return corrected, "your correction", 1.0, f"you renamed “{title}” to this before", candidates
    return title, chosen.source, chosen.score, reason, candidates


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
    date: str = "",
) -> Decision:
    """The saved filename for one file, numbered in source order for this job.

    `date` is the upload date ("2026-10-27") for a filename template's {date}, or ""."""
    settings = SETTINGS.get()
    number = source_order.assign(str(url))
    row = source_order.stored(str(url))
    if row and row.get("naming") == settings.fingerprint():
        name = str(row.get("name"))
        title = str(row.get("title") or "")
        return Decision(name, number, title, "kept from this job", float(row.get("confidence") or 1),
                        chosen, str(row.get("strategy") or settings.strategy), "same file as earlier in this job",
                        group=title_folder(title, str(row.get("source") or ""), folder, settings)
                        or context_folder(naming, folder, settings))
    if thumbnail or is_multipart_archive(chosen):
        why = "thumbnail" if thumbnail else "parts of a split archive must keep one name"
        return Decision(chosen, number, chosen, "original filename", 1.0, chosen, "original", why)
    stem, extension = source_order.split_name(chosen, ext)
    extension = extension or (ext or "")
    candidates: list[Candidate] = []
    if settings.smart:
        title, source, confidence, reason, candidates = explain_title(stem, naming, page_url, settings)
    else:
        title, source, confidence, reason = stem, "original filename", meaningfulness(stem), "smart names are off"
    mode = settings.numbering
    digits = source_order.padded(number)
    limit = settings.max_len
    # Windows paths: keep the whole path under 240 characters when the folder allows it
    room = 240 - len(str(folder)) - len(extension) - 1
    if room < limit:
        limit = max(16, room)
    title = windows_safe(sanitize(title + extension).removesuffix(extension) or stem, stem or "file")
    if settings.strip_dates:
        title = strip_dates(title)
    shaped = ""
    if settings.template:
        values = template_values(title, stem, extension, naming, page_url, date)
        shaped = render_template(settings.template, values, digits if number else "", limit - len(digits) - 1)
        shaped = windows_safe(sanitize(shaped + extension).removesuffix(extension), "") if shaped else ""
        if shaped and "{number}" in settings.template:
            mode = "off"  # the template placed the number itself
    tail = f"-{digits}" if mode == "suffix" else f"{digits}_" if mode == "prefix" else ""
    if shaped:
        title = _fit(shaped, tail, limit)
        reason = f"{reason}; template {settings.template}" if reason else f"template {settings.template}"
    else:
        title = _fit(title, tail, limit)
    name = source_order.format_number(number, title + extension, mode, extension)
    name = windows_safe(name, f"file{extension}")
    if settings.smart and source == "original filename" and confidence < 0.5:
        # nothing named this file; "Read titles inside files" may still find one after it downloads
        UNNAMED[str(url)] = number
    return Decision(name, number, title, source, round(confidence, 2), chosen,
                    settings.strategy if settings.smart else "off", reason, candidates,
                    group=(title_folder(title, source, folder, settings) if settings.smart else "")
                    or context_folder(naming, folder, settings))


# Titles that several files share (a post's caption, an album): with "Folder for each title" on,
# those files get a folder of that name. A file's own name (original filename, its own title) never does.
_GROUP_SOURCES = frozenset({"caption", "forum post", "tweet", "post title", "post description", "album title",
                            "thread title", "gallery/creator name", "your correction"})


def _folder_base(name: str) -> str:
    """"Lala Kudo (SimpCity)" / "allecakes (2)" -> "lala kudo" / "allecakes", for comparing with a title."""
    return _norm(re.sub(r"(\s*\([^)]*\))+$", "", name))


def title_folder(title: str, source: str, folder: Path, settings: NamingSettings) -> str:
    """The folder for a file named after `title`, or "" when it stays where it is: the setting is off,
    the name is the file's own, or the folder it is in (or one above) already has that title."""
    if not settings.group_titles or not title or source not in _GROUP_SOURCES:
        return ""
    return _folder_name(title, folder, settings)


def _folder_name(title: str, folder: Path, settings: NamingSettings) -> str:
    if settings.strip_dates:
        title = strip_dates(title)
    name = windows_safe(_fit(title, "", 80), "")
    key = _norm(name)
    if not key or any(_folder_base(part) == key or _folder_base(strip_dates(part)) == key for part in folder.parts[-6:]):
        return ""
    return name


def context_folder(naming: dict[str, str], folder: Path, settings: NamingSettings) -> str:
    """"Folder for each post or album" (3.1.1): the folder named after the post or album a file came
    from, also when the file keeps its own name. "" when the setting is off, the file has no post or
    album title worth a folder, or the folder it is in (or one above) already has that title."""
    if not settings.context_folders:
        return ""
    for value, post_text in ((naming.get("post_title"), False), (naming.get("caption"), True), (naming.get("album"), False)):
        title = clean_title(value, 80, post_text=post_text) if value else ""
        if settings.strip_dates:
            title = strip_dates(title)
        if title and assess(title)[0] >= ACCEPT:
            return _folder_name(title, folder, settings)
    return ""


UNNAMED: dict[str, int] = {}  # file url -> its number, for files that kept a meaningless original name


def embedded_name(url: str, path: Path, settings: NamingSettings) -> tuple[str, str]:
    """(new filename, where the title was found) from a title inside the downloaded file, or ("", "").

    Only for files nothing else could name. Embedded tags are the creator's own metadata, so
    they are trusted like a page title; they never replace a name that came from the site.
    """
    from cyberdrop_dl import media_tags

    number = UNNAMED.get(url)
    if number is None:
        return "", ""
    title, where = media_tags.read_title(path)
    title = clean_title(title, settings.max_len, post_text=False)
    quality, _why = assess(title)
    if quality * _BASE["embedded title"] < ACCEPT:
        return "", ""
    extension = path.suffix
    room = 240 - len(str(path.parent)) - len(extension) - 1
    mode = settings.numbering
    digits = source_order.padded(number)
    tail = f"-{digits}" if mode == "suffix" else f"{digits}_" if mode == "prefix" else ""
    title = _fit(windows_safe(title), tail, max(16, min(settings.max_len, room)))
    name = windows_safe(source_order.format_number(number, title + extension, mode, extension), path.name)
    return name, where


# ─── names you corrected ─────────────────────────────────────────────────────


def context_map(naming: dict[str, str]) -> dict[str, str]:
    """Where a correction can apply: the post, album, thread and creator the file came from."""
    return {kind: value for kind in ("post", "album", "thread", "profile") if (value := naming.get(f"{kind}_key"))}


def context_keys(naming: dict[str, str]) -> list[str]:
    """`context_map` values, most specific first."""
    return list(context_map(naming).values())


class CorrectionStore:
    """Names you changed in the History tab, used again for files from the same post, album or thread.

    One JSON object per line: {"context": owner key, "title": the name NexDrop chose, "use": your name}.
    A correction is never applied outside its own post, album or thread.
    """

    def __init__(self) -> None:
        self.rules: dict[tuple[str, str], str] = {}

    def load(self, path: str | Path | None) -> None:
        self.rules.clear()
        if not path or not Path(path).is_file():
            return
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            context, title, use = str(row.get("context") or ""), _norm(str(row.get("title") or "")), str(row.get("use") or "")
            if not context or not title:
                continue
            if row.get("removed"):
                self.rules.pop((context, title), None)
            elif use.strip():
                self.rules[(context, title)] = use.strip()

    def lookup(self, naming: dict[str, str], title: str) -> str:
        if not self.rules:
            return ""
        key = _norm(title)
        for context in context_keys(naming):
            if (use := self.rules.get((context, key))) is not None:
                return use
        return ""


CORRECTIONS = CorrectionStore()


# ─── naming report ───────────────────────────────────────────────────────────


def safe_url(url: object) -> str:
    """Scheme, host and path only: no query string, fragment or login, so no tokens reach a report."""
    parts = urlsplit(str(url or ""))
    if not parts.netloc:
        return ""
    host = parts.hostname or ""
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


class NamingReport:
    """'Explain file names' (Settings > Smart naming): why each file got its name, as plain text."""

    def __init__(self) -> None:
        self.path: Path | None = None

    def open(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write("NexDrop naming report: why each file got its name.\n"
                                 "Scores: 0 = unusable, 1 = certain. Links are shown without query strings.\n\n")
            except OSError:
                self.path = None

    def write(self, decision: Decision, folder: str, identity: dict[str, Any]) -> None:
        if not self.path:
            return
        lines = [f"#{decision.number:03d}  {decision.original}  ->  {decision.filename}"]
        lines.append(f"  folder:  {folder or '.'}")
        trail = [identity.get(k) for k in ("post_url", "page_url") if identity.get(k)]
        if trail:
            lines.append(f"  from:    {'  >  '.join(dict.fromkeys(trail))}")
        parents = [f"{label} “{identity[k]}”" for k, label in (("thread", "thread"), ("post_title", "post"),
                                                                       ("album", "album"), ("creator", "creator"))
                   if identity.get(k)]
        if parents:
            lines.append(f"  in:      {', '.join(parents)}")
        lines.append(f"  chose:   {decision.source} “{decision.title}” ({decision.confidence:.2f}): {decision.reason}")
        for cand in decision.candidates:
            mark = {"chosen": "*", "rejected": "x"}.get(cand.verdict, "-")
            why = f": {cand.reason}" if cand.reason else ""
            lines.append(f"   {mark} {cand.source} “{cand.title}” {cand.score:.2f} {cand.verdict}{why}")
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n\n")
        except OSError:
            pass


REPORT = NamingReport()


def media_type(ext: str) -> str:
    ext = (ext or "").lower().lstrip(".")
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "avif", "heic", "heif", "bmp", "tif", "tiff", "jxl", "svg"}:
        return "image"
    if ext in {"mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "ts", "m2ts", "3gp", "mpg", "mpeg"}:
        return "video"
    if ext in {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma", "alac"}:
        return "audio"
    if ext in {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst"}:
        return "archive"
    return "other"
