"use strict";

const { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, net, protocol, shell, session, safeStorage, nativeImage, Notification, Tray } = require("electron");
const { pathToFileURL } = require("url");
const { spawn, spawnSync } = require("child_process");
const { StringDecoder } = require("string_decoder");
const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");
const { createVault, CATALOG } = require("./credential-store");
const { createUpdater } = require("./updater");
const fmt = require("./renderer/format.js");

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
const SETTING_KEYS = new Set(["downloads_dir", "cookie_files", "prefs", "window", "queue", "watches", "active_run", "thread_posts", "update_skip"]);
const LOG_DIR = path.join(APPDATA, "logs");
const FLARE_BIN = ["flaresolverr.exe", "flaresolverr"].map((n) => path.join(ROOT, "runtime", "flaresolverr", n)).find((f) => fs.existsSync(f)) || "";
const FLARE_URL = "http://127.0.0.1:8191/v1";
const STOP_GRACE_MS = 6000;

let mainWindow = null;
let quitting = false; // true once the user really quits (not "close to tray")
const START_HIDDEN = process.argv.includes("--hidden");

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

// Folders chosen for single jobs (3.0 job manager). Only these and the main download folder
// can be a run's destination or be opened from the app.
const JOB_DIRS_MAX = 50;

function jobDirs() {
  const saved = readSettings().job_dirs;
  return Array.isArray(saved) ? saved.filter((dir) => typeof dir === "string" && dir).slice(0, JOB_DIRS_MAX) : [];
}

function samePath(a, b) {
  const x = path.resolve(a);
  const y = path.resolve(b);
  return process.platform === "win32" ? x.toLowerCase() === y.toLowerCase() : x === y;
}

/** Rules (3.1) whose folder, when they have one, was chosen in a folder dialog on this PC. */
function trustedRules(rules) {
  if (!Array.isArray(rules)) return [];
  const known = [downloadsDir(), ...jobDirs()];
  return rules.filter((rule) => rule && typeof rule === "object" && (rule.then !== "folder" || (typeof rule.folder === "string" && known.some((dir) => samePath(dir, rule.folder))))).slice(0, 50);
}

function runFolder(requested) {
  const main = downloadsDir();
  if (!requested || typeof requested !== "string") return main;
  if (samePath(requested, main) || jobDirs().some((dir) => samePath(dir, requested))) return requested;
  sendLog("That job's folder was not chosen on this PC, so it downloads to the main download folder.", "warning");
  return main;
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

function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (!mainWindow.isVisible()) mainWindow.show();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
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
    // Started with Windows: stay in the tray, or start minimised when the tray is off.
    if (START_HIDDEN && appOptions().closeToTray) return;
    mainWindow.show();
    if (START_HIDDEN) mainWindow.minimize();
  });
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  mainWindow.on("close", (event) => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    try {
      writeSettings({ window: { ...mainWindow.getNormalBounds(), maximized: mainWindow.isMaximized() } });
    } catch {
      /* window position is a convenience */
    }
    if (!quitting && tray && appOptions().closeToTray) {
      // Keep downloads and watched links going; Quit in the tray menu really exits.
      event.preventDefault();
      mainWindow.hide();
      if (!trayHinted) {
        trayHinted = true;
        if (Notification.isSupported()) {
          new Notification({ title: "NEXDROP is still running", body: "Downloads and watched links continue. Use the tray icon to open NEXDROP or quit.", icon: appIcon(), silent: true }).show();
        }
      }
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
    // A running download is stopped by before-quit, which lets it save its state first.
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
    setTrayTip(`${progress.label ? `${progress.label}: ` : ""}${finished} of ${event.total} files`);
  } else if (event.type === "finished") {
    mainWindow.setProgressBar(-1);
    setTrayTip("");
    notifyFinished(event);
  }
}

function notifyFinished(event, background = false, label = progress.label) {
  if (background && mainWindow.isFocused()) return;
  const parts = [`${event.saved || 0} saved`];
  if (event.skipped) parts.push(`${event.skipped} already saved`);
  if (event.failed) parts.push(`${event.failed} failed`);
  const title = event.stopped ? "Download paused" : event.failed ? "Download finished with errors" : "Download finished";
  const body = `${label ? `${label}: ` : ""}${parts.join(", ")}.`;
  if (!mainWindow.isFocused()) mainWindow.flashFrame(true);
  if (Notification.isSupported() && !mainWindow.isFocused()) {
    const note = new Notification({ title, body, icon: appIcon(), silent: false });
    note.on("click", showWindow);
    note.show();
  }
}

// ─── download session ───────────────────────────────────────────────────────

// Slot 0 is the job shown in the window; slots 1..MAX_BACKGROUND run other queued jobs beside it (3.1).
const MAX_BACKGROUND = 3;
const sessions = new Map(); // slot -> { id, slot, child, stopTimer }
let sessionCounter = 0;
let current = null; // the window's own run (slot 0)

function killSession(force, slot = 0) {
  const run = sessions.get(slot);
  if (!run) return;
  clearTimeout(run.stopTimer);
  if (force) {
    sessions.delete(slot);
    if (slot === 0) current = null;
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
    if (sessions.get(slot) === run && run.child.exitCode === null) killTree(run.child.pid, true);
  }, STOP_GRACE_MS);
}

