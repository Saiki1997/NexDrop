/* Pure helpers shared by the window and the tests. No DOM access. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.fmt = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function bytes(n) {
    if (!Number.isFinite(n) || n <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = n;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = unit === 0 ? 0 : value >= 100 ? 0 : value >= 10 ? 1 : 2;
    return `${value.toFixed(digits)} ${units[unit]}`;
  }

  function speed(n) {
    return Number.isFinite(n) && n > 0 ? `${bytes(n)}/s` : "–";
  }

  /** Speed in megabytes per second, e.g. "3.42 MB/s". */
  function mbps(n) {
    if (!Number.isFinite(n) || n <= 0) return "0.00 MB/s";
    const mb = n / 1048576;
    return `${mb >= 100 ? mb.toFixed(0) : mb >= 10 ? mb.toFixed(1) : mb.toFixed(2)} MB/s`;
  }

  function duration(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const rest = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${rest}` : `${m}:${rest}`;
  }

  function ago(iso, now = Date.now()) {
    const t = Date.parse(iso || "");
    if (!Number.isFinite(t)) return "";
    const minutes = Math.round((now - t) / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} h ago`;
    const days = Math.round(hours / 24);
    if (days < 30) return days === 1 ? "yesterday" : `${days} days ago`;
    return new Date(t).toLocaleDateString();
  }

  /** Split pasted text into downloadable links, rejected torrent links, and anything else. */
  function parseLinks(text) {
    const urls = [];
    const unsupported = [];
    const ignored = [];
    const seen = new Set();
    for (const raw of String(text || "").split(/[\r\n]+/)) {
      if (raw.trim().startsWith("#")) continue; // comment line in an imported .txt
      for (const token of raw.trim().split(/\s+/)) {
        const line = token.trim().replace(/^[<("']+|[>)"',;]+$/g, "");
        if (!line || line.startsWith("#")) continue;
        if (/^magnet:/i.test(line) || /^https?:\/\/\S+\.torrent(?:$|\?)/i.test(line)) {
          unsupported.push(line);
        } else if (/^https?:\/\/[^\s/]+\.[^\s]+/i.test(line)) {
          if (!seen.has(line)) {
            seen.add(line);
            urls.push(line);
          }
        } else if (line.length > 1) {
          ignored.push(line);
        }
      }
    }
    return { urls, unsupported, ignored };
  }

  function host(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch {
      return "";
    }
  }

  /** The closing line for a finished run. */
  function finishText(event) {
    const saved = Number(event.saved || 0);
    const failed = Number(event.failed || 0);
    const skipped = Number(event.skipped || 0);
    if (event.stopped) {
      return { tone: "warning", message: `Stopped. ${saved} saved before stopping.` };
    }
    if (saved === 0 && failed === 0 && skipped === 0) {
      return { tone: "warning", message: String(event.emptyReason || "").trim() || "Finished, but no files were found." };
    }
    const parts = [`${saved} saved`];
    if (skipped) parts.push(`${skipped} already saved before`);
    if (failed) parts.push(`${failed} failed`);
    return { tone: failed ? "warning" : "ok", message: `Finished. ${parts.join(", ")}.` };
  }

  return { bytes, speed, mbps, duration, ago, parseLinks, host, finishText };
});
