/* NEXDROP media library (3.1). An extra view over downloaded files: gallery and list, search,
   filters, groups, preview, duplicates and storage. The download window is not changed by it. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const api = window.api;
  const fmt = window.fmt;

  const PAGE = 200;
  const CELL_W = 172;
  const CELL_H = 206;
  const LIST_H = 52;
  const KIND_ICON = { image: "IMG", video: "VID", audio: "AUD", archive: "ZIP", other: "FILE" };

  const lib = {
    open: false,
    tab: "files",
    viewMode: "grid",
    total: 0,
    pages: new Map(), // page index -> items
    loading: new Set(),
    seq: 0,
    working: false,
    current: null, // file in the preview
  };

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  function setStatus(sel, text, tone) {
    const node = $(sel);
    if (!node) return;
    node.textContent = text || "";
    if (tone) node.dataset.tone = tone;
    else delete node.dataset.tone;
  }

  function dims(item) {
    const parts = [];
    if (item.width && item.height) parts.push(`${item.width}×${item.height}`);
    if (item.duration && (item.kind === "video" || item.kind === "audio")) parts.push(fmt.duration(item.duration));
    if (item.kind === "audio" && item.bitrate) parts.push(`${Math.round(item.bitrate / 1000)} kb/s`);
    return parts.join(" · ");
  }

  function thumb(item, size) {
    if (item.thumb) {
      const img = el("img", "lib-thumb");
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = "";
      img.src = `nxlib://thumb/${encodeURIComponent(item.thumb)}`;
      if (size) img.width = size;
      img.addEventListener("error", () => img.replaceWith(el("span", `lib-thumb lib-icon k-${item.kind}`, KIND_ICON[item.kind] || "FILE")), { once: true });
      return img;
    }
    return el("span", `lib-thumb lib-icon k-${item.kind}`, (item.ext || KIND_ICON[item.kind] || "file").toUpperCase().slice(0, 4));
  }

  // ─── opening and indexing ─────────────────────────────────────────────────

  async function openLibrary() {
    lib.open = true;
    $("#library").hidden = false;
    document.body.classList.add("library-open");
    showTab(lib.tab);
    setStatus("#lib-index", "Looking for files…");
    const synced = await api.librarySync().catch(() => null);
    if (synced && !synced.ok) setStatus("#lib-status", synced.error, "bad");
    else if (synced && (synced.relinked || synced.missing)) {
      const bits = [];
      if (synced.relinked) bits.push(`${synced.relinked} moved or renamed file${synced.relinked === 1 ? "" : "s"} found again`);
      if (synced.missing) bits.push(`${synced.missing} missing`);
      setStatus("#lib-status", bits.join(", "));
    }
    await loadFacets();
    resetFiles();
    indexLoop();
  }

  function closeLibrary() {
    lib.open = false;
    $("#library").hidden = true;
    document.body.classList.remove("library-open");
  }

  // Metadata, fingerprints and thumbnails a few seconds at a time while the library is open.
  async function indexLoop() {
    if (lib.working) return;
    lib.working = true;
    let rounds = 0;
    try {
      while (lib.open) {
        const result = await api.libraryWork({ seconds: 12 }).catch(() => null);
        if (!result || !result.ok) {
          setStatus("#lib-index", result?.error ? "Indexing stopped." : "");
          break;
        }
        if (result.busy) {
          await new Promise((resolve) => setTimeout(resolve, 2000));
          continue;
        }
        rounds += 1;
        if (!result.ffmpeg) {
          setStatus("#lib-index", "ffmpeg was not found, so there are no thumbnails or similar-file checks.");
          break;
        }
        if (result.left > 0) setStatus("#lib-index", `Reading ${result.left.toLocaleString()} file${result.left === 1 ? "" : "s"}…`);
        else setStatus("#lib-index", "");
        if (result.done) refreshVisible();
        if (result.left <= 0) break;
      }
    } finally {
      lib.working = false;
      if (rounds) loadFacets();
    }
  }

  async function loadFacets() {
    const facets = await api.libraryFacets().catch(() => null);
    if (!facets?.ok) return;
    fillSelect("#lib-host", facets.hosts, "All sites");
    fillSelect("#lib-creator", facets.creators, "All creators");
    fillSelect("#lib-album", facets.albums, "All albums");
    const missingOption = $("#lib-missing option[value=only]");
    missingOption.textContent = facets.missing ? `Missing files (${facets.missing.toLocaleString()})` : "Missing files";
  }

  function fillSelect(sel, values, label) {
    const select = $(sel);
    const keep = select.value;
    select.textContent = "";
    select.append(new Option(label, ""));
    for (const value of values || []) select.append(new Option(value.length > 48 ? `${value.slice(0, 47)}…` : value, value));
    if (keep && !(values || []).includes(keep)) select.append(new Option(keep, keep));
    select.value = keep;
  }

  // ─── files: virtualized grid and list, loaded a page at a time ────────────

  const filters = { date: "", kind: "", folder: "" };

  function query() {
    return {
      text: $("#lib-text").value,
      kind: $("#lib-kind").value || filters.kind,
      host: $("#lib-host").value,
      creator: $("#lib-creator").value,
      album: $("#lib-album").value,
      sort: $("#lib-sort").value,
      missing: $("#lib-missing").value,
      date: filters.date,
      folder: filters.folder,
    };
  }

  async function resetFiles() {
    lib.seq += 1;
    lib.pages.clear();
    lib.loading.clear();
    $("#lib-scroll").scrollTop = 0;
    await loadPage(0);
    loadGroups();
    renderFiles();
  }

  async function loadPage(index) {
    if (lib.pages.has(index) || lib.loading.has(index)) return;
    lib.loading.add(index);
    const seq = lib.seq;
    const result = await api.libraryQuery({ ...query(), offset: index * PAGE, limit: PAGE }).catch(() => null);
    lib.loading.delete(index);
    if (seq !== lib.seq) return;
    if (!result?.ok) {
      setStatus("#lib-status", result?.error || "The library could not be read.", "bad");
      return;
    }
    lib.total = result.total;
    lib.pages.set(index, result.items);
    const count = `${result.total.toLocaleString()} file${result.total === 1 ? "" : "s"}`;
    const active = [filters.date && `saved ${filters.date}`, filters.folder && `in ${filters.folder}`, filters.kind && `type ${filters.kind}`].filter(Boolean);
    setStatus("#lib-status", active.length ? `${count} · ${active.join(" · ")}` : count);
  }

  async function refreshVisible() {
    // new thumbnails: reload the pages on screen without jumping
    const indexes = [...lib.pages.keys()];
    lib.seq += 1;
    lib.pages.clear();
    await Promise.all(indexes.map((index) => loadPage(index)));
    renderFiles();
  }

  function layout() {
    const scroller = $("#lib-scroll");
    const width = scroller.clientWidth - 8;
    if (lib.viewMode === "list") return { cols: 1, rowH: LIST_H, cellW: width };
    const cols = Math.max(1, Math.floor(width / CELL_W));
    return { cols, rowH: CELL_H, cellW: Math.floor(width / cols) };
  }

  function itemAt(index) {
    const page = lib.pages.get(Math.floor(index / PAGE));
    return page ? page[index % PAGE] : undefined;
  }

  function renderFiles() {
    const scroller = $("#lib-scroll");
    const box = $("#lib-items");
    const { cols, rowH, cellW } = layout();
    const rows = Math.ceil(lib.total / cols);
    $("#lib-spacer").style.height = `${rows * rowH}px`;
    const first = Math.max(0, Math.floor(scroller.scrollTop / rowH) - 2);
    const last = Math.min(rows, Math.ceil((scroller.scrollTop + scroller.clientHeight) / rowH) + 2);
    box.textContent = "";
    box.className = `lib-items view-${lib.viewMode}`;
    if (!lib.total) {
      const empty = el("p", "empty-note", $("#lib-text").value.trim() || query().host || query().creator ? "No files match." : "Files appear here after a run. Add other folders with Folders….");
      box.append(empty);
      return;
    }
    const missingPages = new Set();
    for (let row = first; row < last; row += 1) {
      for (let col = 0; col < cols; col += 1) {
        const index = row * cols + col;
        if (index >= lib.total) break;
        const item = itemAt(index);
        if (!item) {
          missingPages.add(Math.floor(index / PAGE));
          continue;
        }
        const node = lib.viewMode === "grid" ? gridCell(item) : listRow(item);
        node.style.transform = `translate(${col * cellW}px, ${row * rowH}px)`;
        node.style.width = `${cellW - (lib.viewMode === "grid" ? 8 : 0)}px`;
        box.append(node);
      }
    }
    for (const page of missingPages) loadPage(page).then(renderFiles);
  }

  function gridCell(item) {
    const cell = el("button", `lib-cell${item.missing ? " is-missing" : ""}`);
    cell.type = "button";
    cell.title = `${item.name}\n${item.path}`;
    const frame = el("span", "lib-frame");
    frame.append(thumb(item));
    if (item.kind === "video" && item.duration) frame.append(el("span", "lib-badge", fmt.duration(item.duration)));
    cell.append(frame, el("span", "lib-name", item.name), el("span", "lib-sub", [fmt.bytes(item.size), item.host].filter(Boolean).join(" · ")));
    cell.addEventListener("click", () => preview(item.id));
    return cell;
  }

  function listRow(item) {
    const row = el("button", `lib-row${item.missing ? " is-missing" : ""}`);
    row.type = "button";
    row.title = item.path;
    row.append(thumb(item, 40), el("span", "lib-name", item.name), el("span", "lib-col", dims(item) || item.ext.toUpperCase()),
      el("span", "lib-col", fmt.bytes(item.size)), el("span", "lib-col", item.host || "–"), el("span", "lib-col", item.creator || item.album || "–"),
      el("span", "lib-col", item.saved_at ? new Date(item.saved_at * 1000).toLocaleDateString() : "–"));
    row.addEventListener("click", () => preview(item.id));
    return row;
  }

  async function loadGroups() {
    const group = $("#lib-group").value;
    const box = $("#lib-groups");
    box.textContent = "";
    if (!group) {
      box.hidden = true;
      return;
    }
    const result = await api.libraryQuery({ ...query(), group, limit: 1 }).catch(() => null);
    box.hidden = false;
    for (const head of result?.groups || []) {
      const chip = el("button", "lib-group");
      chip.type = "button";
      const label = head.key || (group === "creator" ? "No creator" : group === "album" ? "No album" : "Unknown");
      chip.append(el("span", "lib-group-name", label), el("span", "lib-group-count", `${head.count.toLocaleString()} · ${fmt.bytes(head.bytes)}`));
      chip.addEventListener("click", () => {
        if (group === "creator") $("#lib-creator").value = head.key;
        else if (group === "album") $("#lib-album").value = head.key;
        else if (group === "host") $("#lib-host").value = head.key;
        else if (group === "kind") $("#lib-kind").value = head.key;
        else if (group === "date") filters.date = head.key;
        else if (group === "folder") filters.folder = head.key;
        $("#lib-group").value = "";
        resetFiles();
      });
      box.append(chip);
    }
    if (!box.children.length) box.append(el("span", "lib-sub", "No groups."));
  }

  // ─── preview ──────────────────────────────────────────────────────────────

  async function preview(id) {
    const page = [...lib.pages.values()].flat().find((item) => item.id === id);
    const dialog = $("#lib-preview");
    const item = page || null;
    lib.current = item;
    if (!item) return;
    $("#lib-preview-title").textContent = item.name;
    const media = $("#lib-preview-media");
    media.textContent = "";
    if (!item.missing && item.kind === "image") {
      const img = el("img", "lib-full");
      img.alt = item.name;
      img.decoding = "async";
      img.src = `nxlib://file/${item.id}`;
      media.append(img);
    } else if (!item.missing && item.kind === "video") {
      const video = el("video", "lib-full");
      video.controls = true;
      video.preload = "metadata";
      video.src = `nxlib://file/${item.id}`;
      media.append(video);
    } else if (!item.missing && item.kind === "audio") {
      const audio = el("audio", "lib-audio");
      audio.controls = true;
      audio.preload = "metadata";
      audio.src = `nxlib://file/${item.id}`;
      media.append(thumb(item), audio);
    } else {
      media.append(thumb(item));
    }
    const meta = $("#lib-preview-meta");
    meta.textContent = "";
    const add = (label, value, link) => {
      if (!value) return;
      const dd = el("dd");
      if (link && /^https?:\/\//i.test(value)) {
        const a = el("a", "", value);
        a.href = value;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        dd.append(a);
      } else dd.textContent = value;
      meta.append(el("dt", "", label), dd);
    };
    add("Size", fmt.bytes(item.size));
    add("Dimensions", item.width && item.height ? `${item.width} × ${item.height}` : "");
    add("Length", item.duration && item.kind !== "image" ? fmt.duration(item.duration) : "");
    add("Codec", item.codec);
    add("Bitrate", item.bitrate ? `${Math.round(item.bitrate / 1000)} kb/s` : "");
    add("Created", item.created);
    add("Saved", item.saved_at ? new Date(item.saved_at * 1000).toLocaleString() : "");
    add("Site", item.host);
    add("Creator", item.creator);
    add("Album", item.album);
    add("Title", item.title);
    add("File link", item.url, true);
    add("Pasted link", item.source, true);
    add("Path", item.path);
    if (item.missing) add("State", "Missing: the file is not where it was saved. Refresh looks for it again.");
    const archive = item.kind === "archive";
    $("#btn-prev-inspect").hidden = !archive || item.missing;
    $("#btn-prev-test").hidden = !archive || item.missing;
    $("#btn-prev-extract").hidden = !archive || item.missing;
    $("#btn-prev-open").hidden = archive || item.missing || item.kind === "other";
    $("#btn-prev-reveal").hidden = item.missing;
    $("#lib-archive").hidden = true;
    setStatus("#lib-preview-status", "");
    if (!dialog.open) dialog.showModal();
  }

  async function previewAction(action) {
    const item = lib.current;
    if (!item) return;
    if (action === "inspect" || action === "test") setStatus("#lib-preview-status", action === "test" ? "Checking every file…" : "Reading…");
    const result = await api.libraryAction({ id: item.id, action }).catch(() => null);
    if (result?.canceled) return setStatus("#lib-preview-status", "");
    if (!result?.ok) return setStatus("#lib-preview-status", result?.error || "That did not work.", "bad");
    if (action === "inspect") return showArchive(result);
    if (action === "test") {
      return setStatus("#lib-preview-status", result.good ? `All ${result.checked} file(s) read back correctly.` : result.error, result.good ? "ok" : "bad");
    }
    if (action === "extract") {
      const skipped = result.skipped ? ` ${result.skipped} unsafe or unsupported entr${result.skipped === 1 ? "y was" : "ies were"} left out.` : "";
      return setStatus("#lib-preview-status", `Extracted ${result.written} file(s) to ${result.dest}.${skipped}`, "ok");
    }
    if (result.revealed) setStatus("#lib-preview-status", "This kind of file is only shown in its folder, never started from NEXDROP.");
    else setStatus("#lib-preview-status", "");
  }

  function showArchive(report) {
    const box = $("#lib-archive");
    box.textContent = "";
    box.hidden = false;
    box.append(el("p", "lib-sub", `${report.kind.toUpperCase()} · ${report.files.toLocaleString()} file(s) · ${fmt.bytes(report.bytes)} unpacked`));
    for (const problem of report.problems) box.append(el("p", "lib-warn", problem));
    if (report.runnable.length) box.append(el("p", "lib-warn", `Contains programs or scripts (${report.runnable.slice(0, 3).join(", ")}). NEXDROP never runs them.`));
    const list = el("ul", "lib-archive-list");
    for (const entry of report.entries.slice(0, 200)) {
      const li = el("li", entry.safe ? "" : "lib-warn");
      li.append(el("span", "", entry.name), el("span", "lib-col", entry.dir ? "folder" : fmt.bytes(entry.size)));
      list.append(li);
    }
    box.append(list);
    if (report.more) box.append(el("p", "lib-sub", `…and ${report.more.toLocaleString()} more`));
  }

  // ─── duplicates ───────────────────────────────────────────────────────────

  let dupeKind = "exact";

  async function findDupes() {
    const list = $("#dupe-list");
    list.textContent = "";
    setStatus("#dupe-status", dupeKind === "exact" ? "Comparing sizes, then SHA-256 of files with the same size…" : "Comparing picture, video and sound fingerprints…");
    $("#btn-dupe-find").disabled = true;
    const result = await api.libraryGroups({ kind: dupeKind, text: $("#dupe-text").value, threshold: Number($("#dupe-threshold").value) }).catch(() => null);
    $("#btn-dupe-find").disabled = false;
    if (!result?.ok) return setStatus("#dupe-status", result?.error || "The library could not be read.", "bad");
    const wasted = result.groups.reduce((sum, group) => sum + group.wasted, 0);
    const parts = [`${result.total.toLocaleString()} group${result.total === 1 ? "" : "s"}`];
    if (dupeKind === "exact" && wasted) parts.push(`${fmt.bytes(wasted)} in extra copies`);
    if (result.partial) parts.push("some large files were not hashed yet, press Find again to continue");
    setStatus("#dupe-status", `${parts.join(" · ")}. Nothing is deleted unless you choose it.`);
    for (const group of result.groups) list.append(dupeGroup(group));
    if (!result.groups.length) list.append(el("p", "empty-note", dupeKind === "similar" && lib.working ? "No similar files yet. Fingerprints are still being read." : "No duplicates found."));
  }

  function dupeGroup(group) {
    const card = el("section", "dupe-group");
    const head = el("header", "dupe-head");
    head.append(el("strong", "", group.label));
    if (group.detail) head.append(el("span", "lib-sub", group.detail));
    if (group.crossSite) head.append(el("span", "pill", `across ${group.hosts.length} sites`));
    if (group.names) head.append(el("span", "pill", "different names"));
    if (group.wasted) head.append(el("span", "lib-sub", `${fmt.bytes(group.wasted)} extra`));
    card.append(head);
    group.files.forEach((file, index) => {
      const row = el("div", "dupe-file");
      const info = el("div", "dupe-info");
      info.append(el("span", "lib-name", file.name),
        el("span", "lib-sub", [index === 0 ? "first saved" : "", fmt.bytes(file.size), dims(file), file.host, file.saved_at ? new Date(file.saved_at * 1000).toLocaleDateString() : ""].filter(Boolean).join(" · ")),
        el("span", "lib-path", file.path));
      const actions = el("div", "dupe-actions");
      const button = (label, action, title) => {
        const b = el("button", "btn btn-quiet btn-small", label);
        b.type = "button";
        if (title) b.title = title;
        b.addEventListener("click", async () => {
          b.disabled = true;
          const result = await api.libraryAction({ id: file.id, action, reason: `${group.label}: the same as ${group.files.filter((f) => f.id !== file.id).map((f) => f.name).slice(0, 2).join(", ")}` }).catch(() => null);
          b.disabled = false;
          if (result?.canceled) return;
          if (!result?.ok) return setStatus("#dupe-status", result?.error || "That did not work.", "bad");
          if (action === "aside" || action === "trash") {
            row.classList.add("is-done");
            row.querySelector(".lib-sub").textContent = action === "trash" ? "Moved to the Recycle Bin" : `Set aside: ${result.path}`;
            actions.textContent = "";
          }
        });
        return b;
      };
      const look = el("button", "btn btn-quiet btn-small", "Preview");
      look.type = "button";
      look.addEventListener("click", () => {
        lib.pages.set(-1, group.files);
        preview(file.id);
      });
      actions.append(button("Show", "reveal"), look, button("Set aside", "aside", "Move this copy to the _duplicates folder. Nothing is deleted."),
        button("Recycle Bin…", "trash", "Asks first. You can restore it from the Recycle Bin."));
      row.append(thumb(file, 56), info, actions);
      card.append(row);
    });
    return card;
  }

  // ─── storage ──────────────────────────────────────────────────────────────

  async function loadStorage() {
    const body = $("#storage-body");
    body.textContent = "";
    setStatus("#storage-status", "Measuring…");
    const result = await api.libraryStorage().catch(() => null);
    if (!result?.ok) return setStatus("#storage-status", result?.error || "Storage could not be read.", "bad");
    setStatus("#storage-status", `${result.files.toLocaleString()} files · ${fmt.bytes(result.bytes)} in the library`);
    const disks = el("section", "storage-block");
    disks.append(el("h3", "", "Disks"));
    for (const disk of result.disks) {
      const used = disk.total ? (disk.total - disk.free) / disk.total : 0;
      const low = disk.free < 10 * 1024 ** 3 || disk.free / disk.total < 0.05;
      const row = el("div", `storage-disk${low ? " is-low" : ""}`);
      const bar = el("span", "storage-bar");
      const fill = el("span", "storage-fill");
      fill.style.width = `${Math.round(used * 100)}%`;
      bar.append(fill);
      row.append(el("span", "lib-name", disk.path), bar, el("span", "lib-sub", `${fmt.bytes(disk.free)} free of ${fmt.bytes(disk.total)}`));
      if (low) row.append(el("span", "lib-warn", "Low on space. New downloads may fail."));
      disks.append(row);
    }
    body.append(disks);
    const split = (title, rows, label) => {
      const block = el("section", "storage-block");
      block.append(el("h3", "", title));
      const max = Math.max(1, ...rows.map((row) => row.bytes));
      for (const row of rows) {
        const line = el("div", "storage-row");
        const bar = el("span", "storage-bar");
        const fill = el("span", "storage-fill");
        fill.style.width = `${Math.max(1, Math.round((row.bytes / max) * 100))}%`;
        bar.append(fill);
        line.append(el("span", "lib-name", label(row.key)), bar, el("span", "lib-sub", `${fmt.bytes(row.bytes)} · ${row.count.toLocaleString()}`));
        block.append(line);
      }
      if (!rows.length) block.append(el("p", "lib-sub", "Nothing yet."));
      body.append(block);
    };
    split("By site", result.byHost, (key) => key || "Other folders");
    split("By type", result.byKind, (key) => ({ image: "Images", video: "Videos", audio: "Audio", archive: "Archives", other: "Other" })[key] || key);
    split("By creator", result.byCreator, (key) => key || "No creator");
    split("Largest folders", result.byFolder, (key) => key);

    const tips = el("section", "storage-block");
    tips.append(el("h3", "", "Clean-up suggestions"));
    const tipButton = (label, fn) => {
      const b = el("button", "btn btn-quiet btn-small", label);
      b.type = "button";
      b.addEventListener("click", fn);
      return b;
    };
    const dupes = el("div", "storage-tip");
    dupes.append(el("span", "", "Find identical and similar files in the library."), tipButton("Open Duplicates", () => showTab("dupes")));
    tips.append(dupes);
    if (result.leftovers.length) {
      const total = result.leftovers.reduce((sum, item) => sum + item.bytes, 0);
      const tip = el("div", "storage-tip");
      tip.append(el("span", "", `${result.leftovers.length} unfinished download part(s) older than a week (${fmt.bytes(total)}). Resume their job, or remove them yourself if you no longer need them.`));
      const list = el("ul", "storage-list");
      for (const item of result.leftovers.slice(0, 30)) {
        const li = el("li");
        li.append(el("span", "lib-path", item.path), el("span", "lib-sub", fmt.bytes(item.bytes)));
        list.append(li);
      }
      tips.append(tip, list);
    }
    if (result.emptyFolders.length) {
      const tip = el("div", "storage-tip");
      tip.append(el("span", "", `${result.emptyFolders.length} empty folder(s).`));
      const list = el("ul", "storage-list");
      for (const dir of result.emptyFolders.slice(0, 30)) list.append(el("li", "lib-path", dir));
      tips.append(tip, list);
    }
    if (!result.leftovers.length && !result.emptyFolders.length) tips.append(el("p", "lib-sub", "No unfinished parts or empty folders."));
    body.append(tips);
    document.dispatchEvent(new CustomEvent("nexdrop-storage", { detail: result }));
  }

  // ─── statistics (3.1) ─────────────────────────────────────────────────────
  const stats = { data: null, span: "daily" };
  const SVG = "http://www.w3.org/2000/svg";
  const TYPE_LABEL = { image: "Images", video: "Videos", audio: "Audio", archive: "Archives", other: "Other" };

  function spanLabel(key, span, short) {
    if (span === "weekly") return short ? key.slice(5) : `Week ${key.slice(6)}, ${key.slice(0, 4)}`;
    if (span === "monthly") {
      const [year, month] = key.split("-").map(Number);
      return new Date(year, month - 1, 1).toLocaleDateString([], short ? { month: "short" } : { month: "long", year: "numeric" });
    }
    const [year, month, day] = key.split("-").map(Number);
    return new Date(year, month - 1, day).toLocaleDateString([], short ? { day: "numeric", month: "short" } : { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  }

  function svg(tag, attrs) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  }

  // One series of bars: files saved per day, week or month. Bars have a rounded top anchored to the
  // baseline, a 2px gap, a recessive grid, and a tooltip on hover over a hit area wider than the bar.
  function drawActivity() {
    const box = $("#stats-chart");
    box.textContent = "";
    const rows = (stats.data && stats.data[stats.span]) || [];
    const width = Math.max(320, box.clientWidth || 640);
    const height = 200;
    const pad = { top: 10, right: 8, bottom: 22, left: 36 };
    const max = Math.max(1, ...rows.map((r) => r.saved));
    const step = Math.pow(10, Math.floor(Math.log10(max)));
    const top = Math.ceil(max / step) * step;
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `Files saved, ${stats.span}` });
    for (const value of [0, top / 2, top]) {
      const y = pad.top + plotH - (value / top) * plotH;
      chart.append(svg("line", { class: "grid", x1: pad.left, x2: width - pad.right, y1: y, y2: y }));
      const label = svg("text", { class: "axis-text", x: pad.left - 6, y: y + 4, "text-anchor": "end" });
      label.textContent = Math.round(value).toLocaleString();
      chart.append(label);
    }
    const slot = plotW / Math.max(1, rows.length);
    const barW = Math.max(2, slot - 2);
    const tip = el("div", "stats-tip");
    tip.hidden = true;
    const every = Math.ceil(rows.length / 8);
    rows.forEach((row, index) => {
      const x = pad.left + index * slot + 1;
      const h = (row.saved / top) * plotH;
      const base = pad.top + plotH;
      const hit = svg("rect", { class: "hit", x: pad.left + index * slot, y: pad.top, width: slot, height: plotH });
      const r = Math.min(4, barW / 2, h);
      const bar = svg("path", { class: "bar", d: h <= 0 ? "" : `M${x},${base} V${base - h + r} Q${x},${base - h} ${x + r},${base - h} H${x + barW - r} Q${x + barW},${base - h} ${x + barW},${base - h + r} V${base} Z` });
      hit.addEventListener("mouseenter", () => {
        bar.classList.add("is-hover");
        tip.hidden = false;
        tip.textContent = "";
        tip.append(document.createTextNode(`${row.saved.toLocaleString()} file${row.saved === 1 ? "" : "s"} · ${fmt.bytes(row.bytes)}`), el("small", "", spanLabel(row.key, stats.span, false)));
        // kept inside the chart: near an edge the tip leans inwards
        const share = (x + barW / 2) / width;
        tip.style.left = `${share * 100}%`;
        tip.style.transform = `translate(${share > 0.85 ? "-100%" : share < 0.15 ? "0" : "-50%"}, calc(-100% - 8px))`;
        tip.style.top = `${((base - h) / height) * 100}%`;
      });
      hit.addEventListener("mouseleave", () => { bar.classList.remove("is-hover"); tip.hidden = true; });
      chart.append(hit, bar);
      const last = index === rows.length - 1;
      if (last || (index % every === 0 && rows.length - 1 - index >= every / 2)) { // no label crowding the last one
        const label = svg("text", { class: "axis-text", x: x + barW / 2, y: height - 6, "text-anchor": "middle" });
        label.textContent = spanLabel(row.key, stats.span, true);
        chart.append(label);
      }
    });
    box.append(chart, tip);
    const table = $("#stats-activity-table");
    table.textContent = "";
    const head = el("tr");
    head.append(el("th", "", stats.span === "daily" ? "Day" : stats.span === "weekly" ? "Week" : "Month"), el("th", "", "Files saved"), el("th", "", "Data"));
    table.append(head);
    for (const row of [...rows].reverse()) {
      const tr = el("tr");
      tr.append(el("td", "", spanLabel(row.key, stats.span, false)), el("td", "", row.saved.toLocaleString()), el("td", "", fmt.bytes(row.bytes)));
      table.append(tr);
    }
  }

  function barList(target, rows, label, value, sub) {
    const box = $(target);
    box.textContent = "";
    const max = Math.max(1, ...rows.map(value));
    for (const row of rows) {
      const line = el("div", "storage-row");
      const bar = el("span", "storage-bar");
      const fill = el("span", "storage-fill");
      fill.style.width = `${Math.max(1, Math.round((value(row) / max) * 100))}%`;
      bar.append(fill);
      line.append(el("span", "lib-name", label(row)), bar, el("span", "lib-sub", sub(row)));
      box.append(line);
    }
    if (!rows.length) box.append(el("p", "lib-sub", "Nothing in this period."));
  }

  async function loadStats() {
    setStatus("#stats-status", "Counting…");
    const result = await api.statsSummary({ days: Number($("#stats-days").value) }).catch(() => null);
    if (!result?.ok) return setStatus("#stats-status", result?.error || "Statistics could not be read.", "bad");
    setStatus("#stats-status", "");
    stats.data = result;
    const t = result.totals;
    const hours = result.seconds / 3600;
    const tiles = [
      [t.saved.toLocaleString(), "Files saved"],
      [fmt.bytes(t.bytes), "Data downloaded"],
      [t.failed.toLocaleString(), "Failed"],
      [t.skipped.toLocaleString(), "Skipped (already saved or filtered)"],
      [result.duplicates.toLocaleString(), "Same content as an earlier file"],
      [result.averageSpeed ? fmt.speed(result.averageSpeed) : "–", "Average speed"],
      [result.peakSpeed ? fmt.speed(result.peakSpeed) : "–", "Peak speed"],
      [result.seconds ? (hours >= 1 ? `${hours.toFixed(1)} h` : result.seconds >= 60 ? `${Math.round(result.seconds / 60)} min` : `${Math.round(result.seconds)} s`) : "–", `Downloading, over ${result.runs.toLocaleString()} run${result.runs === 1 ? "" : "s"}`],
    ];
    const box = $("#stats-tiles");
    box.textContent = "";
    for (const [value, label] of tiles) {
      const tile = el("div", "stat-tile");
      tile.append(el("span", "stat-value", value), el("span", "stat-label", label));
      box.append(tile);
    }
    drawActivity();
    barList("#stats-sites", result.sites, (r) => r.site, (r) => r.saved,
      (r) => `${r.saved.toLocaleString()} saved${r.failed ? ` · ${r.failed.toLocaleString()} failed` : ""} · ${fmt.bytes(r.bytes)}`);
    barList("#stats-types", result.types, (r) => TYPE_LABEL[r.type] || r.type, (r) => r.bytes,
      (r) => `${fmt.bytes(r.bytes)} · ${r.saved.toLocaleString()}`);
  }

  $("#stats-days").addEventListener("change", loadStats);
  for (const button of $$("#stats-span [role=radio]")) {
    button.addEventListener("click", () => {
      stats.span = button.dataset.span;
      for (const other of $$("#stats-span [role=radio]")) other.setAttribute("aria-checked", String(other === button));
      drawActivity();
    });
  }
  $("#btn-stats-export").addEventListener("click", async () => {
    const result = await api.statsExport({ days: Number($("#stats-days").value) });
    if (result?.canceled) return;
    setStatus("#stats-status", result?.ok ? `Saved ${result.path}` : result?.error || "The file could not be written.", result?.ok ? "ok" : "bad");
  });
  window.addEventListener("resize", () => lib.open && lib.tab === "stats" && stats.data && drawActivity());

  // ─── organize and rename ──────────────────────────────────────────────────

  let orgPlanId = "";

  async function loadOrganize() {
    const info = await api.libraryFolders().catch(() => null);
    const select = $("#org-root");
    const keep = select.value;
    select.textContent = "";
    for (const dir of info?.roots || []) select.append(new Option(dir, dir));
    if (keep) select.value = keep;
    loadBatches();
  }

  function relative(target, root) {
    return root && target.toLowerCase().startsWith(root.toLowerCase()) ? target.slice(root.length).replace(/^[\\/]/, "") || "." : target;
  }

  async function previewOrganize() {
    const preset = $("#org-preset").value;
    const folders = preset === "custom" ? $("#org-folders").value.trim() : preset;
    const names = $("#org-names").value.trim();
    const root = $("#org-root").value;
    const filters = $("#org-scope").value === "filters" ? query() : {};
    setStatus("#org-status", "Working out where each file goes…");
    $("#btn-org-apply").disabled = true;
    const plan = await api.organizePlan({ filters, root, folders, names }).catch(() => null);
    const rows = $("#org-rows");
    rows.textContent = "";
    if (!plan?.ok) {
      $("#org-plan").hidden = true;
      return setStatus("#org-status", plan?.error || "The preview did not work.", "bad");
    }
    orgPlanId = plan.id;
    $("#org-plan").hidden = !plan.count;
    for (const move of plan.moves) {
      const tr = document.createElement("tr");
      if (move.conflict) tr.className = "dupe";
      const name = move.from.split(/[\\/]/).pop();
      for (const text of [name, relative(move.from, root), relative(move.to, root), move.conflict || ""]) {
        const td = el("td", "", text);
        td.title = text;
        tr.append(td);
      }
      rows.append(tr);
    }
    const ready = plan.count - plan.conflicts;
    const bits = [`${ready.toLocaleString()} file${ready === 1 ? "" : "s"} to move`];
    if (plan.conflicts) bits.push(`${plan.conflicts} conflict${plan.conflicts === 1 ? "" : "s"} stay where they are`);
    if (plan.count > plan.moves.length) bits.push(`the first ${plan.moves.length} shown`);
    if (plan.truncated) bits.push("only the first 20,000 files are planned at once");
    setStatus("#org-status", plan.count ? bits.join(" · ") : "Every file is already where this pattern puts it.");
    $("#btn-org-apply").disabled = !ready;
  }

  async function applyOrganize() {
    $("#btn-org-apply").disabled = true;
    setStatus("#org-status", "Moving…");
    const result = await api.organizeApply(orgPlanId).catch(() => null);
    if (result?.canceled) {
      $("#btn-org-apply").disabled = false;
      return setStatus("#org-status", "");
    }
    if (!result?.ok) return setStatus("#org-status", result?.error || "Nothing was moved.", "bad");
    $("#org-plan").hidden = true;
    const extra = result.skipped ? ` ${result.skipped} left where they were${result.errors[0] ? ` (${result.errors[0]})` : ""}.` : "";
    setStatus("#org-status", `Moved ${result.moved.toLocaleString()} file${result.moved === 1 ? "" : "s"}.${extra}`, "ok");
    loadBatches();
    lib.pages.clear();
  }

  async function loadBatches() {
    const list = $("#org-batches");
    list.textContent = "";
    const result = await api.organizeBatches().catch(() => null);
    for (const batch of (result?.batches || []).filter((b) => b.count).slice(0, 5)) {
      const li = el("li");
      li.append(el("span", "lib-sub", `${new Date(batch.start * 1000).toLocaleString()} · ${batch.count} file${batch.count === 1 ? "" : "s"} moved${batch.undone ? " · undone" : ""}`));
      if (!batch.undone) {
        const undo = el("button", "btn btn-quiet btn-small", "Undo");
        undo.type = "button";
        undo.addEventListener("click", async () => {
          const result = await api.organizeUndo(batch.batch).catch(() => null);
          if (result?.canceled) return;
          if (!result?.ok) return setStatus("#org-status", result?.error || "Undo did not work.", "bad");
          setStatus("#org-status", `Moved ${result.restored} file(s) back.${result.problems.length ? ` ${result.problems.length} could not be moved back: ${result.problems[0]}` : ""}`, result.problems.length ? "bad" : "ok");
          loadBatches();
          lib.pages.clear();
        });
        li.append(undo);
      }
      list.append(li);
    }
  }

  $("#org-preset").addEventListener("change", () => {
    $("#org-folders-box").hidden = $("#org-preset").value !== "custom";
    $("#btn-org-apply").disabled = true;
  });
  for (const sel of ["#org-folders", "#org-names", "#org-root", "#org-scope"]) $(sel).addEventListener("input", () => { $("#btn-org-apply").disabled = true; });
  $("#btn-org-preview").addEventListener("click", previewOrganize);
  $("#btn-org-apply").addEventListener("click", applyOrganize);

  // ─── folders ──────────────────────────────────────────────────────────────

  async function manageFolders() {
    const info = await api.libraryFolders().catch(() => null);
    if (!info?.ok) return;
    const dialog = document.createElement("dialog");
    dialog.className = "dialog";
    const form = el("form");
    form.method = "dialog";
    form.append(el("h2", "", "Library folders"), el("p", "dialog-text", "The download folders are always included. Add other folders to find files from anywhere on this PC."));
    const list = el("ul", "storage-list");
    const draw = (extra) => {
      list.textContent = "";
      for (const dir of info.roots) {
        const li = el("li");
        li.append(el("span", "lib-path", dir));
        if (extra.includes(dir)) {
          const remove = el("button", "btn btn-quiet btn-small", "Remove");
          remove.type = "button";
          remove.addEventListener("click", async () => {
            const result = await api.libraryRemoveFolder(dir);
            info.roots = info.roots.filter((d) => d !== dir);
            draw(result.extra || []);
          });
          li.append(remove);
        } else li.append(el("span", "lib-sub", "download folder"));
        list.append(li);
      }
    };
    draw(info.extra);
    const actions = el("div", "dialog-actions");
    const add = el("button", "btn btn-quiet", "Add folder…");
    add.type = "button";
    add.addEventListener("click", async () => {
      const result = await api.libraryAddFolder();
      if (result?.ok) {
        const fresh = await api.libraryFolders();
        info.roots = fresh.roots;
        draw(fresh.extra);
      }
    });
    const done = el("button", "btn btn-primary", "Done");
    done.value = "close";
    actions.append(add, done);
    form.append(list, actions);
    dialog.append(form);
    document.body.append(dialog);
    dialog.addEventListener("close", async () => {
      dialog.remove();
      await api.librarySync();
      await loadFacets();
      resetFiles();
      indexLoop();
    });
    dialog.showModal();
  }

  // ─── duplicates after a run ("Ask" and "Replace after confirmation") ──────

  function showRunDuplicates(event) {
    const items = event.items || [];
    if (!items.length) return;
    const dialog = $("#dupes-dialog");
    const replace = event.action === "replace";
    $("#dupes-text").textContent = `${items.length} new file${items.length === 1 ? " has" : "s have"} the same content as a file you already had. Both copies are kept until you choose.`;
    const list = $("#dupes-pairs");
    list.textContent = "";
    const setAside = [];
    for (const pair of items) {
      const li = el("li", "dupe-file");
      const info = el("div", "dupe-info");
      info.append(el("span", "lib-name", pair.path.split(/[\\/]/).pop()), el("span", "lib-path", `new: ${pair.path}`), el("span", "lib-path", `already had: ${pair.of}`));
      const actions = el("div", "dupe-actions");
      const state = el("span", "lib-sub");
      const finish = (text) => {
        actions.textContent = "";
        state.textContent = text;
        actions.append(state);
      };
      const run = async (target, action) => {
        const result = await api.duplicateAction({ path: target, action, reason: "It has the same content as the other copy." }).catch(() => null);
        if (result?.canceled || action === "reveal") return;
        if (!result?.ok) return (state.textContent = result?.error || "That did not work.");
        finish(action === "trash" ? "Moved to the Recycle Bin" : action === "aside" ? "New copy set aside" : "New copy renamed");
      };
      const act = (label, title, handler) => {
        const b = el("button", "btn btn-quiet btn-small", label);
        b.type = "button";
        if (title) b.title = title;
        b.addEventListener("click", handler);
        return b;
      };
      actions.append(act("Show", "", () => run(pair.path, "reveal")), act("Keep both", "", () => finish("Both kept")),
        act("Rename new", "Adds (duplicate) to the new copy's name", () => run(pair.path, "rename")),
        act("Set new aside", "Moves the new copy to the _duplicates folder", () => run(pair.path, "aside")),
        act("New to Recycle Bin…", "Asks first", () => run(pair.path, "trash")));
      setAside.push(() => (actions.contains(state) ? null : run(pair.path, "aside")));
      if (replace) actions.append(act("Older to Recycle Bin…", "Keeps the new copy. Asks first.", () => run(pair.of, "trash")));
      li.append(info, actions);
      list.append(li);
    }
    $("#btn-dupes-aside-all").onclick = async () => {
      for (const step of setAside) await step();
    };
    if (!dialog.open) dialog.showModal();
  }

  api.onPipelineEvent((event) => {
    if (event?.type === "duplicates") showRunDuplicates(event);
    else if (event?.type === "job-event" && event.event?.type === "duplicates") showRunDuplicates(event.event);
  });

  // ─── wiring ───────────────────────────────────────────────────────────────

  function showTab(name) {
    lib.tab = name;
    for (const tab of $$("#library-tabs [role=tab]")) tab.setAttribute("aria-selected", String(tab.dataset.lib === name));
    for (const pane of $$("#library .lib-pane")) pane.hidden = pane.dataset.lib !== name;
    if (name === "storage") {
      loadOrganize();
      loadStorage();
    }
    if (name === "files") renderFiles();
    if (name === "stats") loadStats();
  }

  let textTimer = 0;
  $("#btn-library").addEventListener("click", openLibrary);
  $("#btn-library-close").addEventListener("click", closeLibrary);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && lib.open && !document.querySelector("dialog[open]")) closeLibrary();
  });
  for (const tab of $$("#library-tabs [role=tab]")) tab.addEventListener("click", () => showTab(tab.dataset.lib));
  $("#lib-text").addEventListener("input", () => {
    clearTimeout(textTimer);
    textTimer = setTimeout(resetFiles, 250);
  });
  for (const sel of ["#lib-kind", "#lib-host", "#lib-creator", "#lib-album", "#lib-sort", "#lib-missing"]) {
    $(sel).addEventListener("change", () => {
      if (sel === "#lib-kind") filters.kind = "";
      resetFiles();
    });
  }
  $("#lib-group").addEventListener("change", () => {
    filters.date = "";
    filters.folder = "";
    loadGroups();
  });
  for (const button of $$("#lib-view [role=radio]")) {
    button.addEventListener("click", () => {
      lib.viewMode = button.dataset.view;
      for (const other of $$("#lib-view [role=radio]")) other.setAttribute("aria-checked", String(other === button));
      $("#lib-scroll").scrollTop = 0;
      renderFiles();
    });
  }
  let scrollFrame = 0;
  $("#lib-scroll").addEventListener("scroll", () => {
    cancelAnimationFrame(scrollFrame);
    scrollFrame = requestAnimationFrame(renderFiles);
  }, { passive: true });
  window.addEventListener("resize", () => lib.open && lib.tab === "files" && renderFiles());
  $("#btn-lib-refresh").addEventListener("click", async () => {
    filters.date = "";
    filters.folder = "";
    setStatus("#lib-index", "Looking for files…");
    const synced = await api.librarySync().catch(() => null);
    setStatus("#lib-index", synced?.ok ? `${synced.added + synced.history} new · ${synced.relinked} found again · ${synced.missing} missing` : synced?.error || "");
    await loadFacets();
    if (lib.tab === "files") resetFiles();
    if (lib.tab === "storage") loadStorage();
    indexLoop();
  });
  $("#btn-lib-folders").addEventListener("click", manageFolders);
  for (const button of $$("#dupe-kind [role=radio]")) {
    button.addEventListener("click", () => {
      dupeKind = button.dataset.kind;
      for (const other of $$("#dupe-kind [role=radio]")) other.setAttribute("aria-checked", String(other === button));
      $("#dupe-threshold-box").hidden = dupeKind !== "similar";
    });
  }
  $("#dupe-threshold").addEventListener("input", () => { $("#dupe-threshold-text").textContent = $("#dupe-threshold").value; });
  $("#btn-dupe-find").addEventListener("click", findDupes);
  $("#dupe-text").addEventListener("keydown", (event) => { if (event.key === "Enter") findDupes(); });
  $("#btn-prev-open").addEventListener("click", () => previewAction("open"));
  $("#btn-prev-reveal").addEventListener("click", () => previewAction("reveal"));
  $("#btn-prev-inspect").addEventListener("click", () => previewAction("inspect"));
  $("#btn-prev-test").addEventListener("click", () => previewAction("test"));
  $("#btn-prev-extract").addEventListener("click", () => previewAction("extract"));
  $("#lib-preview").addEventListener("close", () => {
    const media = $("#lib-preview-media");
    for (const node of $$("video, audio", media)) node.pause();
    media.textContent = ""; // release the file
    lib.pages.delete(-1);
  });

  window.nexdropLibrary = { open: openLibrary, showTab };
})();