function freeSlot() {
  for (let slot = 1; slot <= MAX_BACKGROUND; slot += 1) if (!sessions.has(slot)) return slot;
  return 0;
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

/** What a session gets: folders checked, accounts and cookies from the vault, never from the window. */
function runConfig(config) {
  const outgoing = { ...(config || {}) };
  outgoing.downloadsDir = runFolder(outgoing.downloadsDir);
  outgoing.rules = trustedRules(outgoing.rules);
  for (const key of ["auth", "apiKey", "password", "cookieFolder", "accountCookies"]) delete outgoing[key];
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
  try {
    const cookies = accountCookieTexts();
    if (Object.keys(cookies).length) outgoing.accountCookies = cookies;
  } catch (error) {
    logFile.write(`[security] saved site cookies could not be read: ${error.message}`);
  }
  return outgoing;
}

function startSession(config, slot = 0) {
  if (sessions.has(slot)) killSession(true, slot);
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
      NEXDROP_SLOT: String(slot),
    },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
    detached: process.platform !== "win32",
  });
  const run = { id, slot, child, stopTimer: null };
  sessions.set(slot, run);
  if (slot === 0) current = run;
  const live = () => sessions.get(slot) === run;
  // A job running beside the window's own sends a summary of its events, tagged with its slot.
  const deliver = (event) => {
    if (slot === 0) {
      taskbar(event);
      send(event);
      return;
    }
    if (event.type === "files") {
      send({ type: "job-event", slot, event: { type: "files", counts: event.counts, total: event.total, bytes: event.bytes, speed: event.speed } });
    } else if (["run", "phase", "finished", "error", "exit", "duplicates"].includes(event.type) || (event.type === "log" && event.level !== "info")) {
      send({ type: "job-event", slot, event });
      if (event.type === "finished") notifyFinished(event, true, String((config && config.label) || "").slice(0, 80));
    }
  };

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
        if (event.type === "log" && event.level !== "info") logFile.write(`${slot ? `[job ${slot}] ` : ""}[${event.level}] ${event.message}`);
        deliver(event);
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
    deliver({ type: "log", level: "error", message: text.slice(0, 500) });
  });
  let reported = false;
  const reportExit = (code) => {
    if (reported) return;
    reported = true;
    clearTimeout(run.stopTimer);
    if (!live()) return;
    sessions.delete(slot);
    if (slot === 0) current = null;
    deliver({ type: "exit", code });
  };
  child.on("error", (error) => {
    if (live()) deliver({ type: "log", level: "error", message: `Could not start the bundled Python (${error.message}).` });
    reportExit(1);
  });
  child.on("close", (code) => reportExit(code));

  const outgoing = runConfig(config);
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

// Jobs at once (3.1): a queued job started beside the window's own run.
ipcMain.handle("start-job", (_event, config) => {
  const slot = freeSlot();
  if (!slot) return { ok: false, error: "No free job slot." };
  startSession(config && typeof config === "object" ? config : {}, slot);
  return { ok: true, slot };
});

ipcMain.handle("stop-job", (_event, slot) => {
  const n = Number(slot);
  if (!Number.isInteger(n) || n < 1 || n > MAX_BACKGROUND) return { ok: false };
  killSession(false, n);
  return { ok: true };
});

function allowedRoots() {
  const roots = [ROOT, downloadsDir(), ...jobDirs()];
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

ipcMain.handle("pick-job-dir", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) return { ok: false, canceled: true, path: "" };
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "Choose a folder for this job",
    defaultPath: downloadsDir(),
    properties: ["openDirectory", "createDirectory"],
  });
  if (result.canceled || !result.filePaths.length) return { ok: false, canceled: true, path: "" };
  const chosen = result.filePaths[0];
  const dirs = [chosen, ...jobDirs().filter((dir) => !samePath(dir, chosen))].slice(0, JOB_DIRS_MAX);
  writeSettings({ job_dirs: dirs });
  return { ok: true, canceled: false, path: chosen };
});

// Jobs to and from a file. Parsing and checking happen in the window (format.js parseJobFile).
ipcMain.handle("jobs-export", async (_event, jobs) => {
  if (!mainWindow || mainWindow.isDestroyed() || !Array.isArray(jobs)) return { ok: false };
  const result = await dialog.showSaveDialog(mainWindow, {
    title: "Export jobs",
    defaultPath: path.join(app.getPath("documents"), "nexdrop-jobs.json"),
    filters: [{ name: "NexDrop jobs", extensions: ["json"] }],
  });
  if (result.canceled || !result.filePath) return { ok: false, canceled: true };
  const body = { format: "nexdrop-jobs", version: 1, exported: new Date().toISOString(), jobs: jobs.slice(0, 500) };
  fs.writeFileSync(result.filePath, JSON.stringify(body, null, 2));
  return { ok: true, path: result.filePath, count: body.jobs.length };
});

ipcMain.handle("jobs-import", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) return { ok: false };
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "Import jobs or links",
    properties: ["openFile"],
    filters: [{ name: "Jobs or link lists", extensions: ["json", "txt", "csv", "m3u", "m3u8"] }],
  });
  if (result.canceled || !result.filePaths.length) return { ok: false, canceled: true };
  const file = result.filePaths[0];
  const size = fs.statSync(file).size;
  if (size > 20 * 1024 * 1024) return { ok: false, error: "That file is larger than 20 MB." };
  return { ok: true, name: path.basename(file), text: fs.readFileSync(file, "utf8"), knownDirs: [downloadsDir(), ...jobDirs()] };
});

// ─── diagnostics, backup and restore (3.0) ──────────────────────────────────

function savedSignIns() {
  try {
    return accountVault().list().filter((item) => item.configured).length;
  } catch {
    return "?";
  }
}

function tailOf(file, lines) {
  try {
    const text = fs.readFileSync(file, "utf8");
    return text.split(/\r?\n/).slice(-lines).join("\n");
  } catch {
    return "(not found)";
  }
}

function newestFile(dir, pattern) {
  try {
    return fs.readdirSync(dir).filter((name) => pattern.test(name))
      .map((name) => ({ file: path.join(dir, name), at: fs.statSync(path.join(dir, name)).mtimeMs }))
      .sort((a, b) => b.at - a.at)[0]?.file || "";
  } catch {
    return "";
  }
}

