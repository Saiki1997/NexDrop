"use strict";

const { app, BrowserWindow, dialog, ipcMain, shell, session, safeStorage, nativeImage, Notification } = require("electron");
const { spawn, spawnSync } = require("child_process");
const { StringDecoder } = require("string_decoder");
const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");
const { createVault, CATALOG } = require("./credential-store");

// ─── paths ──────────────────────────────────────────────────────────────────

const ROOT = (() => {
  const candidates = [
    path.dirname(process.execPath),
    path.resolve(__dirname, "..", ".."),
    path.resolve(__dirname, ".."),
    path.resolve(process.resourcesPath || __dirname, ".."),
  ];
  return candidates.find((root) => fs.existsSync(path.join(root, "engine", "bridge.py"))) || path.resolve(__dirname, "..");
})();
const APPDATA = path.join(ROOT, "engine", "appdata");
const SETTINGS_FILE = path.join(APPDATA, "gui-settings.json");
const LOG_FILE = path.join(APPDATA, "logs", "gui.log");
const RUNS_FILE = path.join(APPDATA, "runs.json");
const ACCOUNT_COOKIES = path.join(APPDATA, "auth-cookies");
const SOURCE_LOGS = path.join(ROOT, "logs");
const PYTHON = [
  path.join(ROOT, "runtime", "python", "python.exe"),
  path.join(ROOT, "runtime", "python", "pythonw.exe"),
  path.join(ROOT, "engine", ".venv", "Scripts", "python.exe"),
  path.join(ROOT, "engine", ".venv", "bin", "python"),
].find((item) => fs.existsSync(item)) || (process.platform === "win32" ? "python.exe" : "python3");
const SETTING_KEYS = new Set(["downloads_dir", "cookie_files", "prefs", "window", "queue", "watches", "active_run", "thread_posts"]);
const LOG_DIR = path.join(APPDATA, "logs");
const FLARE_BIN = ["flaresolverr.exe", "flaresolverr"].map((n) => path.join(ROOT, "runtime", "flaresolverr", n)).find((f) => fs.existsSync(f)) || "";
const FLARE_URL = "http://127.0.0.1:8191/v1";
const STOP_GRACE_MS = 6000;

let mainWindow = null;

// ─── settings and log ───────────────────────────────────────────────────────

function readSettings() {
  try {
    return JSON.parse(fs.readFileSync(SETTINGS_FILE, "utf8"));
  } catch {
    return {};
  }
}

