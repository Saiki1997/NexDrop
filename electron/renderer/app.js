/* NEXDROP window. Talks to the main process through window.api (see preload.js). */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const api = window.api;
  const fmt = window.fmt;

  const ROW_H = 64;
  const OVERSCAN = 6;
  const LOG_KEEP = 2000;
  const LOG_DOM_MAX = 500;
  const ACTIVE_STATES = new Set(["queued", "active", "retrying"]);
  const STATE_LABEL = { queued: "Waiting", active: "Downloading", retrying: "Retrying", done: "Saved", skipped: "Already saved", failed: "Failed", cancelled: "Stopped" };

  // ─── state ────────────────────────────────────────────────────────────────
  const run = {
    running: false,
    log: "",
    startedAt: 0,
    root: "",
    phase: "",
    jobId: "",         // job the rows belong to, so Resume can continue them
    doneBefore: new Set(), // urls saved before a pause
    bytesBefore: 0,    // bytes saved before a pause
    rows: new Map(),   // url -> row model
    order: [],         // row models in display order
    needsSort: false,
    arrival: 0,
    bytes: 0,
    speed: 0,
    failedUrls: [],
  };
  const view = { filter: "all", host: "", list: [], dirty: true, hosts: new Map() };
  let prefs = {};
  let cookieFiles = [];
  let downloadsDir = "";

  // ─── preferences ──────────────────────────────────────────────────────────
  const PREF_FIELDS = {
    orderMode: { get: () => $("#order-seg [aria-checked=true]")?.dataset.order || "parallel", set: (v) => setOrder(v) },
    sequencePrefix: { el: "#sequence-prefix", type: "check", def: true },
    deepScrape: { el: "#deep-scrape", type: "check", def: false },
    flareSolver: { el: "#flare", type: "check", def: true },
    threadDepth: { el: "#thread-depth", type: "int", def: 0, min: 0, max: 3 },
    maxDownloads: { el: "#max-downloads", type: "int", def: 16, min: 1, max: 32 },
    perHost: { el: "#per-host", type: "int", def: 6, min: 1, max: 16 },
    serverSlots: { el: "#server-slots", type: "int", def: 3, min: 1, max: 6 },
    tiktokAudio: { el: "#tiktok-audio", type: "check", def: false },
    autoRetry: { el: "#auto-retry", type: "check", def: true },
    typeFolders: { el: "#type-folders", type: "check", def: false },
    typeImages: { el: "#type-images", type: "check", def: true },
    typeVideos: { el: "#type-videos", type: "check", def: true },
    typeAudio: { el: "#type-audio", type: "check", def: true },
    typeOther: { el: "#type-other", type: "check", def: true },
    minImageKB: { el: "#min-image-kb", type: "int", def: 0, min: 0, max: 100000 },
    attempts: { el: "#attempts", type: "int", def: 2, min: 1, max: 3 },
    speedLimitMB: { el: "#speed-limit", type: "int", def: 0, min: 0, max: 1000 },
    connectionTimeout: { el: "#connect-timeout", type: "int", def: 15, min: 10, max: 30 },
    readTimeout: { el: "#read-timeout", type: "int", def: 60, min: 30, max: 300 },
  };

  function readField(spec) {
    if (spec.get) return spec.get();
    const el = $(spec.el);
    if (!el) return spec.def;
    if (spec.type === "check") return el.checked;
    const n = parseInt(el.value, 10);
    return Number.isFinite(n) ? Math.max(spec.min, Math.min(spec.max, n)) : spec.def;
  }

  function writeField(spec, value) {
    if (spec.set) return spec.set(value);
    const el = $(spec.el);
    if (!el) return;
    if (spec.type === "check") el.checked = value === undefined ? spec.def : Boolean(value);
    else el.value = value === undefined || value === null ? spec.def : value;
  }

  function collectPrefs() {
    return { ...Object.fromEntries(Object.entries(PREF_FIELDS).map(([key, spec]) => [key, readField(spec)])), version: 2 };
  }

  let prefTimer = 0;
  function savePrefsSoon() {
    clearTimeout(prefTimer);
    prefTimer = setTimeout(() => {
      prefs = collectPrefs();
      api.saveGuiSetting({ prefs });
    }, 300);
  }

  function setOrder(mode) {
    const valid = ["source", "parallel", "fastest"].includes(mode) ? mode : "parallel";
    $$("#order-seg button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.order === valid)));
  }

  // ─── links box ────────────────────────────────────────────────────────────
  const urlsBox = $("#urls");
  function autoGrow() {
    urlsBox.style.height = "auto";
    urlsBox.style.height = `${Math.min(156, Math.max(44, urlsBox.scrollHeight + 2))}px`;
    const { urls } = fmt.parseLinks(urlsBox.value);
    const count = $("#links-count");
    count.hidden = urls.length < 2;
    count.textContent = `${urls.length} links`;
  }
  urlsBox.addEventListener("input", autoGrow);
  urlsBox.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      startRun();
    }
  });

  function addLinks(text, replace) {
    const { urls, unsupported } = fmt.parseLinks(text);
    const lines = [...urls, ...unsupported];
    if (!lines.length) return 0;
    const existing = replace ? [] : urlsBox.value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    urlsBox.value = [...new Set([...existing, ...lines])].join("\n");
    autoGrow();
    return lines.length;
  }

  $("#btn-import").addEventListener("click", () => $("#import-file").click());
  $("#import-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const added = addLinks(await file.text(), false);
    log(added ? "info" : "warning", added ? `Added ${added} links from ${file.name}.` : `${file.name} has no links.`);
  });

  // Drop a .txt file or links anywhere on the window.
  document.addEventListener("dragover", (event) => event.preventDefault());
  document.addEventListener("drop", async (event) => {
    event.preventDefault();
    const file = event.dataTransfer?.files?.[0];
    const text = file && /\.txt$/i.test(file.name) ? await file.text() : event.dataTransfer?.getData("text") || "";
    if (addLinks(text, false)) urlsBox.focus();
  });

  // ─── jobs: one run of one or more links ────────────────────────────────────
  // A job is {id, urls, unsupported, label, startPage, endPage, redownload, watchId}.
  // The queue, resume after pause, and watched threads all go through runJob().
  let queue = [];
  let watches = [];
  let threadPosts = {};
  let activeJob = null;

  function jobLabel(urls) {
    const first = urls[0] || "";
    try {
      const u = new URL(first);
      const parts = u.pathname.split("/").filter(Boolean);
      const thread = parts.indexOf("threads");
      const name = thread >= 0 ? parts[thread + 1] : parts[parts.length - 1];
      const label = decodeURIComponent(name || u.hostname).replace(/\.\d+$/, "").replace(/[-_]+/g, " ");
      return urls.length > 1 ? `${label} + ${urls.length - 1} more` : label;
    } catch {
      return first.slice(0, 40);
    }
  }

  function jobFromInput() {
    const { urls, unsupported } = fmt.parseLinks(urlsBox.value);
    if (!urls.length && !unsupported.length) {
      urlsBox.focus();
      flash(urlsBox);
      return null;
    }
    const startPage = Math.max(1, parseInt($("#start-page").value, 10) || 1);
    const endPage = parseInt($("#end-page").value, 10) || null;
    if (endPage && endPage < startPage) {
      log("error", "The last page is lower than the first page.");
      $("#end-page").focus();
      return null;
    }
    return {
      id: `j${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
      urls,
      unsupported,
      label: jobLabel(urls),
      startPage,
      endPage,
      redownload: $("#redownload").checked,
    };
  }

  async function startRun() {
    if (run.running) return addToQueue();
    if (!urlsBox.value.trim() && queue.length && $("#resume").hidden) return startNextInQueue();
    const job = jobFromInput();
    if (!job) return;
    if (!job.redownload && job.urls.length) {
      const states = await api.lookupUrlState(job.urls).catch(() => []);
      const seen = states.filter((s) => s && s.known);
      if (seen.length) {
        const choice = await askSeen(seen, job.urls.length);
        if (choice === "cancel") return;
        job.redownload = choice === "again";
      }
    }
    runJob(job);
  }

  function runJob(job) {
    prefs = collectPrefs();
    api.saveGuiSetting({ prefs });
    activeJob = job;
    api.saveGuiSetting({ active_run: { ...job, savedAt: new Date().toISOString() } });
    hideResume();
    // Resume continues the paused list instead of starting a fresh one.
    const continuing = Boolean(job.resumed) && run.jobId === job.id && run.order.length > 0;
    resetRun(continuing);
    run.jobId = job.id;
    run.doneBefore = new Set(job.resumed ? job.doneUrls || [] : []);
    run.bytesBefore = job.resumed ? Number(job.bytesSoFar) || run.bytes : 0;
    run.bytes = run.bytesBefore;
    setRunning(true);
    if (job.resumed) {
      log("phase", `Resuming: ${job.label}`);
      log("info", "Files saved before the pause are skipped and half-finished files continue where they stopped. The links are read again from the start to find the rest.");
    } else {
      log("phase", job.watchId ? `Checking watched thread: ${job.label}` : `Starting: ${job.label}`);
    }
    api.startPipeline({
      urls: job.urls,
      unsupported: job.unsupported || [],
      downloadsDir,
      redownload: Boolean(job.redownload),
      startPage: job.startPage || 1,
      endPage: job.endPage || null,
      cookieFiles,
      orderMode: prefs.orderMode,
      sequencePrefix: prefs.sequencePrefix,
      deepScrape: prefs.deepScrape,
      flareSolver: prefs.flareSolver,
      threadDepth: prefs.threadDepth,
      maxDownloads: prefs.maxDownloads,
      perHost: prefs.perHost,
      serverSlots: prefs.serverSlots,
      tiktokAudio: prefs.tiktokAudio,
      attempts: prefs.attempts,
      autoRetry: prefs.autoRetry,
      typeFolders: prefs.typeFolders,
      fileTypes: { images: prefs.typeImages, videos: prefs.typeVideos, audio: prefs.typeAudio, other: prefs.typeOther },
      minImageKB: prefs.minImageKB,
      skipUrls: job.doneUrls || [],
      speedLimit: prefs.speedLimitMB * 1024 * 1024,
      connectionTimeout: prefs.connectionTimeout,
      readTimeout: prefs.readTimeout,
    });
  }

  // Called when a run ends. Paused runs stay saved for Resume; finished ones start the next job.
  function jobEnded(event) {
    const job = activeJob;
    activeJob = null;
    if (!job) return;
    if (event && event.stopped) {
      // Remember what finished, so Resume skips it even when the run uses Download again.
      const done = run.order.filter((r) => r.state === "done" || r.state === "skipped").map((r) => r.url);
      const paused = { ...job, doneUrls: [...new Set([...(job.doneUrls || []), ...done])] };
      paused.savedSoFar = paused.doneUrls.length;
      paused.bytesSoFar = run.bytes;
      api.saveGuiSetting({ active_run: paused });
      showResume(paused);
      return;
    }
    api.saveGuiSetting({ active_run: null });
    if (job.watchId) watchFinished(job.watchId, event);
    setTimeout(startNextInQueue, 1500);
  }

  // ─── queue ────────────────────────────────────────────────────────────────
  function saveQueue() {
    api.saveGuiSetting({ queue });
    renderQueue();
  }

  function addToQueue(job) {
    const item = job || jobFromInput();
    if (!item) return;
    if (queue.some((q) => q.urls.join("\n") === item.urls.join("\n"))) {
      log("info", `${item.label} is already in the queue.`);
      return;
    }
    queue.push(item);
    saveQueue();
    if (!job) {
      urlsBox.value = "";
      autoGrow();
      log("info", `Added to the queue: ${item.label}.`);
    }
  }

  function startNextInQueue() {
    if (run.running || !queue.length) return;
    if (!$("#resume").hidden) return; // a paused run waits for Resume or Discard first
    const next = queue.shift();
    saveQueue();
    runJob(next);
  }

  function renderQueue() {
    const box = $("#queue");
    const list = $("#queue-list");
    list.textContent = "";
    for (const item of queue) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = item.label;
      name.title = item.urls.join("\n");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "queue-remove";
      remove.setAttribute("aria-label", `Remove ${item.label} from the queue`);
      remove.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6 6l8 8M14 6l-8 8"/></svg>';
      remove.addEventListener("click", () => {
        queue = queue.filter((q) => q.id !== item.id);
        saveQueue();
      });
      li.append(name, remove);
      list.appendChild(li);
    }
    $("#queue-count").textContent = queue.length === 1 ? "1 link waiting" : `${queue.length} waiting`;
    box.hidden = !queue.length;
    $("#btn-queue").hidden = !run.running;
  }

  $("#btn-queue").addEventListener("click", () => addToQueue());
  $("#btn-queue-clear").addEventListener("click", () => {
    queue = [];
    saveQueue();
  });

  // ─── pause / resume ───────────────────────────────────────────────────────
  let pausedJob = null;

  function showResume(job) {
    pausedJob = job;
    const done = job.savedSoFar ? `${job.savedSoFar.toLocaleString()} files saved so far` : "not finished";
    $("#resume-text").textContent = `Paused: ${job.label}, ${done}.`;
    $("#resume").hidden = false;
  }

  function hideResume() {
    pausedJob = null;
    $("#resume").hidden = true;
  }

  $("#btn-resume").addEventListener("click", () => {
    if (!pausedJob || run.running) return;
    // History skips finished files; half-downloaded files continue where they stopped.
    runJob({ ...pausedJob, resumed: true });
  });
  $("#btn-discard").addEventListener("click", () => {
    hideResume();
    api.saveGuiSetting({ active_run: null });
    startNextInQueue();
  });

  function stopRun() {
    if (!run.running) return;
    $("#btn-stop").disabled = true;
    $("#btn-stop").textContent = "Pausing…";
    api.stopPipeline();
  }

  $("#btn-start").addEventListener("click", startRun);
  $("#btn-stop").addEventListener("click", stopRun);

  // ─── watched threads ──────────────────────────────────────────────────────
  const WATCH_TICK_MS = 60 * 1000;

  function threadKey(url) {
    const match = String(url).match(/^(https?:\/\/[^/]+\/threads\/[^/?#]+)/i);
    return match ? match[1].toLowerCase().replace(/^http:/, "https:") : "";
  }

  function rememberLastPost(url) {
    const key = threadKey(url);
    if (!key || !/\/post-\d+/.test(url)) return;
    threadPosts[key] = url;
    api.saveGuiSetting({ thread_posts: threadPosts });
  }

  function saveWatches() {
    api.saveGuiSetting({ watches });
    renderWatches();
    updateWatchButton();
  }

  function addWatch(url, hours) {
    const key = threadKey(url);
    if (!key) {
      setStatus("#watch-status", "Only forum thread links (with /threads/ in them) can be watched.", "bad");
      return false;
    }
    if (watches.some((w) => threadKey(w.url) === key)) {
      setStatus("#watch-status", "That thread is already watched.", null);
      return false;
    }
    watches.push({ id: `w${Date.now().toString(36)}`, url: key + "/", label: jobLabel([key]), everyHours: hours || 6, lastCheck: null, lastNew: null });
    saveWatches();
    setStatus("#watch-status", `Watching ${jobLabel([key])}. NEXDROP checks it while it is open.`, "ok");
    return true;
  }

  function watchJob(watch) {
    // Start from the last post seen, so only newer posts are scanned.
    const from = threadPosts[threadKey(watch.url)] || watch.url;
    return { id: `j${Date.now().toString(36)}`, urls: [from], unsupported: [], label: watch.label, startPage: 1, endPage: null, redownload: false, watchId: watch.id };
  }

  function checkWatch(watch) {
    if (queue.some((q) => q.watchId === watch.id) || (activeJob && activeJob.watchId === watch.id)) return;
    addToQueue(watchJob(watch));
    if (!run.running) startNextInQueue();
  }

  function watchFinished(id, event) {
    const watch = watches.find((w) => w.id === id);
    if (!watch) return;
    watch.lastCheck = new Date().toISOString();
    watch.lastNew = event ? event.saved || 0 : 0;
    watch.lastFailed = Boolean(event && event.failed && !event.saved && !event.skipped);
    saveWatches();
    if (watch.lastFailed) log("warning", `${watch.label}: the check failed. It runs again at the next interval, or use Check now.`);
    else if (watch.lastNew) log("ok", `${watch.label}: ${watch.lastNew} new file(s).`);
    else log("info", `${watch.label}: no new files.`);
  }

  function dueWatches() {
    const now = Date.now();
    return watches.filter((w) => !w.lastCheck || now - Date.parse(w.lastCheck) >= w.everyHours * 3600 * 1000);
  }

  setInterval(() => {
    if (run.running || !$("#resume").hidden) return;
    dueWatches().forEach(checkWatch);
  }, WATCH_TICK_MS);

  function renderWatches() {
    const list = $("#watch-list");
    list.textContent = "";
    if (!watches.length) {
      const li = document.createElement("li");
      li.className = "empty-note";
      li.textContent = "No threads watched yet.";
      list.appendChild(li);
      return;
    }
    for (const watch of watches) {
      const li = document.createElement("li");
      li.className = "watch-item";
      const info = document.createElement("div");
      info.className = "watch-info";
      const name = document.createElement("strong");
      name.textContent = watch.label;
      name.title = watch.url;
      const meta = document.createElement("small");
      meta.textContent = !watch.lastCheck
        ? "Not checked yet"
        : watch.lastFailed
          ? `Check failed ${fmt.ago(watch.lastCheck)}`
          : `Checked ${fmt.ago(watch.lastCheck)}, ${watch.lastNew ? `${watch.lastNew} new file(s)` : "nothing new"}`;
      info.append(name, meta);
      const every = document.createElement("select");
      every.className = "select";
      every.setAttribute("aria-label", `How often to check ${watch.label}`);
      for (const h of [1, 3, 6, 12, 24]) {
        const option = document.createElement("option");
        option.value = String(h);
        option.textContent = h === 1 ? "Every hour" : `Every ${h} hours`;
        every.appendChild(option);
      }
      every.value = String(watch.everyHours);
      every.addEventListener("change", () => {
        watch.everyHours = Number(every.value);
        saveWatches();
      });
      const now = document.createElement("button");
      now.type = "button";
      now.className = "btn btn-quiet btn-small";
      now.textContent = "Check now";
      now.addEventListener("click", () => checkWatch(watch));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-quiet btn-small btn-danger";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        watches = watches.filter((w) => w.id !== watch.id);
        saveWatches();
      });
      li.append(info, every, now, remove);
      list.appendChild(li);
    }
  }

  // "Watch this thread" appears after running a single forum thread.
  let lastThread = "";
  function updateWatchButton() {
    const key = threadKey(lastThread);
    const button = $("#btn-watch");
    button.hidden = !key || run.running;
    const watched = watches.some((w) => threadKey(w.url) === key);
    button.textContent = watched ? "Watched" : "Watch for new posts";
    button.disabled = watched;
  }
  $("#btn-watch").addEventListener("click", () => {
    if (addWatch(lastThread, 6)) log("ok", "Watching this thread. New posts are downloaded every 6 hours while NEXDROP is open.");
  });
  $("#btn-watch-add").addEventListener("click", () => {
    const value = $("#watch-url").value.trim();
    if (value && addWatch(value, Number($("#watch-every").value) || 6)) $("#watch-url").value = "";
  });

  function askSeen(seen, total) {
    const dialog = $("#seen-dialog");
    const latest = seen.sort((a, b) => String(b.last_run).localeCompare(String(a.last_run)))[0];
    const which = seen.length === 1 && total === 1 ? "This link was" : `${seen.length} of these links were`;
    const saved = Number(latest.saved || 0) + Number(latest.skipped || 0);
    $("#seen-text").textContent =
      `${which} downloaded ${fmt.ago(latest.last_run) || "before"}${saved ? `, with ${saved} ${saved === 1 ? "file" : "files"}` : ""}. ` +
      "Getting new files only skips everything already saved.";
    dialog.returnValue = "";
    dialog.showModal();
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue || "cancel"), { once: true });
    });
  }

  function stopRun() {
    if (!run.running) return;
    $("#btn-stop").disabled = true;
    $("#btn-stop").textContent = "Stopping…";
    api.stopPipeline();
  }

  $("#btn-start").addEventListener("click", startRun);
  $("#btn-stop").addEventListener("click", stopRun);

  function setRunning(on) {
    run.running = on;
    $("#app").dataset.state = on ? "running" : run.order.length ? "done" : "idle";
    $("#btn-start").hidden = on;
    $("#btn-stop").hidden = !on;
    $("#btn-stop").disabled = false;
    $("#btn-stop").textContent = "Pause";
    $("#btn-queue").hidden = !on;
    $("#btn-watch").hidden = true;
    if (!on) updateWatchButton();
    if (on) {
      run.startedAt = Date.now();
      tickClock();
    }
  }

  // keep: a resumed run keeps the paused rows and log; unfinished rows go back to Waiting.
  function resetRun(keep = false) {
    run.speed = 0;
    run.failedUrls = [];
    run.phase = "";
    $("#phases").className = "phases";
    setPhase("");
    if (keep) {
      for (const row of run.order) {
        if (row.state !== "done" && row.state !== "skipped") Object.assign(row, { state: "queued", speed: 0, reason: "" });
      }
      view.dirty = true;
      scheduleRender();
      return;
    }
    run.rows.clear();
    run.order = [];
    run.arrival = 0;
    run.bytes = 0;
    view.hosts.clear();
    view.filter = "all";
    view.host = "";
    $$("#file-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.filter === "all")));
    $("#host-filter").innerHTML = '<option value="">All sites</option>';
    $("#log").textContent = "";
    logEntries.length = 0;
    $("#list").scrollTop = 0;
    view.dirty = true;
    scheduleRender();
  }

  // ─── phases ───────────────────────────────────────────────────────────────
  const PHASES = ["start", "crawl", "download", "finalize"];
  function setPhase(phase) {
    run.phase = phase;
    const index = PHASES.indexOf(phase);
    $$("#phases li").forEach((li, i) => {
      li.classList.toggle("done", index >= 0 && i < index);
      li.classList.toggle("current", i === index);
    });
  }

  // ─── pipeline events ──────────────────────────────────────────────────────
  api.onPipelineEvent((event) => {
    switch (event.type) {
      case "run":
        run.log = event.log || "";
        run.root = event.root || run.root;
        if (run.root) showFolder(run.root);
        break;
      case "phase":
        setPhase(event.stage);
        log("phase", event.message);
        break;
      case "log":
        log(event.level || "info", event.message);
        break;
      case "files":
        applyFiles(event);
        break;
      case "last_post":
        rememberLastPost(event.url || "");
        break;
      case "flare":
        showFlare(event.state, event.message, true);
        break;
      case "error":
        log("error", event.message);
        break;
      case "finished":
        finish(event);
        break;
      case "exit":
        if (run.running) {
          // The session ended without a summary (crash or forced stop).
          if (event.code) log("error", `The download engine stopped unexpectedly (code ${event.code}). Details are in engine\\appdata\\logs.`);
          $("#phases").classList.add("failed");
          setRunning(false);
          loadRecent();
          jobEnded({ stopped: true });
        }
        break;
      default:
        break;
    }
  });

  function applyFiles(event) {
    for (const item of event.items || []) {
      let row = run.rows.get(item.url);
      if (!row) {
        row = { url: item.url, arrival: run.arrival++, sequence: 0 };
        run.rows.set(item.url, row);
        run.order.push(row);
        run.needsSort = true;
      }
      if (item.sequence && item.sequence !== row.sequence) run.needsSort = true;
      // A file saved before the pause stays "Saved" when the resumed run skips it.
      const savedBefore = item.state === "skipped" && (row.state === "done" || run.doneBefore.has(item.url));
      Object.assign(row, item, savedBefore ? { state: "done", reason: "" } : {});
      if (!view.hosts.has(row.host)) addHost(row.host);
    }
    if (event.bytes) run.bytes = run.bytesBefore + event.bytes;
    run.speed = event.speed || 0;
    view.dirty = true;
    scheduleRender();
  }

  function finish(event) {
    run.failedUrls = event.failedUrls || [];
    if (event.bytes) run.bytes = run.bytesBefore + event.bytes;
    run.speed = 0;
    run.root = event.root || run.root;
    const result = fmt.finishText(event);
    log(result.tone === "ok" ? "ok" : "warning", result.message);
    const phases = $("#phases");
    const anySaved = event.saved || event.skipped;
    if (!event.stopped && anySaved) setPhase("finalize");
    if (!event.stopped && !event.failed && anySaved) phases.classList.add("complete");
    else if (event.stopped || anySaved) phases.classList.add("partial");
    else phases.classList.add("failed");
    lastThread = activeJob && activeJob.urls.length === 1 && !activeJob.watchId ? activeJob.urls[0] : "";
    setRunning(false);
    view.dirty = true;
    scheduleRender();
    loadRecent();
    jobEnded(event);
  }

  // ─── file list (virtualised: only visible rows exist in the DOM) ──────────
  const listEl = $("#list");
  const rowsEl = $("#list-rows");
  const spacer = $("#list-spacer");
  const pool = [];
  let frame = 0;

  function scheduleRender() {
    if (!frame) frame = requestAnimationFrame(render);
  }

  function matches(row) {
    if (view.host && row.host !== view.host) return false;
    switch (view.filter) {
      case "active": return ACTIVE_STATES.has(row.state);
      case "done": return row.state === "done";
      case "skipped": return row.state === "skipped";
      case "failed": return row.state === "failed" || row.state === "cancelled";
      default: return true;
    }
  }

  function sortRows() {
    // Page order first (sequence number), then the order files appeared in.
    run.order.sort((a, b) => (a.sequence || Infinity) - (b.sequence || Infinity) || a.arrival - b.arrival);
    run.needsSort = false;
  }

  function render() {
    frame = 0;
    if (view.dirty) {
      if (run.needsSort) sortRows();
      view.list = run.order.filter(matches);
      updateCounts();
      updateTally();
      view.dirty = false;
    }
    $("#empty").hidden = run.order.length > 0 || run.running;
    spacer.style.height = `${view.list.length * ROW_H}px`;
    const first = Math.max(0, Math.floor(listEl.scrollTop / ROW_H) - OVERSCAN);
    const count = Math.ceil(listEl.clientHeight / ROW_H) + OVERSCAN * 2;
    const slice = view.list.slice(first, first + count);
    rowsEl.style.transform = `translateY(${first * ROW_H}px)`;
    while (pool.length < slice.length) pool.push(makeRow());
    pool.forEach((el, i) => {
      const row = slice[i];
      if (!row) {
        if (el.isConnected) el.remove();
        return;
      }
      if (!el.isConnected) rowsEl.appendChild(el);
      paintRow(el, row);
    });
  }

  listEl.addEventListener("scroll", scheduleRender, { passive: true });
  new ResizeObserver(scheduleRender).observe(listEl);

  function makeRow() {
    const el = document.createElement("div");
    el.className = "row";
    el.innerHTML =
      '<span class="row-seq"></span>' +
      '<span class="row-main"><span class="row-name"></span>' +
      '<span class="row-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><span class="row-bar-fill"></span></span>' +
      '<span class="row-sub"><span class="host"></span><span class="detail"></span></span></span>' +
      '<span class="row-stats"><span class="row-size"></span><span class="row-speed"></span></span>' +
      '<span class="row-state"></span>' +
      '<button class="row-act" type="button" data-act="copy" title="Copy link" aria-label="Copy link"><svg viewBox="0 0 20 20"><rect x="7" y="7" width="9" height="9" rx="1.5"/><path d="M4 13V5.5A1.5 1.5 0 0 1 5.5 4H13"/></svg></button>';
    el._parts = {
      seq: $(".row-seq", el),
      name: $(".row-name", el),
      host: $(".host", el),
      detail: $(".detail", el),
      size: $(".row-size", el),
      speed: $(".row-speed", el),
      state: $(".row-state", el),
      bar: $(".row-bar", el),
      fill: $(".row-bar-fill", el),
    };
    return el;
  }

  function nameFor(row) {
    if (row.name) return row.name;
    try {
      const u = new URL(row.url);
      return decodeURIComponent(u.pathname.split("/").filter(Boolean).pop() || u.hostname);
    } catch {
      return row.url;
    }
  }

  // Progress colour: copper (0%) → brass (50%) → verdigris (100%).
  function barColor(p) {
    const stops = [[0, [200, 128, 78]], [0.5, [201, 168, 90]], [1, [95, 166, 151]]];
    const i = p <= 0.5 ? 0 : 1;
    const [p0, c0] = stops[i];
    const [p1, c1] = stops[i + 1];
    const t = (p - p0) / (p1 - p0);
    const mix = c0.map((v, k) => Math.round(v + (c1[k] - v) * t));
    return `rgb(${mix.join(",")})`;
  }

  function paintRow(el, row) {
    const p = el._parts;
    el.dataset.url = row.url;
    if (el.dataset.state !== row.state) el.dataset.state = row.state;
    const total = Number(row.total) || 0;
    const read = Number(row.read) || 0;
    const moving = row.state === "active" || row.state === "retrying";
    const fraction = total ? Math.min(1, read / total) : 0;
    el.toggleAttribute("data-unknown", moving && !total);

    // bar
    const width = moving || row.state === "cancelled" ? fraction : 0;
    const widthText = `${(width * 100).toFixed(1)}%`;
    if (p.fill.style.width !== widthText) p.fill.style.width = widthText;
    const color = moving ? barColor(fraction) : "";
    if (color) el.style.setProperty("--bar", color);
    else el.style.removeProperty("--bar");
    p.fill.style.setProperty("--bar-glow", moving ? color.replace("rgb(", "rgba(").replace(")", ",.35)") : "transparent");
    p.bar.setAttribute("aria-valuenow", String(Math.round((row.state === "done" ? 1 : fraction) * 100)));

    setText(p.seq, row.sequence ? String(row.sequence).padStart(3, "0") : "");
    setText(p.name, nameFor(row));
    el.title = row.url;
    setText(p.host, row.host || "");

    let detail = "";
    if (row.state === "failed" || row.state === "skipped" || row.state === "cancelled") detail = row.reason || "";
    else if (row.state === "active" && total && row.speed > 0) detail = `${fmt.duration((total - read) / row.speed)} left`;
    else if (row.state === "retrying") detail = "Retrying";
    setText(p.detail, detail);

    let size = "";
    if (moving) size = total ? `${fmt.bytes(read)} / ${fmt.bytes(total)}` : read ? fmt.bytes(read) : "Starting…";
    else if (row.state === "done") size = fmt.bytes(total || read);
    else if (row.state === "cancelled" && read) size = `${fmt.bytes(read)} / ${total ? fmt.bytes(total) : "?"}`;
    setText(p.size, size);
    setText(p.speed, moving && row.speed > 0 ? fmt.mbps(row.speed) : "");

    // All bytes are in, but the engine is still writing and checking the file.
    const saving = moving && total > 0 && read >= total;
    const showPct = moving && total > 0 && !saving;
    p.state.classList.toggle("pct", showPct);
    setText(p.state, saving ? "Saving" : showPct ? `${Math.floor(fraction * 100)}%` : STATE_LABEL[row.state] || "");
    if (saving) setText(p.speed, "");
  }

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  rowsEl.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-act]");
    if (!button) return;
    const url = button.closest(".row")?.dataset.url;
    if (url && button.dataset.act === "copy") {
      await navigator.clipboard.writeText(url).catch(() => {});
      log("info", "Link copied.");
    }
  });

  function updateCounts() {
    const c = { all: 0, active: 0, done: 0, skipped: 0, failed: 0 };
    for (const row of run.order) {
      if (view.host && row.host !== view.host) continue;
      c.all += 1;
      if (ACTIVE_STATES.has(row.state)) c.active += 1;
      else if (row.state === "cancelled") c.failed += 1;
      else if (c[row.state] !== undefined) c[row.state] += 1;
    }
    for (const [key, value] of Object.entries(c)) {
      const badge = $(`[data-count="${key}"]`);
      setText(badge, String(value));
      badge.toggleAttribute("data-zero", value === 0);
    }
    const unfinished = run.order.filter((r) => r.state === "failed" || r.state === "cancelled").length;
    $("#btn-retry-failed").hidden = run.running || unfinished === 0;
  }

  function updateTally() {
    let done = 0;
    for (const row of run.order) if (row.state === "done" || row.state === "skipped") done += 1;
    setText($("#t-done"), String(done));
    setText($("#t-total"), String(run.order.length));
    setText($("#t-bytes"), fmt.bytes(run.bytes));
    setText($("#t-speed"), run.running ? fmt.mbps(run.speed) : "–");
  }

  function tickClock() {
    if (!run.running) return;
    setText($("#t-time"), fmt.duration((Date.now() - run.startedAt) / 1000));
    setTimeout(tickClock, 1000);
  }

  function addHost(host) {
    view.hosts.set(host, true);
    const select = $("#host-filter");
    const option = document.createElement("option");
    option.value = host;
    option.textContent = host || "unknown";
    const after = $$("option", select).slice(1).find((o) => o.value.localeCompare(host) > 0);
    select.insertBefore(option, after || null);
  }

  $("#file-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-filter]");
    if (!tab) return;
    view.filter = tab.dataset.filter;
    $$("#file-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b === tab)));
    listEl.scrollTop = 0;
    view.dirty = true;
    scheduleRender();
  });
  $("#host-filter").addEventListener("change", (event) => {
    view.host = event.target.value;
    view.dirty = true;
    scheduleRender();
  });
  $("#btn-retry-failed").addEventListener("click", () => {
    const failed = run.order.filter((r) => (r.state === "failed" || r.state === "cancelled") && /^https?:/.test(r.url)).map((r) => r.url);
    if (!failed.length) return;
    urlsBox.value = failed.join("\n");
    autoGrow();
    urlsBox.focus();
    log("info", `Loaded ${failed.length} unfinished links. Press Start to try them again.`);
  });

  // ─── activity log ─────────────────────────────────────────────────────────
  const logEl = $("#log");
  const logEntries = [];
  let logFilter = "all";
  let logQueue = [];
  let logFrame = 0;

  function log(level, message) {
    if (!message) return;
    const text = String(message);
    const last = logEntries[logEntries.length - 1];
    if (last && last.text === text && Date.now() - last.t < 5000) {
      last.repeat += 1;
      if (last.node) {
        let badge = last.node.querySelector(".repeat");
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "repeat";
          last.node.querySelector(".msg").appendChild(badge);
        }
        badge.textContent = `×${last.repeat}`;
      }
      return;
    }
    const entry = { level: level === "warn" ? "warning" : level, text, t: Date.now(), repeat: 1, node: null };
    logEntries.push(entry);
    if (logEntries.length > LOG_KEEP) logEntries.splice(0, logEntries.length - LOG_KEEP);
    logQueue.push(entry);
    if (!logFrame) logFrame = requestAnimationFrame(flushLog);
  }

  function visibleIn(entry) {
    return logFilter === "all" || entry.level === "error" || entry.level === "warning";
  }

  function logNode(entry) {
    const li = document.createElement("li");
    li.dataset.level = entry.level;
    const time = document.createElement("time");
    time.textContent = new Date(entry.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
    const msg = document.createElement("span");
    msg.className = "msg";
    msg.textContent = entry.text;
    li.append(time, msg);
    entry.node = li;
    return li;
  }

  function flushLog() {
    logFrame = 0;
    const stick = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    const fragment = document.createDocumentFragment();
    for (const entry of logQueue) if (visibleIn(entry)) fragment.appendChild(logNode(entry));
    logQueue = [];
    logEl.appendChild(fragment);
    while (logEl.childElementCount > LOG_DOM_MAX) logEl.firstElementChild.remove();
    if (stick) logEl.scrollTop = logEl.scrollHeight;
  }

  function rebuildLog() {
    logEl.textContent = "";
    logQueue = logEntries.filter(visibleIn).slice(-LOG_DOM_MAX);
    flushLog();
  }

  $("#log-filter").addEventListener("click", (event) => {
    const button = event.target.closest("[data-level]");
    if (!button) return;
    logFilter = button.dataset.level;
    $$("#log-filter button").forEach((b) => b.setAttribute("aria-checked", String(b === button)));
    rebuildLog();
  });
  $("#btn-log-copy").addEventListener("click", async () => {
    const text = logEntries.map((e) => `${new Date(e.t).toISOString()} [${e.level}] ${e.text}${e.repeat > 1 ? ` (×${e.repeat})` : ""}`).join("\n");
    await navigator.clipboard.writeText(text).catch(() => {});
  });
  $("#btn-log-clear").addEventListener("click", () => {
    logEntries.length = 0;
    logEl.textContent = "";
  });

  // ─── run logs ─────────────────────────────────────────────────────────────
  $("#btn-log-open").addEventListener("click", async () => {
    const result = run.log ? await api.openLogFile(run.log) : await api.openLogs("run");
    if (result && !result.ok && result.error) log("warning", result.error);
  });
  $("#btn-log-folder").addEventListener("click", () => api.openLogs("run"));

  // ─── FlareSolverr status ──────────────────────────────────────────────────
  const FLARE_TEXT = { off: "Bot check: off", starting: "Bot check: starting", ready: "Bot check: ready", failed: "Bot check: failed", missing: "Bot check: not installed" };
  let flareState = "off";
  function showFlare(state, message, announce) {
    const pill = $("#flare-pill");
    pill.dataset.state = state;
    pill.textContent = FLARE_TEXT[state] || state;
    pill.title = message || "FlareSolverr solves bot checks such as DDoS-Guard";
    setStatus("#flare-status", state === "off" ? "" : `${FLARE_TEXT[state]}. ${message || ""}`.trim(), state === "ready" ? "ok" : state === "failed" ? "bad" : null);
    if (announce && state !== flareState) {
      if (state === "ready") log("ok", `FlareSolverr is ready. ${message || ""}`.trim());
      if (state === "failed") log("warning", `FlareSolverr failed. ${message || ""} Its output is in engine\\appdata\\logs\\flaresolverr.log.`);
    }
    flareState = state;
  }
  $("#flare").addEventListener("change", async (event) => {
    const status = await api.flareSet(event.target.checked);
    showFlare(status.state, status.message, false);
  });
  api.flareStatus().then((status) => showFlare(status.state, status.message, false)).catch(() => {});

  // ─── folder ───────────────────────────────────────────────────────────────
  function shortPath(dir) {
    const parts = String(dir).split(/[\\/]/).filter(Boolean);
    return parts.length > 3 ? `…${dir.includes("\\") ? "\\" : "/"}${parts.slice(-3).join(dir.includes("\\") ? "\\" : "/")}` : dir;
  }

  function showFolder(dir) {
    $("#folder-path").textContent = shortPath(dir);
    $("#btn-folder").title = `Open ${dir}`;
  }
  $("#btn-folder").addEventListener("click", async () => {
    const result = await api.openPath(run.root || downloadsDir);
    if (result && !result.ok && result.error) log("warning", result.error);
  });

  // ─── recent links (empty state) ───────────────────────────────────────────
  async function loadRecent() {
    const recent = await api.recentRuns(6).catch(() => []);
    const list = $("#recent-list");
    list.textContent = "";
    for (const item of recent) {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.title = "Add this link again";
      const url = document.createElement("span");
      url.className = "recent-url";
      url.textContent = item.url;
      const meta = document.createElement("span");
      meta.className = "recent-meta";
      const files = Number(item.saved || 0) + Number(item.skipped || 0);
      meta.textContent = `${fmt.ago(item.last_run)}${files ? `, ${files} ${files === 1 ? "file" : "files"}` : ""}`;
      button.append(url, meta);
      button.addEventListener("click", () => {
        addLinks(item.url, false);
        urlsBox.focus();
      });
      li.appendChild(button);
      list.appendChild(li);
    }
    $("#recent").hidden = !recent.length;
  }

  // ─── settings sheet ───────────────────────────────────────────────────────
  const sheet = $("#settings");
  let lastFocus = null;
  function openSheet(pane) {
    lastFocus = document.activeElement;
    if (pane) selectPane(pane);
    $("#sheet-scrim").hidden = false;
    sheet.inert = false;
    sheet.setAttribute("aria-hidden", "false");
    sheet.classList.add("open");
    $("#btn-settings-close").focus();
  }
  function closeSheet() {
    sheet.classList.remove("open");
    sheet.setAttribute("aria-hidden", "true");
    sheet.inert = true;
    $("#sheet-scrim").hidden = true;
    savePrefsSoon();
    lastFocus?.focus?.();
  }
  function selectPane(name) {
    $$("#sheet-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.pane === name)));
    $$(".sheet .pane").forEach((p) => { p.hidden = p.dataset.pane !== name; });
    if (name === "accounts") loadAccounts();
  }
  $("#btn-settings").addEventListener("click", () => openSheet());
  $("#btn-settings-close").addEventListener("click", closeSheet);
  $("#sheet-scrim").addEventListener("click", closeSheet);
  $("#sheet-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-pane]");
    if (tab) selectPane(tab.dataset.pane);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sheet.classList.contains("open")) closeSheet();
    if ((event.ctrlKey || event.metaKey) && event.key === ",") {
      event.preventDefault();
      openSheet();
    }
  });
  sheet.addEventListener("change", savePrefsSoon);
  $("#order-seg").addEventListener("click", (event) => {
    const button = event.target.closest("[data-order]");
    if (!button) return;
    setOrder(button.dataset.order);
    savePrefsSoon();
  });

  $("#btn-pick-dir").addEventListener("click", async () => {
    const result = await api.pickDownloadDir();
    if (result?.ok && result.path) {
      downloadsDir = result.path;
      $("#downloads-dir").value = downloadsDir;
      showFolder(downloadsDir);
      run.root = "";
    }
  });

  // cookie files used for every site
  function renderCookieFiles() {
    const list = $("#cookie-files");
    list.textContent = "";
    if (!cookieFiles.length) {
      const li = document.createElement("li");
      li.className = "empty-note";
      li.textContent = "No cookie files added.";
      list.appendChild(li);
      return;
    }
    cookieFiles.forEach((file, index) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = file;
      name.title = file;
      const remove = document.createElement("button");
      remove.className = "btn btn-quiet btn-small";
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        cookieFiles.splice(index, 1);
        api.saveGuiSetting({ cookie_files: cookieFiles });
        renderCookieFiles();
      });
      li.append(name, remove);
      list.appendChild(li);
    });
  }
  $("#btn-add-cookie").addEventListener("click", async () => {
    const picked = await api.pickCookiesFile();
    if (!picked?.ok || !picked.path || cookieFiles.includes(picked.path)) return;
    cookieFiles.push(picked.path);
    api.saveGuiSetting({ cookie_files: cookieFiles });
    renderCookieFiles();
  });

  // ─── sign-ins ─────────────────────────────────────────────────────────────
  const METHOD_LABEL = { none: "No sign-in", api_key: "API token", password: "Email and password", cookies: "Cookie file", chrome: "Import from Chrome", telegram: "Telegram account" };
  let accounts = [];

  function currentAccount() {
    return accounts.find((a) => a.id === $("#account-site").value) || null;
  }

  function setStatus(id, text, tone) {
    const el = $(id);
    el.textContent = text || "";
    if (tone) el.dataset.tone = tone;
    else delete el.dataset.tone;
  }

  async function loadAccounts() {
    const result = await api.authList().catch(() => null);
    accounts = result?.accounts || [];
    const select = $("#account-site");
    const keep = select.value;
    select.textContent = "";
    for (const account of accounts) {
      const option = document.createElement("option");
      option.value = account.id;
      option.textContent = account.configured ? `${account.name} (signed in)` : account.name;
      select.appendChild(option);
    }
    if (keep && accounts.some((a) => a.id === keep)) select.value = keep;
    if (result && !result.ok && result.error) setStatus("#account-status", result.error, "bad");
    fillMethods();
  }

  function fillMethods() {
    const account = currentAccount();
    const select = $("#account-method");
    select.textContent = "";
    if (!account) return;
    for (const method of account.methods) {
      const option = document.createElement("option");
      option.value = method;
      option.textContent = METHOD_LABEL[method] || method;
      select.appendChild(option);
    }
    if (account.configured && account.methods.includes(account.method)) select.value = account.method;
    else if (account.id === "telegram") select.value = "telegram";
    $("#account-note").textContent = account.note || "";
    const state = account.configured
      ? (account.label ? `Signed in as ${account.label}.` : `Signed in with ${METHOD_LABEL[account.method] || account.method}.`)
      : "Not signed in.";
    setStatus("#account-status", account.lastError ? `${state} ${account.lastError}` : state, account.lastError ? "bad" : null);
    showMethodFields();
  }

  function showMethodFields() {
    const account = currentAccount();
    const method = $("#account-method").value;
    $$(".pane[data-pane=accounts] [data-for]").forEach((el) => {
      const key = el.dataset.for;
      el.hidden = key === "cookies-host" ? !(method === "cookies" && account && !account.hosts?.length) : key !== method;
    });
    $("#btn-account-cookie").hidden = method !== "cookies";
    $("#btn-account-save").hidden = method === "cookies" || method === "chrome" || method === "telegram" || (method === "none" && !account?.configured);
    if (method !== "telegram") telegramReset(false);
    $("#btn-account-save").textContent = method === "none" ? "Save" : "Save sign-in";
    $("#btn-account-test").hidden = !account?.configured;
    $("#btn-account-remove").hidden = !account?.configured;
    if (method === "chrome") {
      setStatus("#account-status", "Chrome keeps cookies encrypted with Windows, so NEXDROP can't read them. Export a cookies.txt for this site from your browser and choose Cookie file instead.", null);
    }
  }

  // Telegram: Send code → code → (two-step password) → done. The session never reaches this window.
  let telegramStep = "start";

  function telegramReset(cancelHelper) {
    if (cancelHelper && telegramStep !== "start") api.telegramLogin({ action: "cancel" }).catch(() => {});
    telegramStep = "start";
    $("#tg-code-field").hidden = true;
    $("#tg-password-field").hidden = true;
    $("#tg-code").value = "";
    $("#tg-password").value = "";
    $("#btn-tg-send").hidden = false;
    $("#btn-tg-send").textContent = "Send code";
    $("#btn-tg-next").hidden = true;
    $("#btn-tg-cancel").hidden = true;
    for (const id of ["#tg-api-id", "#tg-api-hash", "#tg-phone"]) $(id).disabled = false;
    $$(".tg-signin input").forEach((el) => el.removeAttribute("aria-invalid"));
  }

  function telegramShow(reply) {
    $$(".tg-signin input").forEach((el) => el.removeAttribute("aria-invalid"));
    if (reply.step === "code" || reply.step === "password") {
      telegramStep = reply.step;
      for (const id of ["#tg-api-id", "#tg-api-hash", "#tg-phone"]) $(id).disabled = true;
      $("#tg-code-field").hidden = false;
      $("#tg-password-field").hidden = reply.step !== "password";
      $("#tg-code").disabled = reply.step === "password";
      $("#btn-tg-send").textContent = "Send a new code";
      $("#btn-tg-next").hidden = false;
      $("#btn-tg-cancel").hidden = false;
      (reply.step === "password" ? $("#tg-password") : $("#tg-code")).focus();
      setStatus("#account-status", reply.message, null);
      return;
    }
    if (reply.step === "done") {
      telegramReset(false);
      $("#tg-api-hash").value = "";
      $("#tg-phone").value = "";
      setStatus("#account-status", reply.message, "ok");
      loadAccounts();
      return;
    }
    if (reply.field === "api") telegramReset(false);
    const field = { api: ["#tg-api-id", "#tg-api-hash"], phone: ["#tg-phone"], code: ["#tg-code"], password: ["#tg-password"] }[reply.field] || [];
    field.forEach((id) => $(id).setAttribute("aria-invalid", "true"));
    setStatus("#account-status", reply.message || "Telegram sign-in failed.", "bad");
  }

  async function telegramAsk(body, busyText) {
    const buttons = ["#btn-tg-send", "#btn-tg-next"].map((id) => $(id));
    buttons.forEach((b) => { b.disabled = true; });
    setStatus("#account-status", busyText, null);
    try {
      telegramShow(await api.telegramLogin(body));
    } catch (error) {
      telegramShow({ step: "error", message: String(error?.message || error) });
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
  }

  $("#btn-tg-send").addEventListener("click", () => {
    $("#tg-code").disabled = false;
    telegramAsk({ action: "send_code", apiId: $("#tg-api-id").value, apiHash: $("#tg-api-hash").value, phone: $("#tg-phone").value },
      "Asking Telegram for a login code…");
  });
  $("#btn-tg-next").addEventListener("click", () => {
    if (telegramStep === "password") telegramAsk({ action: "password", password: $("#tg-password").value }, "Checking the password…");
    else telegramAsk({ action: "code", code: $("#tg-code").value }, "Signing in…");
    $("#tg-password").value = "";
  });
  $("#btn-tg-cancel").addEventListener("click", () => {
    telegramReset(true);
    setStatus("#account-status", "Sign-in cancelled.", null);
  });
  for (const id of ["#tg-code", "#tg-password"]) {
    $(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") $("#btn-tg-next").click();
    });
  }

  $("#account-site").addEventListener("change", fillMethods);
  $("#account-method").addEventListener("change", showMethodFields);
  $("#btn-account-save").addEventListener("click", async () => {
    const account = currentAccount();
    if (!account) return;
    const result = await api.authSave({
      id: account.id,
      method: $("#account-method").value,
      apiKey: $("#account-key").value,
      email: $("#account-email").value,
      password: $("#account-password").value,
    });
    $("#account-key").value = "";
    $("#account-password").value = "";
    await loadAccounts();
    setStatus("#account-status", result?.error || (result?.configured ? "Saved. It won't be shown again." : "Sign-in removed."), result?.error ? "bad" : "ok");
  });
  $("#btn-account-cookie").addEventListener("click", async () => {
    const account = currentAccount();
    if (!account) return;
    const picked = await api.pickCookiesFile();
    if (!picked?.ok || !picked.path) return;
    const result = await api.authSave({ id: account.id, method: "cookies", cookieFile: picked.path, host: $("#account-host").value });
    await loadAccounts();
    setStatus("#account-status", result?.message || result?.error || "Saved.", result?.ok ? "ok" : "bad");
  });
  $("#btn-account-test").addEventListener("click", async () => {
    const account = currentAccount();
    if (!account) return;
    setStatus("#account-status", "Checking…");
    const result = await api.authTest(account.id);
    const good = result?.state === "valid" || result?.state === "present";
    await loadAccounts();
    setStatus("#account-status", result?.message || result?.error || "No result.", good ? "ok" : result?.state === "unchecked" ? null : "bad");
  });
  $("#btn-account-remove").addEventListener("click", async () => {
    const account = currentAccount();
    if (!account) return;
    const result = await api.authRemove(account.id);
    await loadAccounts();
    setStatus("#account-status", result?.ok ? "Removed from this computer." : result?.error, result?.ok ? "ok" : "bad");
  });

  // ─── sites (source registry) ──────────────────────────────────────────────
  function showSourceReport(report) {
    if (!report || report.error || report.ok === false) {
      setStatus("#source-status", report?.error || "The check failed. Downloads are not affected.", "bad");
      return;
    }
    const parts = [`Engine ${report.engine || "10.10.0"} with ${report.handlers || 0} site handlers.`];
    if (report.latest) parts.push(`Newest published engine: ${report.latest}.`);
    if (report.checked_at) parts.push(`Checked ${fmt.ago(report.checked_at)}.`);
    parts.push(`${report.unsupported || 0} unsupported links logged.`);
    setStatus("#source-status", parts.join(" "), null);
    const box = $("#source-report");
    if (Array.isArray(report.candidates)) {
      box.hidden = false;
      box.textContent = report.candidates.length
        ? report.candidates.map((c) => `${c.priority}  ${c.source}  ×${c.count}`).join("\n")
        : "No unsupported links are waiting.";
    }
  }
  async function sourceAction(action, button) {
    button.disabled = true;
    const label = button.textContent;
    if (action === "check") button.textContent = "Checking…";
    try {
      showSourceReport(await api.sourceMaintenance(action));
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }
  $("#btn-source-check").addEventListener("click", (e) => sourceAction("check", e.currentTarget));
  $("#btn-source-backlog").addEventListener("click", (e) => sourceAction("backlog", e.currentTarget));
  $("#btn-source-clear").addEventListener("click", async (e) => {
    await sourceAction("clear", e.currentTarget);
    showSourceReport(await api.sourceMaintenance("status"));
  });
  $("#btn-source-open").addEventListener("click", () => api.openLogs());

  // ─── helpers ──────────────────────────────────────────────────────────────
  function flash(el) {
    el.animate?.([{ borderColor: "#cf5f49" }, { borderColor: "" }], { duration: 700 });
  }

  // ─── startup ──────────────────────────────────────────────────────────────
  api.getDefaults().then((defaults) => {
    downloadsDir = defaults.downloadsDir || "";
    $("#downloads-dir").value = downloadsDir;
    showFolder(downloadsDir);
    cookieFiles = Array.isArray(defaults.cookieFiles) ? defaults.cookieFiles : [];
    renderCookieFiles();
    prefs = defaults.prefs || {};
    // Settings from 2.1.0 and earlier used slow defaults (Page order, 2 downloads, 1 per site).
    // Move them once to the fast defaults; anything changed after this is kept.
    if (!prefs.version || prefs.version < 2) {
      if (!prefs.orderMode || prefs.orderMode === "source") prefs.orderMode = "parallel";
      if (!prefs.maxDownloads || prefs.maxDownloads <= 2) prefs.maxDownloads = 16;
      if (!prefs.perHost || prefs.perHost <= 1) prefs.perHost = 6;
      prefs.speedLimitMB = 0;
      prefs.version = 2;
      api.saveGuiSetting({ prefs });
    }
    for (const [key, spec] of Object.entries(PREF_FIELDS)) writeField(spec, prefs[key]);
    queue = Array.isArray(defaults.queue) ? defaults.queue : [];
    watches = Array.isArray(defaults.watches) ? defaults.watches : [];
    threadPosts = defaults.threadPosts || {};
    renderQueue();
    renderWatches();
    if (defaults.activeRun && Array.isArray(defaults.activeRun.urls)) {
      // NEXDROP was closed or paused in the middle of this run.
      showResume(defaults.activeRun);
    }
  });
  loadRecent();
  autoGrow();
  scheduleRender();
  urlsBox.focus();
})();