// A plain-text report to attach to a bug report. It never includes links you queued, sign-ins or
// cookies, and everything goes through fmt.redact. The user picks where it is saved and reads it first.
ipcMain.handle("diagnostics-export", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) return { ok: false };
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
  const picked = await dialog.showSaveDialog(mainWindow, {
    title: "Save diagnostics report",
    defaultPath: path.join(app.getPath("documents"), `nexdrop-diagnostics-${stamp}.txt`),
    filters: [{ name: "Text", extensions: ["txt"] }],
  });
  if (picked.canceled || !picked.filePath) return { ok: false, canceled: true };
  const settings = readSettings();
  const prefs = { ...(settings.prefs || {}) };
  if (prefs.proxy) prefs.proxy = "(set)";
  const health = fs.existsSync(JOBS_DB)
    ? parseJson((await runPython([path.join(ROOT, "desktop", "job_store.py"), "health", JOBS_DB, "7"], { timeout: 30000 })).stdout.trim(), {})
    : {};
  const runLog = newestFile(path.join(LOG_DIR, "runs"), /\.log$/i);
  const sections = [
    ["NexDrop diagnostics", [
      `version ${app.getVersion()}, Electron ${process.versions.electron}, ${os.type()} ${os.release()} ${process.arch}`,
      `memory ${Math.round(os.totalmem() / 1073741824)} GB, ${os.cpus().length} CPU threads`,
      `python ${fs.existsSync(PYTHON) ? "bundled" : PYTHON}, FlareSolverr ${FLARE_BIN ? "bundled" : "missing"}`,
      `queue ${Array.isArray(settings.queue) ? settings.queue.length : 0} job(s), watched ${Array.isArray(settings.watches) ? settings.watches.length : 0}, saved sign-ins ${savedSignIns()}`,
      "Links in the queue, sign-ins and cookies are not included. Tokens, passwords, e-mail addresses and your user name are removed.",
    ].join("\n")],
    ["Settings", JSON.stringify(prefs, null, 2)],
    ["Sites, last 7 days", (health.sites || []).map((s) => `${s.host}: ${s.saved} saved, ${s.failed} failed${s.reasons?.length ? ` (${s.reasons.map((r) => `${r.label} ${r.count}`).join(", ")})` : ""}`).join("\n") || "(no history)"],
    [`Latest run log (${runLog ? path.basename(runLog) : "none"})`, runLog ? tailOf(runLog, 400) : "(none)"],
    ["App log", tailOf(LOG_FILE, 200)],
    ["FlareSolverr log", tailOf(path.join(LOG_DIR, "flaresolverr.log"), 60)],
  ];
  const body = sections.map(([title, text]) => `===== ${title} =====\n${text}\n`).join("\n");
  fs.writeFileSync(picked.filePath, fmt.redact(body, os.homedir()));
  shell.showItemInFolder(picked.filePath);
  return { ok: true, path: picked.filePath };
});

ipcMain.handle("backup-create", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) return { ok: false };
  const stamp = new Date().toISOString().slice(0, 10);
  const picked = await dialog.showSaveDialog(mainWindow, {
    title: "Back up NexDrop",
    defaultPath: path.join(app.getPath("documents"), `nexdrop-backup-${stamp}.zip`),
    filters: [{ name: "NexDrop backup", extensions: ["zip"] }],
  });
  if (picked.canceled || !picked.filePath) return { ok: false, canceled: true };
  const result = await runPython([path.join(ROOT, "desktop", "backup.py"), "create", APPDATA, picked.filePath], { timeout: 600000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "Backup failed.").slice(0, 300) };
});

ipcMain.handle("backup-restore", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) return { ok: false };
  if (sessions.size) return { ok: false, error: "Pause or finish the running downloads before restoring." };
  const picked = await dialog.showOpenDialog(mainWindow, {
    title: "Restore a NexDrop backup",
    properties: ["openFile"],
    filters: [{ name: "NexDrop backup", extensions: ["zip"] }],
  });
  if (picked.canceled || !picked.filePaths.length) return { ok: false, canceled: true };
  const source = picked.filePaths[0];
  const info = parseJson((await runPython([path.join(ROOT, "desktop", "backup.py"), "inspect", source], { timeout: 60000 })).stdout.trim(), null);
  if (!info || !info.ok) return { ok: false, error: info?.error || "That file could not be read." };
  const answer = await dialog.showMessageBox(mainWindow, {
    type: "question",
    buttons: ["Restore and restart", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    title: "Restore backup",
    message: `Restore the backup from ${info.created || "an unknown date"}?`,
    detail: `It replaces: ${info.files.map((f) => f.name).join(", ")}.\nYour current copies are kept beside them as ".before-restore" files, so nothing is lost. Sign-ins are not part of a backup and stay as they are. NexDrop restarts afterwards.`,
  });
  if (answer.response !== 0) return { ok: false, canceled: true };
  const result = parseJson((await runPython([path.join(ROOT, "desktop", "backup.py"), "restore", APPDATA, source], { timeout: 600000 })).stdout.trim(), null);
  if (!result || !result.ok) return { ok: false, error: result?.error || "Restore failed." };
  logFile.write(`[info] Restored backup ${path.basename(source)}: ${result.restored.join(", ")}`);
  setTimeout(() => {
    quitting = true;
    app.relaunch();
    app.exit(0);
  }, 800);
  return result;
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

// Site cookies are sign-ins, so they are stored encrypted with Windows credential protection
// (<id>.txt.enc). 2.x kept them as plain <id>.txt; those are encrypted and removed on first use.
function accountCookiePath(id) {
  return path.join(ACCOUNT_COOKIES, `${id}.txt.enc`);
}

function legacyCookiePath(id) {
  return path.join(ACCOUNT_COOKIES, `${id}.txt`);
}

function writeAccountCookies(id, text) {
  if (!safeStorage.isEncryptionAvailable()) throw new Error("Secure storage is not available on this computer. Nothing was saved.");
  fs.mkdirSync(ACCOUNT_COOKIES, { recursive: true });
  const dest = accountCookiePath(id);
  const temp = `${dest}.tmp`;
  fs.writeFileSync(temp, safeStorage.encryptString(text).toString("base64"), { mode: 0o600 });
  fs.renameSync(temp, dest);
  return dest;
}

function readAccountCookies(id) {
  const legacy = legacyCookiePath(id);
  if (fs.existsSync(legacy)) {
    try {
      const text = fs.readFileSync(legacy, "utf8");
      writeAccountCookies(id, text);
      fs.rmSync(legacy, { force: true });
      logFile.write(`[security] encrypted the saved cookies for ${id}`);
      return text;
    } catch (error) {
      logFile.write(`[security] could not encrypt the saved cookies for ${id}: ${error.message}`);
      return fs.readFileSync(legacy, "utf8");
    }
  }
  try {
    return safeStorage.decryptString(Buffer.from(fs.readFileSync(accountCookiePath(id), "utf8"), "base64"));
  } catch {
    return "";
  }
}

function removeAccountCookies(id) {
  for (const file of [accountCookiePath(id), legacyCookiePath(id)]) fs.rmSync(file, { force: true });
}

/** Every saved site's cookies, decrypted for one run. They reach the session through its stdin only. */
function accountCookieTexts() {
  const out = {};
  if (!fs.existsSync(ACCOUNT_COOKIES)) return out;
  const ids = new Set(fs.readdirSync(ACCOUNT_COOKIES)
    .map((name) => (name.match(/^([a-z0-9_-]+)\.txt(?:\.enc)?$/i) || [])[1])
    .filter((id) => id && ACCOUNT_IDS.has(id)));
  for (const id of ids) {
    const text = readAccountCookies(id);
    if (text && text.includes("\t")) out[id] = text;
  }
  return out;
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
      const dest = writeAccountCookies(id, result.stdout);
      fs.rmSync(legacyCookiePath(id), { force: true });
      store.save(id, "cookies", { cookieFile: dest, host: hosts[0] });
      return { ok: true, configured: true, message: `Saved cookies for ${hosts.join(", ")} only.` };
    }
    store.save(id, method, body || {});
    if (method === "none") removeAccountCookies(id);
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
    removeAccountCookies(id);
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
    const text = readAccountCookies(id);
    if (!text) return { ok: true, state: "missing", message: "The saved cookies are missing or could not be decrypted on this computer. Import them again." };
    const result = await runPython([path.join(ROOT, "desktop", "auth_cookies.py"), "--status-stdin", "--hosts", hosts.join(",")], { input: text });
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

// ─── app options: tray, start with Windows, clipboard, browser links ────────
// These live in prefs (saved by the window) and are applied here, in the main process.

let tray = null;
let trayHinted = false;

function appOptions() {
  const prefs = readSettings().prefs || {};
  return {
    closeToTray: prefs.closeToTray === true,
    startWithWindows: prefs.startWithWindows === true,
    clipboardWatch: prefs.clipboardWatch === true,
    linkHandler: prefs.linkHandler === true,
    updateCheck: prefs.updateCheck !== false,
  };
}

function setTrayTip(text) {
  if (tray && !tray.isDestroyed()) tray.setToolTip(text ? `NEXDROP – ${text}` : "NEXDROP");
}

function command(name) {
  send({ type: "app-command", command: name });
}

function ensureTray(on) {
  if (!on) {
    if (tray && !tray.isDestroyed()) tray.destroy();
    tray = null;
    return;
  }
  if (tray && !tray.isDestroyed()) return;
  const icon = appIcon();
  tray = new Tray(icon ? icon.resize({ width: 16, height: 16 }) : nativeImage.createEmpty());
  tray.setToolTip("NEXDROP");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open NEXDROP", click: showWindow },
    { type: "separator" },
    { label: "Pause downloads", click: () => command("pause") },
    { label: "Check watched links now", click: () => command("check-watches") },
    { label: "Open download folder", click: () => command("open-folder") },
    { type: "separator" },
    { label: "Quit NEXDROP", click: () => { quitting = true; app.quit(); } },
  ]));
  tray.on("click", showWindow);
}

