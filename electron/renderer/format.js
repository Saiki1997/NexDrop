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
        let line = token.trim().replace(/^[<("']+|[>)"',;]+$/g, "");
        if (!line || line.startsWith("#")) continue;
        // Telegram: "t.me/..." without https://, and a chat ID such as -1001988700745
        if (/^(?:t\.me|telegram\.me|telegram\.dog)\//i.test(line)) line = `https://${line}`;
        const chatId = /^-100(\d{5,})$/.exec(line);
        if (chatId) line = `https://t.me/c/${chatId[1]}`;
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

  /** Compare "2.3.4" with "v2.4.0": negative, 0 or positive. Pre-release tags sort before the release. */
  function compareVersions(a, b) {
    const split = (v) => {
      const [main, pre = ""] = String(v || "").trim().replace(/^v/i, "").split("-", 2);
      return { parts: main.split(".").map((n) => parseInt(n, 10) || 0), pre };
    };
    const x = split(a);
    const y = split(b);
    for (let i = 0; i < Math.max(x.parts.length, y.parts.length, 3); i += 1) {
      const d = (x.parts[i] || 0) - (y.parts[i] || 0);
      if (d) return d;
    }
    if (x.pre === y.pre) return 0;
    if (!x.pre) return 1;
    if (!y.pre) return -1;
    return x.pre.localeCompare(y.pre);
  }

  /** "HH:MM" to minutes after midnight, or null. */
  function clockMinutes(text) {
    const match = /^(\d{1,2}):(\d{2})$/.exec(String(text || "").trim());
    if (!match) return null;
    const h = Number(match[1]);
    const m = Number(match[2]);
    return h < 24 && m < 60 ? h * 60 + m : null;
  }

  /** Is the time of `date` inside from–to? Windows may cross midnight (22:00–06:00). Equal ends mean all day. */
  function inTimeWindow(date, from, to) {
    const start = clockMinutes(from);
    const end = clockMinutes(to);
    if (start === null || end === null || start === end) return true;
    const now = date.getHours() * 60 + date.getMinutes();
    return start < end ? now >= start && now < end : now >= start || now < end;
  }

  /**
   * Links from a nexdrop:// address, as sent by the browser extension:
   * nexdrop://add?url=<encoded>&url=<encoded>. Only http(s) links, at most 50.
   */
  function appLinkUrls(address) {
    let parsed;
    try {
      parsed = new URL(String(address || ""));
    } catch {
      return [];
    }
    if (parsed.protocol !== "nexdrop:") return [];
    const urls = [];
    for (const value of parsed.searchParams.getAll("url")) {
      const url = String(value).trim();
      if (url.length <= 4096 && /^https?:\/\/[^\s/]+\.[^\s]+$/i.test(url) && !urls.includes(url)) urls.push(url);
      if (urls.length >= 50) break;
    }
    return urls;
  }

  /** The SHA-256 for `name` in a SHA256SUMS.txt ("<hash>  <name>" per line), lower case, or "". */
  function checksumFor(text, name) {
    for (const line of String(text || "").split(/\r?\n/)) {
      const match = /^([0-9a-fA-F]{64})\s+\*?(.+)$/.exec(line.trim());
      if (match && match[2].trim() === name) return match[1].toLowerCase();
    }
    return "";
  }

  /* Download modes: how many files at once, per site and per server. "custom" keeps the user's own numbers. */
  const DOWNLOAD_MODES = {
    auto: { maxDownloads: 16, perHost: 6, serverSlots: 3 },
    balanced: { maxDownloads: 8, perHost: 4, serverSlots: 2 },
    max: { maxDownloads: 24, perHost: 10, serverSlots: 4 },
    low: { maxDownloads: 3, perHost: 2, serverSlots: 1 },
  };

  /** The limits a run uses for `mode`; custom (or an unknown mode) uses the numbers in `custom`. */
  function modeLimits(mode, custom) {
    const preset = DOWNLOAD_MODES[mode];
    if (preset) return { ...preset };
    const pick = (value, def, max) => {
      const n = parseInt(value, 10);
      return Number.isFinite(n) ? Math.max(1, Math.min(max, n)) : def;
    };
    const own = custom || {};
    return { maxDownloads: pick(own.maxDownloads, 16, 32), perHost: pick(own.perHost, 6, 16), serverSlots: pick(own.serverSlots, 3, 6) };
  }

  /** The mode for settings saved before 3.0: the old defaults are Auto, anything changed is Custom. */
  function guessMode(prefs) {
    const p = prefs || {};
    const same = ["maxDownloads", "perHost", "serverSlots"].every((k) => p[k] === undefined || p[k] === DOWNLOAD_MODES.auto[k]);
    return same ? "auto" : "custom";
  }

  /* Filename templates (Smart Naming 3.0). Mirrors smart_naming.py so Settings can show an example. */
  const TEMPLATE_VARS = ["title", "original", "album", "thread", "post", "creator", "date", "site", "type", "number", "ext"];
  const NAME_VARS = ["title", "original", "album", "thread", "post", "creator"];
  const TOKEN = /\{([a-z]+)\}/g;

  /** Why a template can't be used, or "" when it can (or is empty). */
  function templateProblem(template) {
    const text = String(template || "").trim().replace(/\.\{ext\}$/i, "");
    if (!text) return "";
    if (text.length > 150) return "longer than 150 characters";
    const names = [...text.matchAll(TOKEN)].map((m) => m[1]);
    const unknown = names.find((n) => !TEMPLATE_VARS.includes(n));
    if (unknown) return `unknown variable {${unknown}}`;
    if (/[{}]/.test(text.replace(TOKEN, ""))) return "a { or } that is not part of a variable";
    if (/[<>:"/\\|?*]/.test(text)) return 'a character Windows does not allow in filenames (< > : " / \\ | ? *)';
    if (!names.some((n) => NAME_VARS.includes(n))) return "no name variable ({title}, {original}, {album}, {thread}, {post} or {creator})";
    return "";
  }

  /** The filename stem for `values`; "" when every name variable is empty (the automatic name is used). */
  function renderTemplate(template, values, number) {
    const text = String(template || "").trim().replace(/\.\{ext\}$/i, "");
    const edge = " -_.,";
    const pieces = [];
    let pos = 0;
    let named = false;
    for (const match of text.matchAll(TOKEN)) {
      const literal = text.slice(pos, match.index);
      const key = match[1];
      const value = key === "number" ? number || "" : key === "ext" ? "" : (values || {})[key] || "";
      if (value && NAME_VARS.includes(key)) named = true;
      if (value) pieces.push(literal, value);
      else if (!pieces.length && [...literal].some((c) => !edge.includes(c))) pieces.push(literal);
      pos = match.index + match[0].length;
    }
    if (!named) return "";
    pieces.push(text.slice(pos));
    let stem = pieces.join("").replace(/([ \-_.,])\1+/g, "$1");
    while (stem && edge.includes(stem[0])) stem = stem.slice(1);
    while (stem && edge.includes(stem[stem.length - 1])) stem = stem.slice(0, -1);
    return stem;
  }

  return { TEMPLATE_VARS, templateProblem, renderTemplate, DOWNLOAD_MODES, modeLimits, guessMode, bytes, speed, mbps, duration, ago, parseLinks, host, finishText, compareVersions, clockMinutes, inTimeWindow, appLinkUrls, checksumFor };
});
