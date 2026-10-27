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

test("compareVersions orders releases and tags", () => {
  assert.ok(fmt.compareVersions("v2.4.0", "2.3.4") > 0);
  assert.ok(fmt.compareVersions("2.3.4", "2.3.10") < 0);
  assert.strictEqual(fmt.compareVersions("v2.3.4", "2.3.4"), 0);
  assert.ok(fmt.compareVersions("2.4.0-beta.1", "2.4.0") < 0);
  assert.ok(fmt.compareVersions("2.4", "2.3.9") > 0);
});

test("inTimeWindow handles windows across midnight", () => {
  const at = (h, m) => new Date(2026, 0, 1, h, m);
  assert.strictEqual(fmt.inTimeWindow(at(2, 0), "01:00", "07:00"), true);
  assert.strictEqual(fmt.inTimeWindow(at(7, 0), "01:00", "07:00"), false);
  assert.strictEqual(fmt.inTimeWindow(at(23, 30), "22:00", "06:00"), true);
  assert.strictEqual(fmt.inTimeWindow(at(5, 59), "22:00", "06:00"), true);
  assert.strictEqual(fmt.inTimeWindow(at(12, 0), "22:00", "06:00"), false);
  assert.strictEqual(fmt.inTimeWindow(at(12, 0), "08:00", "08:00"), true);
  assert.strictEqual(fmt.inTimeWindow(at(12, 0), "bad", "06:00"), true);
  assert.strictEqual(fmt.clockMinutes("24:00"), null);
});

test("appLinkUrls keeps only web links from nexdrop:// addresses", () => {
  const address = `nexdrop://add?url=${encodeURIComponent("https://bunkr.cr/a/x")}&url=${encodeURIComponent("javascript:alert(1)")}&url=${encodeURIComponent("https://bunkr.cr/a/x")}&url=${encodeURIComponent("http://t.me/s/chan")}`;
  assert.deepStrictEqual(fmt.appLinkUrls(address), ["https://bunkr.cr/a/x", "http://t.me/s/chan"]);
  assert.deepStrictEqual(fmt.appLinkUrls("https://example.com/?url=https://a.b/c"), []);
  assert.deepStrictEqual(fmt.appLinkUrls("not a url"), []);
  const many = `nexdrop://add?${Array.from({ length: 80 }, (_, i) => `url=https://a.com/${i}`).join("&")}`;
  assert.strictEqual(fmt.appLinkUrls(many).length, 50);
});

test("checksumFor reads SHA256SUMS.txt", () => {
  const hash = "a".repeat(64);
  const text = `${"b".repeat(64)}  NexDrop-source.zip\n${hash.toUpperCase()}  NexDrop-windows-x64.zip\n`;
  assert.strictEqual(fmt.checksumFor(text, "NexDrop-windows-x64.zip"), hash);
  assert.strictEqual(fmt.checksumFor(text, "other.zip"), "");
});

test("download modes resolve to fixed limits, custom keeps the user's numbers", () => {
  assert.deepStrictEqual(fmt.modeLimits("balanced"), { maxDownloads: 8, perHost: 4, serverSlots: 2, segments: 2 });
  assert.deepStrictEqual(fmt.modeLimits("low", { maxDownloads: 30 }), { maxDownloads: 3, perHost: 2, serverSlots: 1, segments: 0 });
  assert.deepStrictEqual(fmt.modeLimits("custom", { maxDownloads: 40, perHost: "5", serverSlots: 0, segments: 12 }), { maxDownloads: 32, perHost: 5, serverSlots: 1, segments: 8 });
  assert.deepStrictEqual(fmt.modeLimits("nonsense", {}), { maxDownloads: 16, perHost: 6, serverSlots: 3, segments: 4 });
  assert.strictEqual(fmt.guessMode({ maxDownloads: 16, perHost: 6, serverSlots: 3 }), "auto");
  assert.strictEqual(fmt.guessMode({}), "auto");
  assert.strictEqual(fmt.guessMode({ maxDownloads: 4, perHost: 6 }), "custom");
});

test("filename templates match the engine's rules", () => {
  const v = { title: "Summer Shoot", original: "IMG_1", thread: "Lala Thread", creator: "lala", date: "2026-10-27", site: "bunkr", type: "image" };
  assert.strictEqual(fmt.renderTemplate("{creator}-{title}-{number}", v, "007"), "lala-Summer Shoot-007");
  assert.strictEqual(fmt.renderTemplate("{thread}-{post}-{number}", v, "007"), "Lala Thread-007");
  assert.strictEqual(fmt.renderTemplate("[{site}] {title}", v, ""), "[bunkr] Summer Shoot");
  assert.strictEqual(fmt.renderTemplate("{album}-{number}", v, "007"), "");
  assert.strictEqual(fmt.renderTemplate("{title}-{number}.{ext}", v, "0001"), "Summer Shoot-0001");
  assert.strictEqual(fmt.templateProblem("{creator}-{title}-{number}"), "");
  assert.match(fmt.templateProblem("{artist}-{title}"), /unknown variable \{artist\}/);
  assert.match(fmt.templateProblem("{date}-{number}"), /no name variable/);
  assert.match(fmt.templateProblem("{title}/{number}"), /Windows/);
});