function applyStartWithWindows(on) {
  if (process.platform !== "win32" && process.platform !== "darwin") return;
  try {
    const args = process.defaultApp ? [path.resolve(process.argv[1] || "."), "--hidden"] : ["--hidden"];
    app.setLoginItemSettings({ openAtLogin: Boolean(on), path: process.execPath, args });
  } catch (error) {
    logFile.write(`[startup] could not change start with Windows: ${error.message}`);
  }
}

// Clipboard: links copied in other programs go into the links box. Text NEXDROP copied
// itself (Copy link, Copy log) is ignored by the window.
const clip = { timer: null, last: "" };

function watchClipboard(on) {
  clearInterval(clip.timer);
  clip.timer = null;
  if (!on) return;
  clip.last = clipboard.readText();
  clip.timer = setInterval(() => {
    let text;
    try {
      text = clipboard.readText();
    } catch {
      return;
    }
    if (!text || text === clip.last) return;
    clip.last = text;
    if (text.length > 100000 || !/https?:\/\/|t\.me\//i.test(text)) return;
    if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused()) return; // copied inside NEXDROP
    send({ type: "clipboard-links", text });
  }, 1500);
}

// nexdrop:// links from the browser extension. The links only land in the links box;
// nothing starts without pressing Start.
const pendingLinks = [];

function protocolArgs() {
  return process.defaultApp ? [path.resolve(process.argv[1] || ".")] : [];
}

function applyLinkHandler(on) {
  try {
    if (on) app.setAsDefaultProtocolClient("nexdrop", process.execPath, protocolArgs());
    else if (app.isDefaultProtocolClient("nexdrop", process.execPath, protocolArgs())) app.removeAsDefaultProtocolClient("nexdrop", process.execPath, protocolArgs());
  } catch (error) {
    logFile.write(`[links] could not change the nexdrop:// handler: ${error.message}`);
  }
}

function linksFromArgv(argv) {
  const urls = [];
  for (const arg of argv || []) {
    if (/^nexdrop:/i.test(String(arg))) urls.push(...fmt.appLinkUrls(arg));
  }
  return [...new Set(urls)];
}

function deliverLinks(urls) {
  if (!urls.length) return;
  if (!mainWindow || mainWindow.isDestroyed() || mainWindow.webContents.isLoading()) {
    pendingLinks.push(...urls);
    return;
  }
  send({ type: "external-links", urls });
  showWindow();
}

function applyAppOptions() {
  const options = appOptions();
  ensureTray(options.closeToTray);
  applyStartWithWindows(options.startWithWindows);
  watchClipboard(options.clipboardWatch);
  applyLinkHandler(options.linkHandler);
  return options;
}

