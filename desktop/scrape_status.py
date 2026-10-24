"""Classify a scrape failure. This does not decide that a site is unsupported."""

from __future__ import annotations


def classify_scrape_message(message: str) -> str:
    text = message.casefold()
    if "max children" in text or "depth limit" in text or "nested thread" in text:
        return "A nested thread was skipped because the depth limit was reached."
    if "requires a login" in text or "failed login" in text or "no cookies" in text or "cookies for" in text:
        return "This site requires a login. Add a cookie file for this site and try again."
    if "missing or private" in text or "not found" in text or "do not have permission" in text:
        return "The thread is missing or private. It was not downloaded."
    if "bot check" in text or "ddos" in text:
        return "The site returned a bot check instead of the thread. Nothing was queued."
    if "no posts" in text:
        return "The thread page contained no posts, so nothing was queued."
    if "no attachments" in text or "no attachments or media" in text:
        return "The thread had posts but no attachments or media links."
    if "timeout" in text or "timed out" in text:
        return "The request timed out. The thread was not downloaded."
    if "429" in text or "rate limit" in text or "slow down" in text:
        return "The forum asked us to slow down. Nothing was queued."
    if message.startswith("Scrape Failed:"):
        return message[:300]
    return "The crawl failed. Nothing was queued."
