"""One classification for every failed file, used by the run summary, the retry pass, the job
database and the site health record.

A category says what went wrong and whether trying again can help:

  category        retry   meaning
  rate_limited    later   the site asked to slow down (429, or 503 with Retry-After)
  server_busy     later   temporary server error (500, 502, 503, 504, 52x)
  timeout         later   the server did not answer in time
  connection      later   connection reset or dropped mid-transfer
  unreachable     later   the site's file servers never answered (many timeouts in a row)
  dns             no      the server name does not exist (any more)
  expired_url     refresh a temporary download link was refused; the page it came from has a new one
  auth_required   no      sign-in or permission needed (401, 403 that is not an expired link)
  bot_check       refresh a bot-check page came back instead of the file
  not_found       no      deleted (404, 410)
  quota           later   the host's transfer quota is used up (509)
  disk_full       no      not enough free space
  permission      no      the file could not be written (permission denied, path too long)
  corrupted       now     the transfer was incomplete or did not match the file it continued
  web_page        no      a web page came back instead of the file (often a login or error page)
  password        no      the link needs a password
  unsupported     no      no downloader for this kind of link
  other           no      anything else
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

RETRY_LATER = frozenset({"rate_limited", "server_busy", "timeout", "connection", "unreachable", "quota"})
RETRY_NOW = frozenset({"corrupted"})
REFRESH = frozenset({"expired_url", "bot_check"})
PERMANENT = frozenset({"dns", "auth_required", "not_found", "disk_full", "permission", "web_page", "password",
                       "unsupported", "other"})

LABELS = {
    "rate_limited": "Rate limited",
    "server_busy": "Server busy",
    "timeout": "Timed out",
    "connection": "Connection dropped",
    "unreachable": "Server unreachable",
    "dns": "Server name not found",
    "expired_url": "Link expired",
    "auth_required": "Sign-in or permission needed",
    "bot_check": "Bot check",
    "not_found": "Deleted",
    "quota": "Transfer quota used up",
    "disk_full": "Disk full",
    "permission": "Could not write the file",
    "corrupted": "Incomplete or mismatched transfer",
    "web_page": "Web page instead of the file",
    "password": "Password needed",
    "unsupported": "No downloader for this link",
    "other": "Failed",
}

ACTIONS = {
    "rate_limited": "NexDrop slowed down for this site. Retry unfinished in a few minutes.",
    "server_busy": "Temporary server problem. Retry unfinished in a few minutes.",
    "timeout": "The server was slow. Retry unfinished, or raise the read timeout in Settings > Speed.",
    "connection": "The connection dropped. Retry unfinished continues where it stopped.",
    "unreachable": "The site's file servers never answered. Your network may block them: try a VPN or a proxy.",
    "dns": "The site moved or shut down. Look for a newer link.",
    "expired_url": "The download link expired. Retry unfinished asks the page for a fresh link.",
    "auth_required": "Sign in to the site in your browser, export fresh cookies under Settings > Sign-ins, then retry.",
    "bot_check": "A bot check blocked the file. Make sure FlareSolverr is on, or add the site's cookies.",
    "not_found": "The host deleted the file. Retrying won't help.",
    "quota": "The host's free transfer quota is used up. Try again in a few hours or sign in.",
    "disk_full": "Free up space on the download drive, then Retry unfinished.",
    "permission": "Choose a download folder you can write to, or a shorter folder path.",
    "corrupted": "The file will be downloaded again from the start.",
    "web_page": "Open the link in your browser: it is probably a login or error page.",
    "password": "Add ?password=THE_PASSWORD to the end of the link and start it again.",
    "unsupported": "This kind of link has no downloader in this build.",
    "other": "Details are in the run log.",
}

_STATUS = re.compile(r"(?<![\d.])([1-5]\d\d)(?![\d.])")
_EXPIRING_HOSTS = ("scdn.st", "bunkr", "cdn.cr", "gofile.io", "pixeldrain", "kemono", "coomer", "cyberdrop",
                   "mediafire", "dropbox", "googleusercontent", "fbcdn", "cdninstagram", "tiktokcdn",
                   "telegram", "redgifs")
_SIGNED_QUERY = re.compile(r"[?&](?:expires?|exp|e|x-expires|token|signature|sig|st|hash|md5|validfrom|validto|"
                           r"x-amz-signature|x-amz-expires|policy|key-pair-id)=", re.I)


def _status(reason: str) -> int | None:
    for match in _STATUS.finditer(reason or ""):
        code = int(match.group(1))
        if 400 <= code <= 599:
            return code
    return None


def looks_temporary_link(url: str) -> bool:
    """A CDN link that is signed or known to expire (Bunkr storage, GoFile, Kemono and similar)."""
    text = (url or "").lower()
    host = urlparse(text).hostname or ""
    return bool(_SIGNED_QUERY.search(text)) or any(name in host for name in _EXPIRING_HOSTS)


def classify(reason: str, url: str = "") -> str:
    """The category for one failure message (engine reason, HTTP status text or exception text)."""
    text = reason or ""
    low = text.lower()
    if "password" in low and "protected" in low:
        return "password"
    if "unsupported" in low or "unknown url path" in low or "no handler" in low or "no downloader" in low:
        return "unsupported"
    if "no space" in low or "insufficient free space" in low or "disk full" in low or "errno 28" in low:
        return "disk_full"
    if "permission denied" in low or "errno 13" in low or "access is denied" in low or "path too long" in low \
            or "winerror 206" in low:
        return "permission"
    if "web page instead" in low or "invalid content type" in low or "was expecting binary" in low:
        return "bot_check" if ("ddos" in low or "bot check" in low or "checking your browser" in low) else "web_page"
    if "ddos-guard" in low or "bot check" in low or "turnstile" in low or "cloudflare" in low or "anti-bot" in low:
        return "bot_check"
    if "corrupted" in low or "resume mismatch" in low or "incomplete transfer" in low or "not enough data" in low \
            or "contentlengtherror" in low or ("expected " in low and " bytes" in low):
        return "corrupted"
    if "server unreachable" in low or "file servers could not be reached" in low:
        return "unreachable"
    if "clientconnectordnserror" in low or "getaddrinfo failed" in low or "name or service not known" in low \
            or "nodename nor servname" in low or "dns lookup failed" in low or "could not resolve" in low:
        return "dns"
    if "509" in text or "bandwidth limit" in low:
        return "quota"
    status = _status(text)
    if status == 429 or "too many requests" in low or "kept asking to slow down" in low:
        return "rate_limited"
    if status in (403, 410) and looks_temporary_link(url):
        return "expired_url"
    if status in (404, 410) or (status is None and "not found" in low and "file not found" not in low):
        return "not_found"
    if status in (401, 403) or "do not have permission" in low or "forbidden" in low:
        return "auth_required"
    if status in (500, 502, 503, 504, 520, 521, 522, 523, 524) or "oops! we ran into some problems" in low \
            or "site paused" in low or "is paused for" in low \
            or "service unavailable" in low or "bad gateway" in low or "maintenance" in low:
        return "server_busy"
    if "timeout" in low or "timed out" in low or status == 408:
        return "timeout"
    if "connection reset" in low or "serverdisconnected" in low or "clientpayloaderror" in low \
            or "clientconnectorerror" in low or "connection aborted" in low or "broken pipe" in low \
            or "connection closed" in low or "clientoserror" in low:
        return "connection"
    return "other"


# Tried again by the retry pass at the end of a run. A bot check already had its FlareSolverr retry,
# a used-up quota or an unreachable site won't recover in a minute, so the user retries those later.
IN_RUN_RETRY = frozenset({"rate_limited", "server_busy", "timeout", "connection", "corrupted", "expired_url"})


def is_retryable(category: str) -> bool:
    """Worth trying again in this run's retry pass (an expired link goes back through its page)."""
    return category in IN_RUN_RETRY


def describe(category: str) -> dict:
    return {"category": category, "label": LABELS.get(category, LABELS["other"]),
            "action": ACTIONS.get(category, ACTIONS["other"]),
            "recoverable": category in RETRY_LATER or category in RETRY_NOW or category in REFRESH}