ipcMain.handle("app-options", () => applyAppOptions());
ipcMain.handle("open-release-page", (_event, url) => {
  const target = String(url || "");
  shell.openExternal(/^https:\/\/github\.com\/Saiki1997\/NexDrop\//i.test(target) ? target : updater.releasesPage);
  return { ok: true };
});

// ─── updates ────────────────────────────────────────────────────────────────

const updater = createUpdater({
  root: ROOT,
  appdata: APPDATA,
  version: app.getVersion(),
  send,
  log: (message) => sendLog(message, "info"),
  isBusy: () => sessions.size > 0,
  quit: () => { quitting = true; app.quit(); },
});
const UPDATE_EVERY_MS = 12 * 3600 * 1000;
let updateTimer = null;

async function checkForUpdate(manual) {
  const status = await updater.check();
  const skipped = String(readSettings().update_skip || "");
  if (status.state === "available" && (manual || status.version !== skipped)) send({ type: "update", ...status });
  if (status.state === "error") logFile.write(`[update] ${status.message}`);
  return status;
}

function scheduleUpdateChecks(on) {
  clearInterval(updateTimer);
  updateTimer = null;
  if (!on) return;
  setTimeout(() => checkForUpdate(false), 8000);
  updateTimer = setInterval(() => checkForUpdate(false), UPDATE_EVERY_MS);
}

ipcMain.handle("update-check", () => checkForUpdate(true));
ipcMain.handle("update-install", () => updater.install());
ipcMain.handle("update-rollback-info", () => {
  const info = updater.rollbackAvailable();
  return info ? { ok: true, from: info.from, files: (info.files || []).length, created: info.created } : { ok: false };
});
ipcMain.handle("update-rollback", async () => {
  const info = updater.rollbackAvailable();
  if (!info) return { ok: false, error: "There is no earlier version saved to go back to." };
  const answer = await dialog.showMessageBox(mainWindow, {
    type: "question",
    buttons: ["Go back and restart", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    title: "Go back to the previous version",
    message: `Go back to NEXDROP ${info.from}?`,
    detail: `The ${(info.files || []).length} app files the update changed are put back and NEXDROP restarts. Settings, sign-ins, history and downloads are not touched.`,
  });
  if (answer.response !== 0) return { ok: false, cancelled: true };
  return updater.rollback();
});
ipcMain.handle("update-skip", (_event, version) => {
  writeSettings({ update_skip: String(version || "") });
  return { ok: true };
});
ipcMain.handle("update-auto", (_event, on) => {
  scheduleUpdateChecks(Boolean(on));
  return { ok: true };
});

// ─── download history ───────────────────────────────────────────────────────

const JOBS_DB = path.join(APPDATA, "jobs.sqlite");

ipcMain.handle("history-search", async (_event, body) => {
  if (!fs.existsSync(JOBS_DB)) return { ok: true, items: [], total: 0 };
  const query = { ...historyQuery(body), limit: Math.max(1, Math.min(500, Number(body?.limit) || 200)) };
  const result = await runPython([path.join(ROOT, "desktop", "job_store.py"), "search", JOBS_DB], { input: `${JSON.stringify(query)}\n`, timeout: 30000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "History search failed.").slice(0, 300), items: [] };
});

function historyQuery(body) {
  return {
    text: String(body?.text || "").slice(0, 200),
    host: String(body?.host || "").slice(0, 200),
    status: String(body?.status || "completed"),
    days: Math.max(0, Math.min(3650, Number(body?.days) || 0)),
    type: String(body?.type || "").slice(0, 20),
  };
}

// 3.0: the History list as CSV, with the same filters.
ipcMain.handle("history-export", async (_event, body) => {
  if (!fs.existsSync(JOBS_DB)) return { ok: false, error: "There is no history yet." };
  if (!mainWindow || mainWindow.isDestroyed()) return { ok: false };
  const stamp = new Date().toISOString().slice(0, 10);
  const picked = await dialog.showSaveDialog(mainWindow, {
    title: "Export history",
    defaultPath: path.join(app.getPath("documents"), `nexdrop-history-${stamp}.csv`),
    filters: [{ name: "CSV", extensions: ["csv"] }],
  });
  if (picked.canceled || !picked.filePath) return { ok: false, canceled: true };
  const dest = picked.filePath.toLowerCase().endsWith(".csv") ? picked.filePath : `${picked.filePath}.csv`;
  const input = `${JSON.stringify({ query: historyQuery(body), dest })}\n`;
  const result = await runPython([path.join(ROOT, "desktop", "job_store.py"), "export", JOBS_DB], { input, timeout: 120000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "Export failed.").slice(0, 300) };
});

// 3.0: per-site results from the history database (nothing is sent anywhere).
// 3.1 network diagnostics: DNS, connect and one HEAD request to a site.
ipcMain.handle("net-check", async (_event, url) => {
  const input = `${JSON.stringify({ url: String(url || "").slice(0, 500), proxy: String(readSettings().prefs?.proxy || "") })}\n`;
  const result = await runPython([path.join(ROOT, "desktop", "net_check.py")], { input, timeout: 45000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "The test could not run.").slice(-300), steps: [] };
});

// 3.1 URL analyzer: what each link is (offline, from the engine's handlers), and with `preview`
// one request per link for its title, size or the reason it can't be read.
ipcMain.handle("analyze-links", async (_event, body) => {
  const urls = (Array.isArray(body?.urls) ? body.urls : []).map(String).filter((u) => u.length < 4000).slice(0, 500);
  const preview = Boolean(body?.preview);
  const input = `${JSON.stringify({ urls, proxy: preview ? String(readSettings().prefs?.proxy || "") : "" })}\n`;
  const result = await runPython([path.join(ROOT, "desktop", "url_analyzer.py"), preview ? "preview" : "analyze"], { input, timeout: preview ? 120000 : 30000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "The links could not be checked.").slice(-300), items: [] };
});

ipcMain.handle("site-health", async (_event, days) => {
  if (!fs.existsSync(JOBS_DB)) return { ok: true, sites: [] };
  const span = String(Math.max(1, Math.min(365, Number(days) || 30)));
  const result = await runPython([path.join(ROOT, "desktop", "job_store.py"), "health", JOBS_DB, span], { timeout: 30000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "Site health could not be read.").slice(0, 300), sites: [] };
});

// Smart naming V2: renaming a file from History. The window passes the item id and the new
// name; the path comes from the history database. With "Learn from my renames" on, the name is
// remembered for files from the same post, album or thread (engine\appdata\naming-corrections.jsonl).
const NAMING_CORRECTIONS = path.join(APPDATA, "naming-corrections.jsonl");

ipcMain.handle("history-rename", async (_event, body) => {
  const query = { id: Number(body?.id) || 0, name: String(body?.name || "").slice(0, 240), corrections: body?.learn ? NAMING_CORRECTIONS : "" };
  const result = await runPython([path.join(ROOT, "desktop", "job_store.py"), "rename", JOBS_DB], { input: `${JSON.stringify(query)}\n`, timeout: 20000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "Rename failed.").slice(0, 300) };
});

ipcMain.handle("naming-corrections", async (_event, body) => {
  const action = { remove: "corrections-remove", clear: "corrections-clear" }[String(body?.action)] || "corrections-list";
  const input = action === "corrections-remove" ? `${JSON.stringify({ context: String(body?.context || ""), title: String(body?.title || "") })}\n` : "\n";
  const result = await runPython([path.join(ROOT, "desktop", "job_store.py"), action, JOBS_DB, NAMING_CORRECTIONS], { input, timeout: 20000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "Learned names could not be read.").slice(0, 300), rules: [] };
});

// The window passes only an item id; the path comes from the history database.
ipcMain.handle("history-reveal", async (_event, id) => {
  const result = await runPython([path.join(ROOT, "desktop", "job_store.py"), "locate", JOBS_DB, String(Number(id) || 0)], { timeout: 20000 });
  const found = parseJson((result.stdout || "").trim(), null);
  if (!found) return { ok: false, error: "That file is not in the history." };
  if (existingFile(found.destination)) {
    shell.showItemInFolder(path.resolve(found.destination));
    return { ok: true };
  }
  try {
    if (found.folder && fs.statSync(found.folder).isDirectory()) {
      const error = await shell.openPath(path.resolve(found.folder));
      return { ok: !error, error: error || null, message: "The file was moved or deleted, so its run's folder was opened." };
    }
  } catch {
    /* folder gone too */
  }
  return { ok: false, error: "The file and its folder are no longer on this computer." };
});

// ─── naming preview (3.1) ───────────────────────────────────────────────────
// The engine crawls the links and names every file without downloading anything. It runs in its
// own slot, so it can run beside a download.
const PREVIEW_SLOT = 8;
let previewChild = null;

ipcMain.handle("naming-preview", (_event, config) => new Promise((resolve) => {
  if (previewChild) killTree(previewChild.pid, true);
  const script = path.join(ROOT, "desktop", "cairn_session.py");
  const child = spawn(PYTHON, ["-u", script], {
    cwd: ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8", NEXDROP_PYTHON: PYTHON,
      NEXDROP_FLARE_MANAGED: flare.child || flare.state === "ready" ? "1" : "0", NEXDROP_SLOT: String(PREVIEW_SLOT) },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
    detached: process.platform !== "win32",
  });
  previewChild = child;
  const body = { ...runConfig(config && typeof config === "object" ? config : {}), previewNames: true, redownload: true };
  let result = null;
  const notes = [];
  const decoder = new StringDecoder("utf8");
  let buffer = "";
  child.stdout.on("data", (chunk) => {
    buffer += decoder.write(chunk);
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const event = parseJson(line.trim(), null);
      if (!event) continue;
      if (event.type === "naming-preview") result = event;
      else if ((event.type === "log" && event.level !== "info") || event.type === "error") notes.push(String(event.message || "").slice(0, 300));
      else if (event.type === "files" || event.type === "phase") send({ type: "preview-progress", total: event.total, message: event.message });
    }
  });
  child.stderr.on("data", () => {});
  child.on("error", (error) => resolve({ ok: false, error: String(error) }));
  child.on("close", () => {
    if (previewChild === child) previewChild = null;
    if (result) resolve({ ok: true, ...result, warnings: notes.slice(0, 20) });
    else resolve({ ok: false, stopped: true, error: notes[0] || "The preview stopped before it finished." });
  });
  child.stdin.write(`${JSON.stringify(body)}\n`);
}));

