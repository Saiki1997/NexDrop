"use strict";

// In-app updates from GitHub releases. Check: ask GitHub for the latest release and compare
// versions. Install (Windows only): download NexDrop-windows-x64.zip, check it against the
// release's SHA256SUMS.txt, unpack it next to the app, then quit and let a small script copy
// the new files over this folder. engine\appdata, downloads and logs are never touched.

const { net } = require("electron");
const { spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const fmt = require("./renderer/format.js");

const REPO = "Saiki1997/NexDrop";
const LATEST_URL = `https://api.github.com/repos/${REPO}/releases/latest`;
const RELEASES_PAGE = `https://github.com/${REPO}/releases/latest`;
const ZIP_NAME = "NexDrop-windows-x64.zip";
const SUMS_NAME = "SHA256SUMS.txt";
const FOLDER_IN_ZIP = "NexDrop-windows-x64";

function createUpdater({ root, appdata, version, send, log, isBusy, quit }) {
  const workDir = path.join(appdata, "updates");
  const resultFile = path.join(workDir, "last-result.txt");
  let latest = null;
  let installing = false;

  const headers = { Accept: "application/vnd.github+json", "User-Agent": `NEXDROP/${version}` };

  async function check() {
    let response;
    try {
      response = await net.fetch(LATEST_URL, { headers, cache: "no-store" });
    } catch (error) {
      return { state: "error", current: version, message: `Could not reach GitHub (${error.message}).` };
    }
    if (response.status === 404) return { state: "current", current: version, message: "No release is published yet." };
    if (!response.ok) return { state: "error", current: version, message: `GitHub answered ${response.status}. Try again later.` };
    let release;
    try {
      release = await response.json();
    } catch {
      return { state: "error", current: version, message: "GitHub sent an unreadable answer." };
    }
    const tag = String(release.tag_name || "");
    const assets = Array.isArray(release.assets) ? release.assets : [];
    const zip = assets.find((a) => a && a.name === ZIP_NAME);
    const sums = assets.find((a) => a && a.name === SUMS_NAME);
    latest = {
      version: tag.replace(/^v/i, ""),
      tag,
      notes: String(release.body || "").slice(0, 20000),
      page: String(release.html_url || RELEASES_PAGE),
      zipUrl: zip ? String(zip.browser_download_url) : "",
      zipSize: zip ? Number(zip.size) || 0 : 0,
      sumsUrl: sums ? String(sums.browser_download_url) : "",
      published: String(release.published_at || ""),
    };
    const newer = tag && fmt.compareVersions(tag, version) > 0;
    return {
      state: newer ? "available" : "current",
      current: version,
      version: latest.version,
      notes: latest.notes,
      page: latest.page,
      size: latest.zipSize,
      published: latest.published,
      installable: newer && process.platform === "win32" && Boolean(latest.zipUrl && latest.sumsUrl),
      message: newer ? `NEXDROP ${latest.version} is available.` : `NEXDROP ${version} is the newest version.`,
    };
  }

  async function download(url, dest, total) {
    const response = await net.fetch(url, { headers: { "User-Agent": headers["User-Agent"] } });
    if (!response.ok || !response.body) throw new Error(`The download failed (${response.status}).`);
    const size = Number(response.headers.get("content-length")) || total || 0;
    const hash = crypto.createHash("sha256");
    const out = fs.createWriteStream(dest);
    const reader = response.body.getReader();
    let read = 0;
    let lastSent = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = Buffer.from(value);
        hash.update(chunk);
        read += chunk.length;
        if (!out.write(chunk)) await new Promise((resolve) => out.once("drain", resolve));
        if (Date.now() - lastSent > 250) {
          lastSent = Date.now();
          send({ type: "update-progress", stage: "download", read, total: size });
        }
      }
    } finally {
      await new Promise((resolve) => out.end(resolve));
    }
    send({ type: "update-progress", stage: "download", read, total: size || read });
    return hash.digest("hex");
  }

  function run(command, args) {
    return new Promise((resolve) => {
      let child;
      try {
        child = spawn(command, args, { windowsHide: true, stdio: "ignore" });
      } catch {
        resolve(-1);
        return;
      }
      child.on("error", () => resolve(-1));
      child.on("close", (code) => resolve(code));
    });
  }

  async function extract(zip, dest) {
    fs.rmSync(dest, { recursive: true, force: true });
    fs.mkdirSync(dest, { recursive: true });
    // tar.exe ships with Windows 10 1803 and later and unpacks zips much faster than PowerShell.
    const tar = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe");
    if (fs.existsSync(tar) && (await run(tar, ["-xf", zip, "-C", dest])) === 0) return;
    fs.rmSync(dest, { recursive: true, force: true });
    fs.mkdirSync(dest, { recursive: true });
    const quote = (p) => `'${p.replace(/'/g, "''")}'`;
    const code = await run("powershell.exe", [
      "-NoProfile", "-NonInteractive", "-Command",
      `Expand-Archive -LiteralPath ${quote(zip)} -DestinationPath ${quote(dest)} -Force`,
    ]);
    if (code !== 0) throw new Error("The update could not be unpacked.");
  }

  // cmd.exe expands %NAME%, so a literal % in a folder name has to be doubled.
  const cmdPath = (p) => `"${String(p).replace(/%/g, "%%")}"`;

  function writeScript(source, target) {
    const exe = path.join(target, "NexDrop.exe");
    const log = path.join(workDir, "update.log");
    const lines = [
      "@echo off",
      "setlocal",
      `set PID=${process.pid}`,
      ":wait",
      // timeout.exe refuses to run without a console input, so ping waits instead.
      'tasklist /FI "PID eq %PID%" /NH 2>nul | find " %PID% " >nul && (ping -n 2 127.0.0.1 >nul & goto wait)',
      "ping -n 3 127.0.0.1 >nul",
      `echo %DATE% %TIME% copying ${latest.version} >> ${cmdPath(log)}`,
      // /E copies every folder; nothing in the NEXDROP folder is deleted, and user data folders are skipped.
      `robocopy ${cmdPath(source)} ${cmdPath(target)} /E /R:5 /W:2 /NP /NFL /NDL /XD ${cmdPath(path.join(source, "engine", "appdata"))} ${cmdPath(path.join(source, "downloads"))} ${cmdPath(path.join(source, "logs"))} >> ${cmdPath(log)}`,
      `if %ERRORLEVEL% GEQ 8 (echo failed ${latest.version} > ${cmdPath(resultFile)}) else (echo updated ${latest.version} > ${cmdPath(resultFile)})`,
      `rmdir /s /q ${cmdPath(path.dirname(source))}`,
      `del /q ${cmdPath(path.join(workDir, ZIP_NAME))}`,
      `start "" ${cmdPath(exe)}`,
      "endlocal",
    ];
    const script = path.join(workDir, "apply-update.cmd");
    fs.writeFileSync(script, `${lines.join("\r\n")}\r\n`);
    return script;
  }

  async function install() {
    if (installing) return { ok: false, error: "The update is already downloading." };
    if (process.platform !== "win32") return { ok: false, error: "Updating in the app works on Windows. Download the new version from the release page." };
    if (isBusy()) return { ok: false, error: "Pause or finish the current download first. The update restarts NEXDROP." };
    if (!latest || !latest.zipUrl || !latest.sumsUrl) {
      const status = await check();
      if (status.state !== "available") return { ok: false, error: status.message };
      if (!status.installable) return { ok: false, error: "This release has no Windows zip and checksum to install from. Use the release page." };
    }
    installing = true;
    try {
      fs.mkdirSync(workDir, { recursive: true });
      send({ type: "update-progress", stage: "checksum", read: 0, total: 0 });
      const sumsResponse = await net.fetch(latest.sumsUrl, { headers: { "User-Agent": headers["User-Agent"] } });
      if (!sumsResponse.ok) throw new Error(`The checksum file could not be downloaded (${sumsResponse.status}).`);
      const expected = fmt.checksumFor(await sumsResponse.text(), ZIP_NAME);
      if (!expected) throw new Error("The release's SHA256SUMS.txt has no line for the Windows zip.");

      const zip = path.join(workDir, ZIP_NAME);
      const actual = await download(latest.zipUrl, zip, latest.zipSize);
      if (actual !== expected) {
        fs.rmSync(zip, { force: true });
        throw new Error("The downloaded zip does not match the published SHA-256 checksum, so it was deleted and nothing was changed.");
      }
      log(`Update ${latest.version}: zip downloaded and checksum verified.`);

      send({ type: "update-progress", stage: "unpack", read: 0, total: 0 });
      const staging = path.join(workDir, "staging");
      await extract(zip, staging);
      const source = path.join(staging, FOLDER_IN_ZIP);
      if (!fs.existsSync(path.join(source, "NexDrop.exe"))) throw new Error("The update zip does not contain NexDrop-windows-x64\\NexDrop.exe.");

      const script = writeScript(source, root);
      send({ type: "update-progress", stage: "restart", read: 0, total: 0 });
      log(`Update ${latest.version}: restarting to finish.`);
      const child = spawn("cmd.exe", ["/d", "/c", script], { detached: true, stdio: "ignore", windowsHide: true });
      child.unref();
      setTimeout(quit, 800);
      return { ok: true };
    } catch (error) {
      installing = false;
      return { ok: false, error: error.message };
    }
  }

  /** What the last update script reported, once, after the restart. */
  function lastResult() {
    try {
      const text = fs.readFileSync(resultFile, "utf8").trim();
      fs.rmSync(resultFile, { force: true });
      const [state, ver] = text.split(/\s+/);
      if (state === "updated") return { ok: true, message: `Updated to NEXDROP ${ver}. Settings, sign-ins and downloads were kept.` };
      if (state === "failed") return { ok: false, message: `The update to ${ver} could not copy every file (see engine\\appdata\\updates\\update.log). Close NEXDROP and extract the zip from the release page over this folder.` };
    } catch {
      /* no update happened */
    }
    return null;
  }

  return { check, install, lastResult, releasesPage: RELEASES_PAGE };
}

module.exports = { createUpdater };
