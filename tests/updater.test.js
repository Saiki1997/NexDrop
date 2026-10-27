// Run: node --test tests
"use strict";

// The updater's rollback (3.1): only app files whose content the update changes are saved,
// user-data folders are never read, and the saved copy is offered only for the version it undoes.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const realLoad = Module._load;
Module._load = function load(request, ...rest) {
  if (request === "electron") return { net: { fetch: async () => ({ ok: false }) } };
  return realLoad.call(this, request, ...rest);
};
const { createUpdater } = require("../electron/updater.js");

function tree(root, files) {
  for (const [rel, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
    fs.writeFileSync(path.join(root, rel), text);
  }
}

test("rollback keeps only the app files the update changes", async () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "nx-upd-"));
  const app = path.join(base, "app");
  const source = path.join(base, "new");
  tree(app, { "NexDrop.exe": "exe-1", "desktop/a.py": "same", "desktop/b.py": "old b", "engine/appdata/jobs.sqlite": "user data" });
  tree(source, { "NexDrop.exe": "exe-2", "desktop/a.py": "same", "desktop/b.py": "new b!", "desktop/c.py": "new file", "engine/appdata/jobs.sqlite": "x" });
  const updater = createUpdater({ root: app, appdata: path.join(app, "engine", "appdata"), version: "3.0.0", send() {}, log() {}, isBusy: () => false, quit() {} });
  const saved = await updater.prepareRollback(source, app, "3.1.0");
  assert.strictEqual(saved, 2);
  const kept = path.join(app, "engine", "appdata", "updates", "rollback");
  const info = JSON.parse(fs.readFileSync(path.join(kept, "rollback.json"), "utf8"));
  assert.deepStrictEqual(info.files.sort(), ["NexDrop.exe", path.join("desktop", "b.py")].sort());
  assert.strictEqual(fs.readFileSync(path.join(kept, "files", "desktop", "b.py"), "utf8"), "old b");
  assert.ok(!fs.existsSync(path.join(kept, "files", "engine")), "user data is never part of a rollback");
  // offered only once the new version runs
  assert.strictEqual(updater.rollbackAvailable(), null);
  const after = createUpdater({ root: app, appdata: path.join(app, "engine", "appdata"), version: "3.1.0", send() {}, log() {}, isBusy: () => false, quit() {} });
  assert.strictEqual(after.rollbackAvailable().from, "3.0.0");
  fs.rmSync(base, { recursive: true, force: true });
});
