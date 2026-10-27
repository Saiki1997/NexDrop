"""Connection check for one site (Settings > Speed > Test a site), 3.1 network diagnostics.

    net_check.py      stdin: {"url": "https://site/...", "proxy": "http://host:port" or ""}

Prints one JSON object with each step and how long it took: DNS lookup, TCP connect (to the
proxy when one is set), and one HEAD request over HTTPS. One small request; nothing downloaded.
"""

from __future__ import annotations

import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def check(url: str, proxy: str = "", blocked=None) -> dict:
    if "://" not in url:
        url = "https://" + url
    parts = urlparse(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    steps: list[dict] = []
    out = {"ok": False, "host": host, "steps": steps, "proxy": bool(proxy)}
    if not host or parts.scheme not in ("http", "https"):
        out["error"] = "Enter a web address such as https://bunkr.cr"
        return out
    reason = blocked(host) if blocked else None
    if reason and "resolve" not in reason:
        out["error"] = reason
        return out

    start = time.perf_counter()
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
        steps.append({"step": "DNS lookup", "ok": True, "ms": _ms(start), "detail": ", ".join(addresses[:4])})
    except socket.gaierror as error:
        steps.append({"step": "DNS lookup", "ok": False, "ms": _ms(start),
                      "detail": f"{host} could not be found ({error.strerror or error}). Check the address, your internet connection or DNS settings."})
        return out

    target = urlparse(proxy) if proxy else None
    connect_host, connect_port = (target.hostname, target.port or 8080) if target else (host, port)
    start = time.perf_counter()
    try:
        with socket.create_connection((connect_host, connect_port), timeout=10):
            pass
        steps.append({"step": "Connect" + (" to the proxy" if proxy else ""), "ok": True, "ms": _ms(start),
                      "detail": f"{connect_host}:{connect_port}"})
    except OSError as error:
        steps.append({"step": "Connect" + (" to the proxy" if proxy else ""), "ok": False, "ms": _ms(start),
                      "detail": f"{connect_host}:{connect_port} did not answer ({error.strerror or error}). A firewall, VPN or the site's block can cause this."})
        return out

    handlers: list = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(f"{parts.scheme}://{parts.netloc}{parts.path or '/'}", method="HEAD", headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"})
    start = time.perf_counter()
    status = 0
    try:
        with opener.open(request, timeout=20) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    except ssl.SSLError as error:
        steps.append({"step": "Secure connection", "ok": False, "ms": _ms(start), "detail": f"TLS failed: {error.reason or error}"})
        return out
    except (urllib.error.URLError, OSError) as error:
        steps.append({"step": "Request", "ok": False, "ms": _ms(start), "detail": str(getattr(error, "reason", error))[:200]})
        return out
    meaning = {200: "the site answers", 301: "it redirects", 302: "it redirects", 403: "refused: a sign-in or browser check may be needed",
               404: "the site answers (this page is not there)", 405: "the site answers (HEAD not allowed)",
               429: "the site asks to slow down", 503: "busy or a browser check (Cloudflare/DDoS-Guard)"}
    steps.append({"step": "Request", "ok": status < 500 and status not in (403, 429), "ms": _ms(start),
                  "detail": f"HTTP {status}: {meaning.get(status, 'answered')}"})
    out["ok"] = all(step["ok"] for step in steps)
    out["status"] = status
    return out


def main() -> int:
    try:
        payload = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        payload = {}
    from cairn_session import blocked_reason, proxy_url

    print(json.dumps(check(str(payload.get("url") or "").strip(), proxy_url(payload.get("proxy")), blocked_reason)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