ipcMain.handle("naming-preview-stop", () => {
  if (previewChild) {
    try {
      previewChild.stdin.end(); // the session takes a closed stdin as "stop"
    } catch {
      killTree(previewChild.pid, true);
    }
  }
  return { ok: true };
});

// ─── media library (3.1) ────────────────────────────────────────────────────
// One catalog of downloaded files (desktop/library.py). Thumbnails come from a cache folder and
// previews only from files in the catalog, both through the nxlib: scheme, so the window never
// gets general file access.

const LIBRARY_DB = path.join(APPDATA, "library.sqlite");
const THUMBS = path.join(APPDATA, "thumbs");
const LIBRARY_SCRIPT = path.join(ROOT, "desktop", "library.py");
const MEDIA_OPEN = new Set(["jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "heic", "heif", "tif", "tiff", "jxl", "mp4", "m4v",
  "mov", "mkv", "webm", "avi", "wmv", "flv", "mpg", "mpeg", "3gp", "ts", "mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma", "txt", "pdf"]);
const libraryPaths = new Map(); // id -> path, for files the window has been shown
let libraryBusy = false;

protocol.registerSchemesAsPrivileged([{ scheme: "nxlib", privileges: { standard: true, secure: true, stream: true, supportFetchAPI: true } }]);

function libraryDirs() {
  const saved = readSettings().library_dirs;
  return Array.isArray(saved) ? saved.filter((dir) => typeof dir === "string" && dir).slice(0, 50) : [];
}

function libraryRoots() {
  const roots = [downloadsDir(), ...jobDirs(), ...libraryDirs()].map((dir) => path.resolve(dir));
  return roots.filter((dir, index) => roots.findIndex((other) => samePath(other, dir)) === index);
}

async function library(command, body = {}, timeout = 120000) {
  if (!fs.existsSync(LIBRARY_SCRIPT)) return { ok: false, error: "The library is not part of this build." };
  const result = await runPython([LIBRARY_SCRIPT, command, LIBRARY_DB], { input: `${JSON.stringify(body)}\n`, timeout });
  const parsed = parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "The library could not be read.").slice(-300) };
  for (const item of parsed.items || []) if (item && item.id && item.path) libraryPaths.set(Number(item.id), item.path);
  for (const group of parsed.groups || []) for (const item of group.files || []) if (item.id && item.path) libraryPaths.set(Number(item.id), item.path);
  if (parsed.file?.id && parsed.file.path) libraryPaths.set(Number(parsed.file.id), parsed.file.path);
  return parsed;
}

function registerLibraryScheme() {
  protocol.handle("nxlib", async (request) => {
    const url = new URL(request.url);
    if (url.hostname === "thumb") {
      const name = path.basename(decodeURIComponent(url.pathname));
      if (!/^\d+\.jpg$/.test(name)) return new Response("", { status: 404 });
      const file = path.join(THUMBS, name);
      if (!existingFile(file)) return new Response("", { status: 404 });
      return net.fetch(pathToFileURL(file).toString());
    }
    if (url.hostname === "file") {
      const id = Number(path.basename(url.pathname));
      let target = libraryPaths.get(id);
      if (!target) target = (await library("file", { id }))?.file?.path;
      if (!target) return new Response("", { status: 404 });
      const resolved = path.resolve(target);
      const ext = path.extname(resolved).slice(1).toLowerCase();
      if (!MEDIA_OPEN.has(ext) || !libraryRoots().some((dir) => isInside(resolved, dir)) || !existingFile(resolved)) {
        return new Response("", { status: 404 });
      }
      return net.fetch(pathToFileURL(resolved).toString(), { headers: request.headers });
    }
    return new Response("", { status: 404 });
  });
}