function writeSettings(patch) {
  const next = { ...readSettings(), ...patch };
  fs.mkdirSync(path.dirname(SETTINGS_FILE), { recursive: true });
  const temp = `${SETTINGS_FILE}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(next, null, 2));
  fs.renameSync(temp, SETTINGS_FILE);
  return next;
}

const logFile = (() => {
  let stream = null;
  return {
    write(line) {
      try {
        if (!stream) {
          fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
          // Keep one previous log; the active one restarts above 5 MB.
          if (fs.existsSync(LOG_FILE) && fs.statSync(LOG_FILE).size > 5 * 1024 * 1024) {
            fs.renameSync(LOG_FILE, `${LOG_FILE}.1`);
          }
          stream = fs.createWriteStream(LOG_FILE, { flags: "a" });
          stream.on("error", () => { stream = null; });
        }
        stream.write(`${new Date().toISOString()} ${line}\n`);
      } catch {
        /* the activity feed still receives the line */
      }
    },
    close() {
      stream?.end();
      stream = null;
    },
  };
})();

function downloadsDir() {
  const saved = readSettings().downloads_dir;
  if (saved && typeof saved === "string") return saved;
  const dir = path.join(ROOT, "downloads", "nexdrop");
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function existingFile(target) {
  try {
    return target && typeof target === "string" && fs.statSync(target).isFile() ? target : "";
  } catch {
    return "";
  }
}

// ─── window ─────────────────────────────────────────────────────────────────

function send(payload) {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("pipeline-event", payload);
}

function sendLog(message, level = "info") {
  logFile.write(`[${level}] ${message}`);
  send({ type: "log", level, message });
}

function appIcon() {
  for (const name of ["icon.png", "logo.png", "logo.jpg"]) {
    const file = path.join(__dirname, "renderer", "assets", name);
    if (fs.existsSync(file)) {
      const image = nativeImage.createFromPath(file);
      if (!image.isEmpty()) return image;
    }
  }
  return undefined;
}

function createWindow() {
  const saved = readSettings().window || {};
  const options = {
    width: Number(saved.width) > 400 ? Number(saved.width) : 1320,
    height: Number(saved.height) > 300 ? Number(saved.height) : 860,
    minWidth: 960,
    minHeight: 640,
    title: "NEXDROP",
    icon: appIcon(),
    autoHideMenuBar: true,
    backgroundColor: "#0b0d12",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      spellcheck: false,
    },
  };
  if (Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
    options.x = saved.x;
    options.y = saved.y;
  }
  mainWindow = new BrowserWindow(options);
  mainWindow.once("ready-to-show", () => {
    if (saved.maximized) mainWindow.maximize();
    mainWindow.show();
  });
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  mainWindow.on("close", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    try {
      writeSettings({ window: { ...mainWindow.getNormalBounds(), maximized: mainWindow.isMaximized() } });
    } catch {
      /* window position is a convenience */
    }
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith("file:")) event.preventDefault();
  });
  mainWindow.webContents.on("did-fail-load", (_event, code, description) => {
    sendLog(`Interface failed to load (${code}): ${description}`, "error");
  });
  mainWindow.on("focus", () => mainWindow.flashFrame(false));
  mainWindow.on("closed", () => {
    killSession(true);
    mainWindow = null;
  });
}

// ─── python helpers ─────────────────────────────────────────────────────────

/** Run a helper script without blocking the window. Resolves with stdout/stderr. */
function runPython(args, { input = "", timeout = 60000 } = {}) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(PYTHON, ["-u", ...args], { cwd: ROOT, windowsHide: true, env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
    } catch (error) {
      resolve({ code: -1, stdout: "", stderr: String(error) });
      return;
    }
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => killTree(child.pid, true), timeout);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (text) => { stdout += text; });
    child.stderr.on("data", (text) => { stderr += text; });
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ code: -1, stdout, stderr: stderr || String(error) });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
    child.stdin.end(input);
  });
}

function parseJson(text, fallback) {
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

function killTree(pid, force) {
  if (!pid) return;
  if (process.platform === "win32") {
    const args = ["/PID", String(pid), "/T"];
    if (force) args.push("/F");
    spawnSync("taskkill", args, { windowsHide: true, stdio: "ignore" });
    return;
  }
  const signal = force ? "SIGKILL" : "SIGTERM";
  try {
    process.kill(-pid, signal);
  } catch {
    try {
      process.kill(pid, signal);
    } catch {
      /* already gone */
    }
  }
}

// ─── FlareSolverr (bot-check solver) ────────────────────────────────────────
// Started when the window opens and kept running between runs, so its slow
// first start (it launches its own Chrome) happens while links are pasted.

const flare = { child: null, state: "off", message: "", timer: null, startedAt: 0 };

function flareAlive() {
  return new Promise((resolve) => {
    const http = require("http");
    const body = '{"cmd":"sessions.list"}';
    const req = http.request(FLARE_URL, { method: "POST", headers: { "Content-Type": "application/json", "Content-Length": body.length }, timeout: 3000 }, (res) => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", (c) => { text += c; });
      res.on("end", () => resolve(res.statusCode === 200 && text.includes('"status"')));
    });
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
    req.end(body);
  });
}

function setFlare(state, message = "") {
  flare.state = state;
  flare.message = message;
  send({ type: "flare", state, message });
  if (state === "ready" || state === "failed") logFile.write(`[flaresolverr] ${state} ${message}`);
}

function flareTail() {
  try {
    const text = fs.readFileSync(path.join(LOG_DIR, "flaresolverr.log"), "utf8").trim().split(/\r?\n/);
    return text.slice(-3).join(" | ").slice(0, 400);
  } catch {
    return "";
  }
}

async function startFlare() {
  if (flare.child || flare.state === "starting") return;
  if (!FLARE_BIN) return setFlare("missing", "FlareSolverr is not in runtime\\flaresolverr.");
  if (await flareAlive()) return setFlare("ready", "Already running on port 8191.");
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const out = fs.openSync(path.join(LOG_DIR, "flaresolverr.log"), "a");
  fs.writeSync(out, `\n--- started by NEXDROP ${new Date().toISOString()} ---\n`);
  let child;
  try {
    child = spawn(FLARE_BIN, [], {
      cwd: path.dirname(FLARE_BIN),
      env: { ...process.env, PORT: "8191", HOST: "127.0.0.1", LOG_LEVEL: "info", LOG_HTML: "false", HEADLESS: "true" },
      stdio: ["ignore", out, out],
      windowsHide: true,
      detached: process.platform !== "win32",
    });
  } catch (error) {
    fs.closeSync(out);
    return setFlare("failed", `Could not start: ${error.message}`);
  }
  fs.closeSync(out);
  flare.child = child;
  flare.startedAt = Date.now();
  setFlare("starting", "First start can take a minute or two.");
  child.on("error", (error) => {
    flare.child = null;
    setFlare("failed", `Could not start: ${error.message}. Antivirus may have blocked runtime\\flaresolverr\\flaresolverr.exe.`);
  });
  child.on("exit", (code) => {
    if (flare.child !== child) return;
    flare.child = null;
    clearInterval(flare.timer);
    if (flare.state !== "off") setFlare("failed", `Stopped (code ${code}). ${flareTail()}`);
  });
  clearInterval(flare.timer);
  flare.timer = setInterval(async () => {
    if (!flare.child) return clearInterval(flare.timer);
    if (await flareAlive()) {
      clearInterval(flare.timer);
      setFlare("ready", `Ready after ${Math.round((Date.now() - flare.startedAt) / 1000)} s.`);
    } else if (Date.now() - flare.startedAt > 240000) {
      clearInterval(flare.timer);
      setFlare("failed", `Not ready after 4 minutes. ${flareTail()}`);
    }
  }, 2000);
}

function stopFlare() {
  clearInterval(flare.timer);
  const child = flare.child;
  flare.child = null;
  if (child) killTree(child.pid, true);
  setFlare("off");
}

// ─── taskbar progress and "finished" notification ───────────────────────────

const progress = { label: "" };

function taskbar(event) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (event.type === "run") {
    const first = (event.urls || [])[0] || "";
    try {
      const u = new URL(first);
      progress.label = decodeURIComponent(u.pathname.split("/").filter(Boolean).slice(-1)[0] || u.hostname).replace(/\.\d+$/, "");
    } catch {
      progress.label = "";
    }
    mainWindow.setProgressBar(2); // indeterminate until files are known
  } else if (event.type === "files" && event.total) {
    const c = event.counts || {};
    const finished = (c.done || 0) + (c.skipped || 0) + (c.failed || 0) + (c.cancelled || 0);
    mainWindow.setProgressBar(Math.min(1, finished / event.total), { mode: c.failed ? "error" : "normal" });
  } else if (event.type === "finished") {
    mainWindow.setProgressBar(-1);
    notifyFinished(event);
  }
}

function notifyFinished(event) {
  const parts = [`${event.saved || 0} saved`];
  if (event.skipped) parts.push(`${event.skipped} already saved`);
  if (event.failed) parts.push(`${event.failed} failed`);
  const title = event.stopped ? "Download paused" : event.failed ? "Download finished with errors" : "Download finished";
  const body = `${progress.label ? `${progress.label}: ` : ""}${parts.join(", ")}.`;
  if (!mainWindow.isFocused()) mainWindow.flashFrame(true);
  if (Notification.isSupported() && !mainWindow.isFocused()) {
    const note = new Notification({ title, body, icon: appIcon(), silent: false });
    note.on("click", () => {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    });
    note.show();
  }
}

// ─── download session ───────────────────────────────────────────────────────

let current = null; // { id, child, stopTimer }
let sessionCounter = 0;

function killSession(force) {
  const run = current;
  if (!run) return;
  clearTimeout(run.stopTimer);
  if (force) {
    current = null;
    killTree(run.child.pid, true);
    return;
  }
  // Ask the session to stop; it cancels the engine, saves history and reports "finished".
  try {
    run.child.stdin.write("stop\n");
  } catch {
    /* pipe already closed */
  }
  run.stopTimer = setTimeout(() => {
    if (current === run && run.child.exitCode === null) killTree(run.child.pid, true);
  }, STOP_GRACE_MS);
}

function accountVault() {
  return createVault(
    path.join(APPDATA, "credentials.json"),
    (text) => {
      if (!safeStorage.isEncryptionAvailable()) {
        throw new Error("Secure storage is not available on this computer. Nothing was saved.");
      }
      return safeStorage.encryptString(text).toString("base64");
    },
    (blob) => safeStorage.decryptString(Buffer.from(blob, "base64")),
  );
}

function startSession(config) {
  if (current) killSession(true);
  const script = path.join(ROOT, "desktop", "cairn_session.py");
  if (!fs.existsSync(script)) {
    sendLog("desktop/cairn_session.py is missing. Keep the desktop folder beside NexDrop.exe.", "error");
    send({ type: "error", message: "The download engine adapter is missing." });
    send({ type: "exit", code: 1 });
    return;
  }
  const id = ++sessionCounter;
  const child = spawn(PYTHON, ["-u", script], {
    cwd: ROOT,
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      PYTHONIOENCODING: "utf-8",
      NEXDROP_PYTHON: PYTHON,
      NEXDROP_FLARE_MANAGED: flare.child || flare.state === "ready" ? "1" : "0",
    },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
    detached: process.platform !== "win32",
  });
  const run = { id, child, stopTimer: null };
  current = run;
  const live = () => current === run;

  // UTF-8 safe line splitting: a multi-byte character can straddle two chunks.
  const decoder = new StringDecoder("utf8");
  let buffer = "";
  const handleLines = (text) => {
    buffer += text;
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    if (!live()) return;
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const event = JSON.parse(trimmed);
        if (event.type === "log" && event.level !== "info") logFile.write(`[${event.level}] ${event.message}`);
        taskbar(event);
        send(event);
      } catch {
        sendLog(trimmed.slice(0, 300), "warning");
      }
    }
  };
  child.stdout.on("data", (chunk) => handleLines(decoder.write(chunk)));
  child.stdout.on("end", () => handleLines(decoder.end() + "\n"));
  const errDecoder = new StringDecoder("utf8");
  child.stderr.on("data", (chunk) => {
    const text = errDecoder.write(chunk).trim();
    if (!text || !live()) return;
    if (text.includes("DeprecationWarning")) return;
    logFile.write(`[stderr] ${text.slice(0, 2000)}`);
    send({ type: "log", level: "error", message: text.slice(0, 500) });
  });
  let reported = false;
  const reportExit = (code) => {
    if (reported) return;
    reported = true;
    clearTimeout(run.stopTimer);
    if (!live()) return;
    current = null;
    send({ type: "exit", code });
  };
  child.on("error", (error) => {
    if (live()) sendLog(`Could not start the bundled Python (${error.message}).`, "error");
    reportExit(1);
  });
  child.on("close", (code) => reportExit(code));

  const outgoing = { ...(config || {}) };
  for (const key of ["auth", "apiKey", "password", "cookieFolder"]) delete outgoing[key];
  try {
    const saved = accountVault().engineAuth();
    if (Object.keys(saved).length) outgoing.auth = saved;
  } catch {
    /* a missing vault must not block a download that needs no account */
  }
  try {
    const telegram = accountVault().telegramAuth();
    if (telegram) outgoing.telegram = telegram;
  } catch {
    /* without it, Telegram links use the public preview */
  }
  if (fs.existsSync(ACCOUNT_COOKIES)) outgoing.cookieFolder = ACCOUNT_COOKIES;
  // stdin stays open so "stop" can be sent later.
  child.stdin.on("error", () => {});
  child.stdin.write(`${JSON.stringify(outgoing)}\n`);
}

// ─── IPC ────────────────────────────────────────────────────────────────────

ipcMain.handle("start-pipeline", (_event, config) => {
  startSession(config && typeof config === "object" ? config : {});
  return { ok: true };
});

ipcMain.handle("stop-pipeline", () => {
  killSession(false);
  return { ok: true };
});

function allowedRoots() {
  const roots = [ROOT, downloadsDir()];
  return roots.map((dir) => path.resolve(dir));
}

function isInside(target, dir) {
  const a = process.platform === "win32" ? target.toLowerCase() : target;
  const b = process.platform === "win32" ? dir.toLowerCase() : dir;
  return a === b || a.startsWith(b.endsWith(path.sep) ? b : b + path.sep);
}

ipcMain.handle("open-path", async (_event, targetPath) => {
  if (!targetPath || typeof targetPath !== "string") return { ok: false };
  const resolved = path.resolve(targetPath);
  if (!allowedRoots().some((dir) => isInside(resolved, dir))) {
    return { ok: false, error: "That folder is outside the download directory." };
  }
  let stat;
  try {
    stat = fs.statSync(resolved);
  } catch {
    return { ok: false, error: "That folder no longer exists." };
  }
  // Folders open; files are only revealed, so a downloaded program is never launched.
  if (!stat.isDirectory()) {
    shell.showItemInFolder(resolved);
    return { ok: true };
  }
  const error = await shell.openPath(resolved);
  return { ok: !error, error: error || null };
});

ipcMain.handle("open-logs", async (_event, which) => {
  const dir = which === "run" ? path.join(LOG_DIR, "runs") : which === "app" ? LOG_DIR : SOURCE_LOGS;
  fs.mkdirSync(dir, { recursive: true });
  const error = await shell.openPath(dir);
  return { ok: !error, error: error || null };
});

// Opens a run log in the default text editor. Only .log files inside engine\appdata\logs.
ipcMain.handle("open-log-file", async (_event, target) => {
  const resolved = path.resolve(String(target || ""));
  if (!isInside(resolved, path.resolve(LOG_DIR)) || !/\.log$/i.test(resolved) || !existingFile(resolved)) {
    return { ok: false, error: "That log file is not available." };
  }
  const error = await shell.openPath(resolved);
  return { ok: !error, error: error || null };
});

ipcMain.handle("flare-status", () => ({ state: flare.state, message: flare.message, installed: Boolean(FLARE_BIN) }));
ipcMain.handle("flare-set", async (_event, on) => {
  if (on) await startFlare();
  else stopFlare();
  return { state: flare.state, message: flare.message };
});

ipcMain.handle("get-defaults", () => {
  const settings = readSettings();
  const cookieFiles = (Array.isArray(settings.cookie_files) ? settings.cookie_files : [settings.cookies_path])
    .map(existingFile)
    .filter(Boolean);
  return {
    downloadsDir: downloadsDir(),
    cookieFiles,
    prefs: settings.prefs && typeof settings.prefs === "object" ? settings.prefs : {},
    queue: Array.isArray(settings.queue) ? settings.queue : [],
    watches: Array.isArray(settings.watches) ? settings.watches : [],
    activeRun: settings.active_run && typeof settings.active_run === "object" ? settings.active_run : null,
    threadPosts: settings.thread_posts && typeof settings.thread_posts === "object" ? settings.thread_posts : {},
    platform: process.platform,
    version: app.getVersion(),
  };
});

ipcMain.handle("save-gui-setting", (_event, patch) => {
  if (!patch || typeof patch !== "object") return { ok: false };
  const clean = Object.fromEntries(Object.entries(patch).filter(([key]) => SETTING_KEYS.has(key)));
  if (!Object.keys(clean).length) return { ok: false };
  writeSettings(clean);
  return { ok: true };
});

ipcMain.handle("pick-cookies-file", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) return { ok: false, canceled: true, path: "" };
  try {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "Select cookies file",
      message: "Pick a Netscape cookies.txt or a storage_state.json exported from your browser.",
      defaultPath: path.join(os.homedir(), "Downloads"),
      properties: ["openFile"],
      filters: [
        { name: "Cookies", extensions: ["txt", "json"] },
        { name: "All files", extensions: ["*"] },
      ],
    });
    if (result.canceled || !result.filePaths.length) return { ok: false, canceled: true, path: "" };
    return { ok: true, canceled: false, path: result.filePaths[0] };
  } catch (error) {
    return { ok: false, canceled: true, path: "", error: String(error) };
  }
});

ipcMain.handle("pick-download-dir", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) return { ok: false, canceled: true, path: "" };
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "Choose download folder",
    defaultPath: downloadsDir(),
    properties: ["openDirectory", "createDirectory"],
  });
  if (result.canceled || !result.filePaths.length) return { ok: false, canceled: true, path: "" };
  writeSettings({ downloads_dir: result.filePaths[0] });
  return { ok: true, canceled: false, path: result.filePaths[0] };
});

function readRuns() {
  const data = parseJson((() => {
    try {
      return fs.readFileSync(RUNS_FILE, "utf8");
    } catch {
      return "{}";
    }
  })(), {});
  return data && typeof data === "object" && !Array.isArray(data) ? data : {};
}

// Reads the run history directly instead of starting Python for every Run click.
ipcMain.handle("lookup-url-state", (_event, urls) => {
  const runs = readRuns();
  return (Array.isArray(urls) ? urls : []).map((url) => {
    const entry = runs[url];
    return entry ? { url, known: true, ...entry } : { url, known: false };
  });
});

ipcMain.handle("recent-runs", (_event, limit) => {
  const runs = readRuns();
  return Object.entries(runs)
    .map(([url, entry]) => ({ url, ...entry }))
    .sort((a, b) => String(b.last_run || "").localeCompare(String(a.last_run || "")))
    .slice(0, Math.max(1, Math.min(100, Number(limit) || 20)));
});

ipcMain.handle("source-maintenance", async (_event, action) => {
  const flags = { check: "--check", backlog: "--backlog", clear: "--clear" };
  const flag = flags[String(action)] || "--status";
  const result = await runPython([path.join(ROOT, "desktop", "source_registry.py"), "--dir", SOURCE_LOGS, flag], { timeout: 120000 });
  const parsed = parseJson((result.stdout || "").trim() || "{}", null);
  return parsed || { ok: false, error: (result.stderr || result.stdout || "Compatibility check failed").slice(0, 400) };
});

// ─── accounts ───────────────────────────────────────────────────────────────

const ACCOUNT_IDS = new Set(CATALOG.map((item) => item.id));

function accountHosts(entry, host) {
  if (entry && entry.hosts && entry.hosts.length) return entry.hosts;
  const clean = String(host || "").trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0];
  return /^[a-z0-9.-]+\.[a-z]{2,}$/.test(clean) ? [clean] : [];
}

function accountCookiePath(id) {
  return path.join(ACCOUNT_COOKIES, `${id}.txt`);
}

ipcMain.handle("auth-list", () => {
  try {
    return { ok: true, accounts: accountVault().list() };
  } catch (error) {
    return { ok: false, error: error.message, accounts: [] };
  }
});

ipcMain.handle("auth-save", async (_event, body) => {
  try {
    const id = String(body?.id || "");
    if (!ACCOUNT_IDS.has(id)) return { ok: false, error: "That website has no account settings in this build." };
    const method = String(body?.method || "none");
    if (method === "telegram") return { ok: false, error: "Use Send code to sign in to Telegram." };
    const store = accountVault();
    const entry = store.list().find((item) => item.id === id);
    if (method === "cookies") {
      const hosts = accountHosts(entry, body?.host);
      if (!hosts.length) return { ok: false, error: "Type the site host, for example example.com. Other sites in the file are ignored." };
      const source = existingFile(String(body?.cookieFile || ""));
      if (!source) return { ok: false, error: "Choose a cookie file first." };
      const result = await runPython([path.join(ROOT, "desktop", "auth_cookies.py"), "--filter", source, "--hosts", hosts.join(",")]);
      if (result.code !== 0 || !result.stdout.includes("\t")) {
        return { ok: false, error: "No cookies for this site were in that file." };
      }
      const dest = accountCookiePath(id);
      fs.mkdirSync(ACCOUNT_COOKIES, { recursive: true });
      fs.writeFileSync(dest, result.stdout, { mode: 0o600 });
      store.save(id, "cookies", { cookieFile: dest, host: hosts[0] });
      return { ok: true, configured: true, message: `Saved cookies for ${hosts.join(", ")} only.` };
    }
    store.save(id, method, body || {});
    if (method === "none" && fs.existsSync(accountCookiePath(id))) fs.rmSync(accountCookiePath(id));
    return { ok: true, configured: method !== "none" };
  } catch (error) {
    return { ok: false, error: error.message };
  }
});

ipcMain.handle("auth-remove", (_event, rawId) => {
  const id = String(rawId || "");
  if (!ACCOUNT_IDS.has(id)) return { ok: false, error: "Unknown account." };
  try {
    accountVault().remove(id);
    if (fs.existsSync(accountCookiePath(id))) fs.rmSync(accountCookiePath(id));
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error.message };
  }
});

ipcMain.handle("auth-test", async (_event, rawId) => {
  const id = String(rawId || "");
  if (!ACCOUNT_IDS.has(id)) return { ok: false, error: "Unknown account." };
  const store = accountVault();
  let secret;
  try {
    secret = store.readSecret(id);
  } catch {
    return { ok: false, error: "The saved account could not be decrypted on this computer. Save it again." };
  }
  if (!secret) return { ok: true, state: "missing", message: "No account is saved for this website." };
  if (secret.method === "cookies") {
    const entry = store.list().find((item) => item.id === id);
    const hosts = accountHosts(entry, secret.host);
    const result = await runPython([path.join(ROOT, "desktop", "auth_cookies.py"), "--status", secret.cookieFile, "--hosts", hosts.join(",")]);
    const parsed = parseJson(result.stdout || "", null);
    if (!parsed) return { ok: false, error: "The cookie file could not be checked." };
    store.mark(id, new Date().toISOString(), parsed.state === "present" ? "" : parsed.message);
    return { ok: true, ...parsed };
  }
  if (id === "telegram" && secret.session) {
    const result = await runPython([path.join(ROOT, "desktop", "telegram_account.py"), "--check"], {
      input: `${JSON.stringify({ apiId: secret.apiId, apiHash: secret.apiHash, session: secret.session })}\n`,
      timeout: 45000,
    });
    const parsed = parseJson((result.stdout || "").trim().split("\n").pop() || "", null) || { state: "error", message: "Could not check the Telegram sign-in." };
    store.mark(id, new Date().toISOString(), parsed.state === "valid" ? "" : parsed.message);
    return { ok: true, state: parsed.state, message: parsed.message };
  }
  if (id === "real_debrid" && secret.apiKey) {
    const outcome = await new Promise((resolve) => {
      const request = https.get(
        "https://api.real-debrid.com/rest/1.0/user",
        { headers: { Authorization: `Bearer ${secret.apiKey}` }, timeout: 15000 },
        (response) => {
          response.resume();
          if (response.statusCode === 200) resolve({ state: "valid", message: "Real-Debrid accepted the token." });
          else if (response.statusCode === 401 || response.statusCode === 403) resolve({ state: "invalid", message: "Real-Debrid rejected the token." });
          else resolve({ state: "error", message: `Real-Debrid returned ${response.statusCode}.` });
        },
      );
      request.on("timeout", () => {
        request.destroy();
        resolve({ state: "timeout", message: "Could not reach Real-Debrid." });
      });
      request.on("error", () => resolve({ state: "timeout", message: "Could not reach Real-Debrid." }));
    });
    store.mark(id, new Date().toISOString(), outcome.state === "valid" ? "" : outcome.message);
    return { ok: true, ...outcome };
  }
  return { ok: true, state: "unchecked", message: "Saved. This site has no separate connection test, so the next download will use it." };
});

// ─── Telegram sign-in ───────────────────────────────────────────────────────
// A small Python helper holds the half-finished sign-in (Telegram ties the code
// to that connection). The finished session goes straight into the encrypted
// vault; the window only ever sees the account's display name.

const telegramLogin = { child: null, pending: null, buffer: "", apiId: "", apiHash: "", timer: null };

function stopTelegramLogin() {
  const child = telegramLogin.child;
  telegramLogin.child = null;
  clearTimeout(telegramLogin.timer);
  if (telegramLogin.pending) {
    telegramLogin.pending({ step: "error", field: "", message: "The sign-in was closed. Start again with Send code." });
    telegramLogin.pending = null;
  }
  if (child && child.exitCode === null) {
    try {
      child.stdin.end(`${JSON.stringify({ cmd: "cancel" })}\n`);
    } catch {
      /* already closed */
    }
    setTimeout(() => {
      if (child.exitCode === null) killTree(child.pid, true);
    }, 3000);
  }
}

function telegramHelper() {
  if (telegramLogin.child && telegramLogin.child.exitCode === null) return telegramLogin.child;
  const child = spawn(PYTHON, ["-u", path.join(ROOT, "desktop", "telegram_account.py"), "--login"], {
    cwd: ROOT,
    windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
  });
  telegramLogin.child = child;
  telegramLogin.buffer = "";
  const decoder = new StringDecoder("utf8");
  child.stdout.on("data", (chunk) => {
    telegramLogin.buffer += decoder.write(chunk);
    let index;
    while ((index = telegramLogin.buffer.indexOf("\n")) >= 0) {
      const line = telegramLogin.buffer.slice(0, index).trim();
      telegramLogin.buffer = telegramLogin.buffer.slice(index + 1);
      const reply = parseJson(line, null);
      if (reply && telegramLogin.pending) {
        const resolve = telegramLogin.pending;
        telegramLogin.pending = null;
        clearTimeout(telegramLogin.timer);
        resolve(reply);
      }
    }
  });
  child.stderr.on("data", (chunk) => logFile.write(`[telegram] ${String(chunk).slice(0, 1000)}`));
  child.on("error", () => {});
  child.on("close", () => {
    if (telegramLogin.child === child) telegramLogin.child = null;
    if (telegramLogin.pending) {
      telegramLogin.pending({ step: "error", field: "", message: "The Telegram helper stopped. Start again with Send code." });
      telegramLogin.pending = null;
    }
  });
  return child;
}

function askTelegram(command) {
  return new Promise((resolve) => {
    const child = telegramHelper();
    telegramLogin.pending = resolve;
    clearTimeout(telegramLogin.timer);
    telegramLogin.timer = setTimeout(() => {
      if (telegramLogin.pending === resolve) {
        telegramLogin.pending = null;
        resolve({ step: "error", field: "", message: "Telegram did not answer in time. Check the connection and try again." });
      }
    }, 90000);
    child.stdin.write(`${JSON.stringify(command)}\n`);
  });
}

ipcMain.handle("telegram-login", async (_event, body) => {
  const action = String(body?.action || "");
  if (action === "cancel") {
    stopTelegramLogin();
    return { step: "cancelled" };
  }
  let reply;
  if (action === "send_code") {
    stopTelegramLogin();
    telegramLogin.apiId = String(body?.apiId || "").trim();
    telegramLogin.apiHash = String(body?.apiHash || "").trim().toLowerCase();
    reply = await askTelegram({ cmd: "send_code", apiId: telegramLogin.apiId, apiHash: telegramLogin.apiHash, phone: String(body?.phone || "") });
  } else if (action === "code" || action === "password") {
    if (!telegramLogin.child) return { step: "error", field: "", message: "Start with Send code." };
    reply = await askTelegram(action === "code" ? { cmd: "code", code: String(body?.code || "") } : { cmd: "password", password: String(body?.password || "") });
  } else {
    return { step: "error", field: "", message: "Unknown step." };
  }
  if (reply.step !== "done") {
    if (reply.step === "error" && reply.field === "api") stopTelegramLogin();
    return { step: reply.step, field: reply.field || "", message: reply.message || "" };
  }
  const label = `${reply.name || "Telegram account"}${reply.username ? ` (@${reply.username})` : ""}`;
  try {
    accountVault().save("telegram", "telegram", { session: reply.session, apiId: telegramLogin.apiId, apiHash: telegramLogin.apiHash, label });
  } catch (error) {
    stopTelegramLogin();
    return { step: "error", field: "", message: error.message };
  }
  stopTelegramLogin();
  return { step: "done", message: `Signed in to Telegram as ${label}. Telegram links now use this account.` };
});

ipcMain.handle("auth-chrome", (_event, body) => {
  if (!body?.confirmed) return { ok: false, error: "Import was not confirmed." };
  return {
    ok: false,
    code: "encrypted",
    message: "Chrome encrypts cookies with Windows, and NEXDROP does not decrypt that database or copy other sites. Export a Netscape cookies.txt for this site and use Import cookie file.",
  };
});

// ─── app lifecycle ──────────────────────────────────────────────────────────

try {
  app.setPath("userData", path.join(APPDATA, "electron"));
  app.setPath("sessionData", path.join(APPDATA, "electron", "session"));
  fs.mkdirSync(app.getPath("userData"), { recursive: true });
} catch {
  /* fall back to the default user-data directory */
}

// Two windows would share one engine database and one download history.
if (process.platform === "win32") app.setAppUserModelId("NEXDROP"); // needed for Windows notifications

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });

  app.whenReady().then(async () => {
    session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
      callback(permission === "clipboard-sanitized-write" || permission === "clipboard-read");
    });
    createWindow();
    const prefs = readSettings().prefs || {};
    if (prefs.flareSolver !== false) mainWindow.webContents.once("did-finish-load", () => startFlare());
    // Job recovery runs after the window is up, so startup is not held back by Python.
    const script = path.join(ROOT, "desktop", "job_store.py");
    if (fs.existsSync(script)) {
      const result = await runPython([script, "recover", path.join(APPDATA, "jobs.sqlite")], { timeout: 30000 });
      const interrupted = parseJson(result.stdout || "{}", {}).interrupted || [];
      if (interrupted.length) {
        const notify = () => sendLog(`Marked ${interrupted.length} unfinished job(s) as interrupted. They were not started again.`, "warning");
        if (mainWindow?.webContents.isLoading()) mainWindow.webContents.once("did-finish-load", notify);
        else notify();
      }
    }
  });
}

app.on("before-quit", () => {
  killSession(true);
  stopFlare();
  stopTelegramLogin();
  logFile.close();
});
app.on("window-all-closed", () => app.quit());
