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
  // Query parameters that only track where a click came from (3.1 duplicate link detection).
  const TRACKING = /^(utm_[a-z]+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|ref_src|ref_url|_ga|yclid|share_id)$/i;
  // Share trackers that mean nothing else only on these sites ("s" is a real search on others).
  const SITE_TRACKING = { "x.com": ["s", "t"], "twitter.com": ["s", "t"], "youtube.com": ["si", "pp", "feature"], "youtu.be": ["si", "feature"], "open.spotify.com": ["si"], "instagram.com": ["igsh", "img_index"] };

  /** The same link without tracking parameters, fragment, "www." or a trailing slash. */
  function normalizeUrl(raw) {
    let u;
    try {
      u = new URL(String(raw).trim());
    } catch {
      return String(raw || "").trim();
    }
    const hostName = u.hostname.toLowerCase().replace(/^www\./, "");
    const drop = SITE_TRACKING[hostName] || [];
    const params = [...u.searchParams].filter(([k]) => !TRACKING.test(k) && !drop.includes(k));
    const query = params.length ? `?${new URLSearchParams(params).toString()}` : "";
    const fragment = u.hash.startsWith("#!") ? u.hash : "";
    const pathName = u.pathname.replace(/\/+$/, "") || "/";
    return `${u.protocol}//${hostName}${u.port ? `:${u.port}` : ""}${pathName}${query}${fragment}`;
  }

  /** "host = N" lines (Settings > Speed > Per-site limits) to { limits: {host: N}, bad: [line] }. */
  function parseSiteLimits(text) {
    const limits = {};
    const bad = [];
    for (const raw of String(text || "").split(/\r?\n/)) {
      const line = raw.trim();
      if (!line || line.startsWith("#")) continue;
      const match = /^(?:https?:\/\/)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})\/?\s*[=:]\s*(\d+)$/i.exec(line);
      const n = match ? parseInt(match[2], 10) : NaN;
      if (match && n >= 1 && n <= 32) limits[match[1].toLowerCase()] = n;
      else bad.push(line);
    }
    return { limits, bad };
  }

  function parseLinks(text) {
    const urls = [];
    const unsupported = [];
    const ignored = [];
    const duplicates = [];
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
          const key = normalizeUrl(line);
          if (!seen.has(key)) {
            seen.add(key);
            urls.push(line);
          } else if (line !== urls.find((u) => normalizeUrl(u) === key)) {
            duplicates.push(line); // same link with other tracking parameters
          }
        } else if (line.length > 1) {
          ignored.push(line);
        }
      }
    }
    return { urls, unsupported, ignored, duplicates };
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
    auto: { maxDownloads: 16, perHost: 6, serverSlots: 3, segments: 4 },
    balanced: { maxDownloads: 8, perHost: 4, serverSlots: 2, segments: 2 },
    max: { maxDownloads: 24, perHost: 10, serverSlots: 4, segments: 6 },
    low: { maxDownloads: 3, perHost: 2, serverSlots: 1, segments: 0 },
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
    const parts = parseInt(own.segments, 10);
    return { maxDownloads: pick(own.maxDownloads, 16, 32), perHost: pick(own.perHost, 6, 16), serverSlots: pick(own.serverSlots, 3, 6),
      segments: Number.isFinite(parts) ? Math.max(0, Math.min(8, parts)) : 4 };
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

  /* Jobs (3.0 job manager): what is kept in an export, and reading an export or a link list back. */
  const JOB_FIELDS = ["label", "urls", "unsupported", "startPage", "endPage", "redownload", "folder", "mode", "speedMB", "hold", "named"];
  const MAX_JOBS = 500;
  const MAX_JOB_URLS = 20000;

  function sameFolder(a, b) {
    const norm = (p) => String(p || "").replace(/[\\/]+$/, "").replace(/\\/g, "/").toLowerCase();
    return Boolean(a) && norm(a) === norm(b);
  }

  /** A queued job as it is saved and exported: only known fields, checked. Returns null when it has no links. */
  function cleanJob(raw, knownDirs) {
    if (!raw || typeof raw !== "object") return null;
    const links = parseLinks([...(Array.isArray(raw.urls) ? raw.urls : []), ...(Array.isArray(raw.unsupported) ? raw.unsupported : [])].map(String).join("\n"));
    const urls = links.urls.slice(0, MAX_JOB_URLS);
    if (!urls.length && !links.unsupported.length) return null;
    const int = (v, lo, hi) => {
      const n = parseInt(v, 10);
      return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : null;
    };
    const job = { urls, unsupported: links.unsupported.slice(0, 1000) };
    if (typeof raw.label === "string" && raw.label.trim()) job.label = raw.label.trim().slice(0, 120);
    job.startPage = int(raw.startPage, 1, 100000) || 1;
    job.endPage = int(raw.endPage, 1, 100000);
    if (job.endPage && job.endPage < job.startPage) job.endPage = null;
    job.redownload = Boolean(raw.redownload);
    if (typeof raw.folder === "string" && raw.folder && (!knownDirs || knownDirs.some((d) => sameFolder(d, raw.folder)))) job.folder = raw.folder;
    if (DOWNLOAD_MODES[raw.mode]) job.mode = raw.mode;
    const speed = int(raw.speedMB, 0, 1000);
    if (speed !== null && raw.speedMB !== "" && raw.speedMB !== null) job.speedMB = speed;
    if (raw.hold) job.hold = true;
    if (raw.named) job.named = true;
    return job;
  }

  /** Jobs from an imported file: a NexDrop jobs export, a JSON list of links, or any text with links in it
   * (.txt, .csv, .m3u). `dropped` counts jobs without links and folders not chosen on this PC. */
  function parseJobFile(name, text, knownDirs) {
    const body = String(text || "").replace(/^\uFEFF/, "");
    let data = null;
    if (/\.json$/i.test(name || "") || /^\s*[[{]/.test(body)) {
      try {
        data = JSON.parse(body);
      } catch {
        data = null;
      }
    }
    const jobs = [];
    let dropped = 0;
    if (data && typeof data === "object" && !Array.isArray(data) && Array.isArray(data.jobs)) {
      for (const raw of data.jobs.slice(0, MAX_JOBS)) {
        const job = cleanJob(raw, knownDirs);
        if (!job) dropped += 1;
        else {
          if (raw && raw.folder && !job.folder) dropped += 1;
          jobs.push(job);
        }
      }
      return { jobs, dropped, kind: "jobs" };
    }
    let lines = [];
    if (Array.isArray(data)) lines = data.map((item) => (item && typeof item === "object" ? item.url : item)).filter((v) => typeof v === "string");
    else lines = body.match(/https?:\/\/[^\s"'<>,;]+/gi) || [];
    const job = cleanJob({ urls: lines });
    if (job) jobs.push(job);
    return { jobs, dropped, kind: "links" };
  }

  /** The fields of a job worth keeping in an export. */
  function exportJob(job) {
    const out = {};
    for (const key of JOB_FIELDS) if (job[key] !== undefined && job[key] !== null && job[key] !== "") out[key] = job[key];
    return out;
  }

  /** Text safe to share in a diagnostics report: no tokens, passwords, cookies, e-mail addresses or user name. */
  function redact(text, home) {
    let out = String(text || "");
    // cookie files: drop every cookie line, keep the comments
    out = out.replace(/^[^\s#][^\t\n]*\t(TRUE|FALSE)\t[^\t\n]*\t(TRUE|FALSE)\t\d+\t[^\t\n]*\t[^\n]*$/gim, "[cookie removed]");
    out = out.replace(/\b(authorization|cookie|set-cookie|x-api-key)\s*[:=]\s*[^\n]+/gi, "$1: [removed]");
    out = out.replace(/([?&;\s"'](?:[a-z0-9_-]*(?:token|sig|signature|secret|password|passwd|pass|auth|session|sess|key|hash|code|cookie|xf_user|expires?)[a-z0-9_-]*))=([^&\s"'#]+)/gi, "$1=[removed]");
    out = out.replace(/("(?:[a-z0-9_]*(?:token|secret|password|api_key|apikey|cookie|session)[a-z0-9_]*)"\s*:\s*)"[^"]*"/gi, '$1"[removed]"');
    out = out.replace(/(https?:\/\/)[^/\s:@]+:[^/\s@]+@/gi, "$1[removed]@");
    out = out.replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[email]");
    if (home) {
      const escaped = String(home).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      out = out.replace(new RegExp(escaped, "gi"), "~");
      const json = JSON.stringify(String(home)).slice(1, -1).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      out = out.replace(new RegExp(json, "gi"), "~");
    }
    return out;
  }

  return { parseSiteLimits, normalizeUrl, redact, cleanJob, parseJobFile, exportJob, TEMPLATE_VARS, templateProblem, renderTemplate, DOWNLOAD_MODES, modeLimits, guessMode, bytes, speed, mbps, duration, ago, parseLinks, host, finishText, compareVersions, clockMinutes, inTimeWindow, appLinkUrls, checksumFor };
});