ipcMain.handle("library-sync", async () => {
  if (libraryBusy) return { ok: true, busy: true };
  libraryBusy = true;
  try {
    return await library("sync", { jobs: JOBS_DB, roots: libraryRoots() }, 600000);
  } finally {
    libraryBusy = false;
  }
});

// Metadata, fingerprints and thumbnails for a few seconds at a time; the window calls it again
// while the Library is open and files are left.
ipcMain.handle("library-work", async (_event, body) => {
  if (libraryBusy) return { ok: true, busy: true, left: -1 };
  libraryBusy = true;
  try {
    const seconds = Math.max(2, Math.min(60, Number(body?.seconds) || 15));
    return await library("work", { seconds, thumbs: THUMBS, fingerprints: body?.fingerprints !== false }, (seconds + 90) * 1000);
  } finally {
    libraryBusy = false;
  }
});

ipcMain.handle("library-query", (_event, body) => library("query", body && typeof body === "object" ? body : {}));
ipcMain.handle("library-facets", () => library("facets"));
ipcMain.handle("library-groups", (_event, body) => library("groups", {
  kind: body?.kind === "similar" ? "similar" : "exact",
  text: String(body?.text || "").slice(0, 200),
  threshold: Math.max(0, Math.min(12, Number(body?.threshold) || 6)),
  seconds: 90,
}, 300000));
ipcMain.handle("library-storage", () => library("storage", { roots: libraryRoots() }, 300000));
ipcMain.handle("library-folders", () => ({ ok: true, roots: libraryRoots(), extra: libraryDirs() }));
ipcMain.handle("library-add-folder", async () => {
  if (!mainWindow) return { ok: false };
  const picked = await dialog.showOpenDialog(mainWindow, { title: "Add a folder to the library", properties: ["openDirectory"] });
  if (picked.canceled || !picked.filePaths[0]) return { ok: false, canceled: true };
  const dirs = [...libraryDirs().filter((dir) => !samePath(dir, picked.filePaths[0])), picked.filePaths[0]];
  writeSettings({ library_dirs: dirs });
  return { ok: true, extra: dirs };
});
ipcMain.handle("library-remove-folder", (_event, dir) => {
  const dirs = libraryDirs().filter((item) => !samePath(item, String(dir || "")));
  writeSettings({ library_dirs: dirs });
  return { ok: true, extra: dirs };
});

async function libraryFile(id) {
  const n = Number(id);
  if (!Number.isInteger(n) || n < 1) return null;
  const found = (await library("file", { id: n }))?.file;
  if (!found?.path) return null;
  const resolved = path.resolve(found.path);
  if (!libraryRoots().some((dir) => isInside(resolved, dir))) return null;
  return { ...found, path: resolved, root: libraryRoots().find((dir) => isInside(resolved, dir)) };
}

/** Move a file to the Recycle Bin, only after the person confirms it. */
async function confirmTrash(target, reason) {
  if (!mainWindow || !existingFile(target)) return { ok: false, error: "The file is not there any more." };
  const answer = await dialog.showMessageBox(mainWindow, {
    type: "warning",
    buttons: ["Move to Recycle Bin", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    title: "Move to Recycle Bin",
    message: `Move ${path.basename(target)} to the Recycle Bin?`,
    detail: `${reason}\n\n${target}\n\nYou can restore it from the Recycle Bin.`,
  });
  if (answer.response !== 0) return { ok: false, canceled: true };
  try {
    await shell.trashItem(target);
    return { ok: true, trashed: true };
  } catch (error) {
    return { ok: false, error: `Could not move it to the Recycle Bin: ${error.message || error}` };
  }
}

async function dupMove(command, target, root) {
  const result = await runPython([path.join(ROOT, "desktop", "dup_actions.py"), command, JOBS_DB],
    { input: `${JSON.stringify({ path: target, root })}\n`, timeout: 60000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "The file could not be moved.").slice(-300) };
}

// Settings > Rules: what the rules would do to the files already in the Library (read only).
ipcMain.handle("rules-preview", async (_event, body) => {
  if (!fs.existsSync(LIBRARY_DB)) return { ok: true, total: 0, skipped: 0, folder: 0, single: 0, byRule: {}, examples: {}, empty: true };
  const rules = Array.isArray(body?.rules) ? body.rules.slice(0, 50) : [];
  const result = await runPython([path.join(ROOT, "desktop", "rules.py"), "preview", LIBRARY_DB], { input: `${JSON.stringify({ rules })}\n`, timeout: 120000 });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "The rules could not be checked.").slice(-300) };
});

async function archiveCall(command, body, timeout = 600000) {
  const result = await runPython([path.join(ROOT, "desktop", "archives.py"), command], { input: `${JSON.stringify(body)}\n`, timeout });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "The archive could not be read.").slice(-300) };
}

ipcMain.handle("library-action", async (_event, body) => {
  const file = await libraryFile(body?.id);
  if (!file) return { ok: false, error: "That file is not in the library." };
  const action = String(body?.action || "");
  if (action === "reveal") {
    shell.showItemInFolder(file.path);
    return { ok: true };
  }
  if (action === "open") {
    // Media and documents open in their usual app; anything else is only shown in its folder,
    // so a downloaded program is never started from here.
    const ext = path.extname(file.path).slice(1).toLowerCase();
    if (!MEDIA_OPEN.has(ext)) {
      shell.showItemInFolder(file.path);
      return { ok: true, revealed: true };
    }
    const error = await shell.openPath(file.path);
    return { ok: !error, error: error || null };
  }
  if (action === "aside" || action === "rename") {
    const result = await dupMove(action, file.path, file.root);
    if (result.ok) await library("moved", { id: file.id, path: result.path });
    return result;
  }
  if (action === "trash") {
    const result = await confirmTrash(file.path, String(body?.reason || "It has the same content as another file in the library."));
    if (result.ok) await library("moved", { id: file.id, path: file.path });
    return result;
  }
  if (action === "inspect" || action === "test") return archiveCall(action, { path: file.path });
  if (action === "extract") {
    if (!mainWindow) return { ok: false };
    const stem = path.basename(file.path).replace(/(\.tar)?\.[^.]+$/i, "");
    const picked = await dialog.showSaveDialog(mainWindow, {
      title: "Extract to a new folder",
      defaultPath: path.join(path.dirname(file.path), stem),
      buttonLabel: "Extract here",
      properties: ["createDirectory", "showOverwriteConfirmation"],
    });
    if (picked.canceled || !picked.filePath) return { ok: false, canceled: true };
    const result = await archiveCall("extract", { path: file.path, dest: picked.filePath });
    if (result.ok) shell.openPath(result.dest);
    return result;
  }
  return { ok: false, error: "Unknown action." };
});