test("job files: exports round-trip, link lists become one job, unknown folders are dropped", () => {
  const job = { id: "j1", label: "Lala", urls: ["https://simpcity.cr/threads/a.1/"], unsupported: [], startPage: 2, endPage: 1,
    redownload: false, folder: "D:\\Media\\Lala", mode: "low", speedMB: 5, hold: true, resumed: true, doneUrls: ["x"] };
  const exported = fmt.exportJob(job);
  assert.strictEqual(exported.id, undefined);
  assert.strictEqual(exported.doneUrls, undefined);
  const text = JSON.stringify({ format: "nexdrop-jobs", version: 1, jobs: [exported, { urls: [] }] });
  const back = fmt.parseJobFile("jobs.json", text, ["d:/media/lala/"]);
  assert.strictEqual(back.kind, "jobs");
  assert.strictEqual(back.jobs.length, 1);
  assert.strictEqual(back.dropped, 1);
  assert.deepStrictEqual(back.jobs[0], { urls: ["https://simpcity.cr/threads/a.1/"], unsupported: [], label: "Lala", startPage: 2,
    endPage: null, redownload: false, folder: "D:\\Media\\Lala", mode: "low", speedMB: 5, hold: true });
  const elsewhere = fmt.parseJobFile("jobs.json", text, ["C:/Downloads"]);
  assert.strictEqual(elsewhere.jobs[0].folder, undefined);
  assert.strictEqual(elsewhere.dropped, 2);
  const m3u = fmt.parseJobFile("list.m3u", "#EXTM3U\n#EXTINF:-1,One\nhttps://a.com/1.mp4\nhttps://a.com/2.mp4\n", []);
  assert.deepStrictEqual(m3u.jobs[0].urls, ["https://a.com/1.mp4", "https://a.com/2.mp4"]);
  const csv = fmt.parseJobFile("l.csv", "name,url\none,https://b.com/x\ntwo,\"https://b.com/y\"\n", []);
  assert.deepStrictEqual(csv.jobs[0].urls, ["https://b.com/x", "https://b.com/y"]);
  assert.deepStrictEqual(fmt.parseJobFile("l.json", '[{"url":"https://c.com/z"}]', []).jobs[0].urls, ["https://c.com/z"]);
  assert.strictEqual(fmt.parseJobFile("bad.json", "{nope", []).jobs.length, 0);
});

test("diagnostics redaction removes secrets and the user name", () => {
  const raw = [
    "GET https://cdn.bunkr.ru/a.mp4?token=abc123&expires=999&n=1 -> 403",
    ".simpcity.cr\tTRUE\t/\tTRUE\t1900000000\txf_user\t12345%2Csecretvalue",
    "Cookie: xf_user=12345; xf_session=zzz",
    '{"api_key": "pk_live_1", "maxDownloads": 8}',
    "proxy http://bob:hunter2@127.0.0.1:8080",
    "mail me at saiki@example.com",
    "Saved to C:\\Users\\Saiki\\Downloads\\x.jpg",
  ].join("\n");
  const out = fmt.redact(raw, "C:\\Users\\Saiki");
  for (const secret of ["abc123", "secretvalue", "zzz", "pk_live_1", "hunter2", "saiki@example.com", "Saiki\\"]) assert.ok(!out.includes(secret), secret);
  assert.ok(out.includes("n=1"));
  assert.ok(out.includes('"maxDownloads": 8'));
  assert.ok(out.includes("~\\Downloads\\x.jpg"));
});

test("duplicate links with other tracking parameters are left out (3.1)", () => {
  assert.strictEqual(fmt.normalizeUrl("https://www.Cyberdrop.cr/a/xyz/?utm_source=x&page=2#top"), "https://cyberdrop.cr/a/xyz?page=2");
  assert.strictEqual(fmt.normalizeUrl("https://site.test/search?s=cats"), "https://site.test/search?s=cats");
  assert.strictEqual(fmt.normalizeUrl("https://x.com/u/status/1?s=20&t=abc"), "https://x.com/u/status/1");
  const parsed = fmt.parseLinks("https://a.test/x?utm_source=1\nhttps://a.test/x/\nhttps://b.test/y\nhttps://b.test/y");
  assert.deepStrictEqual(parsed.urls, ["https://a.test/x?utm_source=1", "https://b.test/y"]);
  assert.deepStrictEqual(parsed.duplicates, ["https://a.test/x/"]);
});
