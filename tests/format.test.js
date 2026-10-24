// Run: node --test tests
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fmt = require("../electron/renderer/format.js");

test("parseLinks splits, dedupes and rejects torrents", () => {
  const out = fmt.parseLinks("https://a.com/x\n magnet:?xt=1 \nhttps://a.com/x\n<https://b.org/t/1.torrent>\nfoo\n# note\nhttps://c.net/p?a=1, https://d.io/z");
  assert.deepStrictEqual(out.urls, ["https://a.com/x", "https://c.net/p?a=1", "https://d.io/z"]);
  assert.deepStrictEqual(out.unsupported, ["magnet:?xt=1", "https://b.org/t/1.torrent"]);
  assert.deepStrictEqual(out.ignored, ["foo"]);
});

test("bytes and duration format", () => {
  assert.strictEqual(fmt.bytes(0), "0 B");
  assert.strictEqual(fmt.bytes(1536), "1.50 KB");
  assert.strictEqual(fmt.bytes(52428800), "50.0 MB");
  assert.strictEqual(fmt.duration(3725), "1:02:05");
  assert.strictEqual(fmt.duration(65), "1:05");
});

test("speed is shown in MB/s", () => {
  assert.strictEqual(fmt.mbps(3584000), "3.42 MB/s");
  assert.strictEqual(fmt.mbps(52428800), "50.0 MB/s");
  assert.strictEqual(fmt.mbps(0), "0.00 MB/s");
});

test("finishText covers every ending", () => {
  assert.strictEqual(fmt.finishText({ saved: 3 }).tone, "ok");
  assert.match(fmt.finishText({ saved: 3, skipped: 2, failed: 1 }).message, /3 saved, 2 already saved before, 1 failed/);
  assert.strictEqual(fmt.finishText({ emptyReason: "The thread is missing or private." }).message, "The thread is missing or private.");
  assert.match(fmt.finishText({ stopped: true, saved: 4 }).message, /^Stopped/);
});

test("Telegram links without https and chat IDs are accepted", () => {
  const { urls, ignored } = fmt.parseLinks("t.me/somechannel\n-1001988700745\nhttps://t.me/c/1988700745/12\n-12345");
  assert.deepEqual(urls, ["https://t.me/somechannel", "https://t.me/c/1988700745", "https://t.me/c/1988700745/12"]);
  assert.deepEqual(ignored, ["-12345"]);
});