// Organize and rename (3.1, Library > Storage). The plan stays here between preview and apply,
// so what is moved is exactly what was shown.
const ORGANIZE_JOURNAL = path.join(APPDATA, "organize-journal.jsonl");
let organizePlan = null;

async function organizer(command, body, timeout = 600000) {
  const result = await runPython([path.join(ROOT, "desktop", "organizer.py"), command, LIBRARY_DB], { input: `${JSON.stringify(body)}\n`, timeout });
  return parseJson((result.stdout || "").trim(), null) || { ok: false, error: (result.stderr || "The organizer did not answer.").slice(-300) };
}

ipcMain.handle("organize-plan", async (_event, body) => {
  const plan = await organizer("plan", {
    filters: body?.filters && typeof body.filters === "object" ? body.filters : {},
    root: String(body?.root || ""),
    folders: String(body?.folders || "").slice(0, 200),
    names: String(body?.names || "").slice(0, 200),
    roots: libraryRoots(),
  });
  organizePlan = plan.ok ? plan : null;
  if (!plan.ok) return plan;
  return { ...plan, moves: plan.moves.slice(0, 500) };
});

ipcMain.handle("organize-apply", async (_event, id) => {
  if (!organizePlan || organizePlan.id !== id) return { ok: false, error: "Preview again first." };
  const ready = organizePlan.count - organizePlan.conflicts;
  if (!ready) return { ok: false, error: "Nothing to move." };
  const answer = await dialog.showMessageBox(mainWindow, {
    type: "question", buttons: [`Move ${ready} file${ready === 1 ? "" : "s"}`, "Cancel"], defaultId: 0, cancelId: 1,
    title: "Organize files", message: `Move or rename ${ready} file${ready === 1 ? "" : "s"}?`,
    detail: `${organizePlan.conflicts ? `${organizePlan.conflicts} with a conflict are left where they are. ` : ""}Nothing is overwritten or deleted, and you can undo this from Storage.`,
  });
  if (answer.response !== 0) return { ok: false, canceled: true };
  const plan = organizePlan;
  organizePlan = null;
  return organizer("apply", { plan, jobs: JOBS_DB, journal: ORGANIZE_JOURNAL, roots: libraryRoots(), label: String(plan.label || "") });
});

ipcMain.handle("organize-batches", () => organizer("batches", { journal: ORGANIZE_JOURNAL }, 30000));

ipcMain.handle("organize-undo", async (_event, batch) => {
  const answer = await dialog.showMessageBox(mainWindow, {
    type: "question", buttons: ["Undo", "Cancel"], defaultId: 0, cancelId: 1, title: "Undo organize",
    message: "Move these files back to where they were?", detail: "Files that were moved again or replaced in the meantime are left alone.",
  });
  if (answer.response !== 0) return { ok: false, canceled: true };
  return organizer("undo", { batch: String(batch || ""), jobs: JOBS_DB, journal: ORGANIZE_JOURNAL });
});

// After a run with "Ask" or "Replace after confirmation": one pair at a time from the list.
ipcMain.handle("duplicate-action", async (_event, body) => {
  const target = path.resolve(String(body?.path || ""));
  const root = allowedRoots().find((dir) => dir !== path.resolve(ROOT) && isInside(target, dir));
  if (!root || !existingFile(target)) return { ok: false, error: "That file is not in a download folder any more." };
  const action = String(body?.action || "");
  if (action === "aside" || action === "rename") return dupMove(action, target, root);
  if (action === "trash") return confirmTrash(target, String(body?.reason || "Another file has the same content."));
  if (action === "reveal") {
    shell.showItemInFolder(target);
    return { ok: true };
  }
  return { ok: false, error: "Unknown action." };
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
  app.on("second-instance", (_event, argv) => {
    if (!mainWindow) return;
    const links = linksFromArgv(argv);
    if (links.length) deliverLinks(links);
    else showWindow();
  });

  app.whenReady().then(async () => {
    registerLibraryScheme();
    session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
      callback(permission === "clipboard-sanitized-write" || permission === "clipboard-read");
    });
    createWindow();
    const options = applyAppOptions();
    pendingLinks.push(...linksFromArgv(process.argv));
    mainWindow.webContents.once("did-finish-load", () => {
      const done = updater.lastResult();
      if (done) sendLog(done.message, done.ok ? "ok" : "warning");
      if (pendingLinks.length) deliverLinks(pendingLinks.splice(0));
    });
    scheduleUpdateChecks(options.updateCheck);
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

// Quitting during a download asks the session to stop first, so it saves history and the run can
// be resumed; after STOP_GRACE_MS it is ended anyway.
let quitWaiting = false;
app.on("before-quit", (event) => {
  quitting = true;
  const live = [...sessions.values()].filter((run) => run.child.exitCode === null);
  if (live.length && !quitWaiting) {
    event.preventDefault();
    quitWaiting = true;
    for (const run of live) killSession(false, run.slot);
    let left = live.length;
    const done = () => {
      clearTimeout(timer);
      app.quit();
    };
    const timer = setTimeout(() => {
      for (const run of live) if (run.child.exitCode === null) killTree(run.child.pid, true);
      done();
    }, STOP_GRACE_MS + 500);
    for (const run of live) {
      run.child.once("close", () => {
        left -= 1;
        if (!left) done();
      });
    }
    return;
  }
  clearInterval(clip.timer);
  for (const slot of [...sessions.keys()]) killSession(true, slot);
  if (previewChild) killTree(previewChild.pid, true);
  stopFlare();
  stopTelegramLogin();
  logFile.close();
});
app.on("window-all-closed", () => app.quit());
