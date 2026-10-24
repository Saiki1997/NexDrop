"""Test-crawl one page of a site in Settings > Sites, without downloading anything.

    python site_check.py <url> [--items <css>] [--next <css>]

Prints one JSON object: what the generic media handler would find on that page
(media by type, a few examples, notes such as DRM or DASH, item links and the
next page). Refuses sites NEXDROP assessed as unsupported, and private or local
addresses. Used to move a site from "review" to "supported".
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "engine" / "vendor"))

import site_policy  # noqa: E402

MAX_PAGE = 5 * 1024 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        return None


def fetch(url: str) -> tuple[str, str]:
    from cairn_session import public_url  # private-address and credential checks, per hop

    opener = urllib.request.build_opener(_NoRedirect)
    current = public_url(url)
    for _ in range(6):
        request = urllib.request.Request(current, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
        try:
            response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        status = response.getcode()
        if status in {301, 302, 303, 307, 308}:
            current = public_url(urljoin(current, response.headers.get("Location") or ""))
            reason = site_policy.blocked_reason(current)
            if reason.startswith("Unsupported"):
                raise PermissionError(f"The page redirects to an unsupported site. {reason}")
            continue
        if status in {401, 403}:
            raise PermissionError(f"HTTP {status}: the page needs a sign-in or is restricted. NEXDROP does not get around that.")
        if status >= 400:
            raise ValueError(f"HTTP {status}")
        body = response.read(MAX_PAGE + 1)
        if len(body) > MAX_PAGE:
            raise ValueError("The page is over 5 MB; is this a file rather than a page?")
        charset = response.headers.get_content_charset() or "utf-8"
        return current, body.decode(charset, errors="replace")
    raise ValueError("Too many redirects")


def check(url: str, items: str = "", next_selector: str = "") -> dict:
    reason = site_policy.blocked_reason(url)
    if reason.startswith("Unsupported"):
        return {"ok": False, "state": "unsupported", "message": reason}
    from bs4 import BeautifulSoup
    from cyberdrop_dl.crawlers._generic_media import find_media, page_title, same_host_links

    try:
        final, html = fetch(url)
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "message": str(error)[:300]}
    soup = BeautifulSoup(html, "html.parser")
    media, notes = find_media(soup, final)
    counts: dict[str, int] = {}
    for entry in media:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    item_links = same_host_links(soup, items, final) if items else []
    next_links = same_host_links(soup, next_selector or 'link[rel="next"], a[rel="next"]', final)
    found = len(media) + len(item_links)
    message = (f"Found {len(media)} file(s)" + (f" and {len(item_links)} item page(s)" if items else "")
               + (", and a next page" if next_links else "") + "." if found
               else "Nothing downloadable on this page. Try a page with a video or gallery, or set an item selector for listing pages.")
    return {
        "ok": bool(found), "url": final, "title": page_title(soup, ""), "counts": counts,
        "samples": [m.url for m in media[:5]], "notes": notes, "itemLinks": len(item_links),
        "nextPage": next_links[0] if next_links else "", "message": message,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--items", default="")
    parser.add_argument("--next", default="")
    args = parser.parse_args()
    print(json.dumps(check(args.url, args.items, args.next)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
