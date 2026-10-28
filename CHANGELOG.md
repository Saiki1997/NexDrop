# NEXDROP 3.1.4

## Download order
- **File numbers follow the post's layout, also for images hosted on other sites.** A post is read left to right, then down. Images uploaded to the forum were already numbered in that order, but images on other sites (jpg5, imgbox, pixhost, Cyberdrop…) got their number when that site answered, so `-001`, `-002`… could come out shuffled. Each link now gets its number when the post lists it, in **Page order** and **Parallel**. A link that turns out to hold several files (an album) gives its first file that number; the rest are numbered after the others. A link that fails leaves a gap in the numbers. **Fastest** is unchanged.
- **Page order downloads one post at a time.** It now finishes every file of a post, links on other sites included, before it starts the next post, through to the last post. A thread linked from a post is finished before the next post too.

# NEXDROP 3.1.3

## Folders and names
- **Folders for untitled posts are named like their files.** In 3.1.2, a post with no usable title got a folder called `Post 114048`. Now the folder takes the name its files got. Files saved as `Lala kudo-001.jpg`, `Lala kudo-002.jpg`… go into a `Lala kudo` folder, even when the thread folder has the same name. Another untitled post in the same thread gets `Lala kudo (2)`, and running the job again uses the same folders. `Post <number>` is used only when the files have no name in common (for example, files that keep their camera names in a thread with no usable title).

Folders already on disk are not renamed. **Library → Organize and rename** can move files, with a preview and undo.

# NEXDROP 3.1.2

## Fixes
- **No more `.part.nxmeta` files left beside finished downloads.** NEXDROP writes `name.part.nxmeta` beside a file while it downloads (`name.part`). It records the link, the server's ETag, Last-Modified and size, so a paused or broken download only resumes when the server still sends the same file. It is removed when the file finishes. When a job ran links again whose files were already saved, the note was written before NEXDROP noticed the file was already there, so it stayed behind. It is now written only once a download really starts. When the same links run again, the notes 3.1.1 left beside those files are removed. A note beside an unfinished `.part` file is kept, because that download can still resume. Leftover notes are safe to delete yourself.
- **Every file of a post goes into a folder, also when the post has no usable title.** A post with no text, or only text like "Discussion Thread", "thanks!" or a link, left its files loose in the thread's folder. They now go into a folder named `Post <number>` (the post's number on the forum), inside the thread's folder. This follows **Folder for each post or album**.
- **Cleaner titles from posts.** Download prompts, `@@` markers, file extensions and sizes are taken out of a post's title. `Download @@PIYO-116 Title.mp4 (1.2 GB)` now names the folder and files `PIYO-116 Title`.
- Titles read from inside a file ("Read titles inside files") have their dates removed too, like every other name.

Files already on disk are not moved or renamed. **Library → Organize and rename** can do that, with a preview and undo.

# NEXDROP 3.1.1

## Folders and names
- **Folder for each post or album** (Settings → Downloads, on by default). Every file goes into a folder named after the forum post or album it came from. Before, only files renamed after the post's title got that folder, so files named by the host, or keeping their own name, were left loose in the thread's folder. Files whose folder already has that title don't get a second one.
- **Take dates out of names** (Settings → Downloads, on by default). Whole dates are removed from file and folder names: `2021-06-10-PIYO-118-Title.mp4` is saved as `PIYO-118-Title-001.mp4`, `2020 06 11 [EMOI-014] Shy` as `[EMOI-014] Shy`, and `(2022-07-12) - (1080p)` as `(1080p)`. Only full dates (year, month and day, or day, month and year) are removed; ids, resolutions and other numbers stay. A name that is nothing but a date is kept. Direct file links are named the same way.
- Both switches follow **Smart filename detection**. Turn either off to get the 3.1.0 behaviour back.
- A job you started with 3.1.0 or earlier gets the new names for files it hasn't saved yet. Files already on disk are not renamed; **Library → Organize and rename** can do that, with a preview and undo.

# NEXDROP 3.1.0

## Faster and more at once
- **Several jobs at once** (Settings → Speed → Jobs at once, up to 4). Queued jobs start beside the running one and share the download limit and speed cap equally. Two jobs from the same site never run together. Each job has its own sign-in cookies and its own log, and History is locked so two jobs never write it at the same moment.
- **Large files in parts.** A file of 64 MB or more downloads over up to 4 connections (Settings → Speed → Parts per large file), but only when the server says it supports this safely: it accepts ranges and sends a strong ETag or Last-Modified. Every part is checked for exactly its range and size. If anything looks wrong, the parts are thrown away and the file is downloaded again in one stream. Each part resumes on its own after a pause.
- **Per-site limits** (for example `bunkr.cr = 2`), the longest wait between attempts, and how long a server's "retry after" is honoured.
- **Big link lists run in parts.** One run reads at most 200 links. Before, pasting more than 200 links quietly used only the first 200. Now a bigger job runs in parts of 200, one after another, and the other parts wait at the top of the queue ("part 2 of 5").

## Know what you are downloading
- **Check** (beside Start) shows, for each link, the site, what it is (album, profile, thread, single file, playlist…), whether it needs a sign-in, and whether it's a duplicate of another link in the box. **Preview** opens each link once, without downloading, to read its title, author and the number of files.
- **Naming preview** finds every file in a job and shows the name it will get, without downloading anything. You can try other templates and strategies there and save the ones you like.
- **Test a site** (Settings → Speed) looks up the site's address, connects (through your proxy when one is set) and sends one small HTTPS request, and shows how long each step took and where it failed.

## Download rules (Settings → Downloads)
- Rules decide per file: **skip**, **only download** matching files, **save into a folder**, or **one at a time** for that site. A rule can match the site, file type, extension, file name, size, album, creator or link.
- **Preview** shows what the rules would have done to the files already in your Library, before you use them.
- Rules also apply to direct file links (links no site handler knows).

## Watch, schedule and retry
- **Watch any link**, not only forum threads: albums, profiles, channels and playlists. Each watch has its own interval (30 minutes to a week) and can be paused. Only new files are downloaded.
- **Download hours on chosen days** (for example weekends only), with an option to **pause when the hours end**. A window that crosses midnight belongs to the day it starts.
- **Start later / pause at:** queue a job to start at a time you choose, and optionally pause it at another.
- **Retry later:** when files still fail for a reason that usually passes (busy server, rate limit, time-out, dropped connection, used-up quota), the job goes back in the queue with a start time. Up to 3 tries, each waiting longer; files already saved are skipped.

## When a job finishes (Settings → Downloads)
- Show a notification, open the download folder, **organize** the new files into folders (`{creator}/{album}`, `{site}/{type}`…), **verify** them, add them to the Library with thumbnails, and **save a manifest**.
- **Verify** checks each saved file is still there, has the size the server sent, and is not a web page saved under a picture or video name. The sites don't publish checksums, so this can't compare against one.
- **Manifest:** one `.nexdrop.json` file per job beside the downloads, with every file's link, name, site, size and SHA-256, so a copy can be checked later. It never holds sign-ins, cookies or the query part of links, and it never overwrites another file.
- **Broken-link report** (Settings → Sites): failed files of the last days grouped by site and reason, as a Markdown file for a GitHub issue. Links are left out unless you ask, and then ids and secrets are removed.

## Library (new window)
- Everything you downloaded in one place: **gallery and list**, search, filters by site, type, creator and album, groups, and a preview.
- Finds files that were **moved, renamed or deleted** outside NEXDROP.
- **Duplicates:** identical content under any name (same size, then same SHA-256), and **similar** pictures, videos and recordings (the same picture resized or re-encoded, the same video in another quality). Fingerprints are made with the bundled ffmpeg.
- **Storage:** space used per folder, site and type, and free space.
- **Organize and rename** with a preview first. Files whose target already exists are marked as conflicts and skipped. Every move is journaled, so a batch can be **undone**, even after a crash.
- **Archives:** list a ZIP or TAR, check every file's checksum, or extract into a new folder you choose. Nothing inside is run; paths that leave the folder, links and archive bombs are refused, and nothing is overwritten. 7z and RAR are reported as not supported.
- **Statistics:** files saved, failed and skipped, time spent downloading, average and peak speed, same-content files, top sites and file types, and activity per day, week and month. **Export CSV**. Worked out from History on this PC.
- Duplicates found during a run can be handled there: keep both, rename the new copy, or set it aside in `_duplicates`. Nothing is deleted.

## Quality
- **Video streams with several qualities** (HLS): best, a cap such as 1080p or 720p, or smallest. The Activity log says which quality was picked. A video offered in one quality is saved as it is.
- **Subtitles:** when a streamed video lists subtitles, save them beside it as `video name.en.vtt`, in the languages you prefer. The log lists the languages a video offers even when this is off.
- **TikTok in original quality** and **lossless audio** from Bandcamp and Octave when the release has it.

## Easier to use
- **Search Settings**, **reset** one setting or a whole tab to its default, and **profiles**: save the current settings under a name and switch between them. Export and import settings as a file (sign-ins are never included).
- **File list:** search, sort by page order, name, size or state, resize columns, and show more columns (site, speed, folder, retries). Right-click a file to copy its link, name or error, open it, or show it in its folder.
- Drop a `.txt`, `.csv`, `.json` or `.m3u` file on the window to add its links.
- The window opens where you left it, unless that monitor is no longer connected.

## Not in this version
These parts of the plan are not done yet: a priority column in the queue, audio-only downloads (extracting the sound from a video), settings regrouped into more categories, and opening `.nexdrop.json` or link files from Explorer (file association).

## Measured
Measured on Linux (4 cores) against a local test server, with `tests/bench_large.py` and a window test; single runs, and Windows numbers will differ. Every saved file was compared byte for byte with what the server sent.

| | 3.0.0 | 3.1.0 |
| --- | ---: | ---: |
| 100 files of 64 KB | 6.6 s | 6.4 s |
| 1,000 files (5 runs of 200) | 46.3 s | 43.9 s |
| 10,000 files (50 runs of 200) | 457 s, all correct | 460 s, all correct |
| First and last of those 50 runs | 8.8 s, 9.1 s | 8.8 s, 9.1 s |
| Highest memory of the download processes, 10,000 files | 177 MB | 177 MB |
| 4 files of 64 MB, server capping each connection at 8 MB/s | 12.0 s | 6.8 s |
| 8 files of 64 MB, no cap (local network speed) | 5.5 s | 7.0 s |
| Window start | 0.68 s | 0.75 s |
| Window memory, idle | 562 MB | 570 MB |
| Window with 10,000 files in the list: longest pause | 44 ms | 18 ms |
| Window start with 10,000 links queued (50 jobs) | 0.79 s | 0.77 s |

- Large files in parts help when a host limits each connection, which many file hosts do. On a connection with no such limit they are slower, because the parts are joined after downloading. Set **Parts per large file** to 1 to turn them off.
- Two problems were found and fixed while measuring: each file took a little longer as the download folder filled (a 200-file run went from 9 s to 16 s over 8 runs into one folder), and large files in parts used twice the memory.
- Window memory is the whole Electron app, measured the same way for both versions.

## Upgrading
Unzip 3.1.0 over your 3.0.0 folder, or update from the banner. Settings, queue, watches, history and sign-ins carry over.

# NEXDROP 3.0.0

## Downloads you can trust
- **A resumed file is never stitched from two different files.** When a download stops half way, NEXDROP remembers which file it was (its size, ETag and Last-Modified, in a small `.part.nxmeta` file beside the `.part`). Resuming asks the server for "the rest of that same file". If the server sends a different file, the wrong part, or the whole file again, the partial file is thrown away and the download starts over.
- **Incomplete files no longer count as saved.** A file must have exactly the size the server announced. Before, a download that ended early, or a web page sent instead of an image or video, could be saved as if it were complete. Direct downloads (links no site handler knows) are checked the same way.
- **Busy servers are retried during the run.** 429 "too many requests" and 5xx "server busy" answers are retried after the time the server asks for (at most 60 s), otherwise after 2, 4, 8 ... up to 20 s. A deleted file (404) is not retried.
- **A site that keeps failing is paused, not the whole run.** After 6 server errors in a row from one site, that site is paused for 1 minute (then 2, 4 ... up to 10), its files fail quickly as "Site paused", and the other sites keep downloading. The retry pass at the end picks them up.
- **A server that asked to slow down gets its connections back** after two quiet minutes, one at a time.
- **Expired links are refreshed.** Bunkr, GoFile, Kemono and other links that expire are asked for a fresh link from the page they came from in the retry pass, and the file is matched up again.

## Smart Naming 3.0
- **Filename templates** (Settings → Downloads): for example `{creator}-{title}-{number}` or `{date} {title}`. Variables: title, original, album, thread, post, creator, date, site, type, number. A variable the site does not provide is left out; if none of the name variables are known, the automatic name is used. A live example shows the result.
- **Number digits:** 001, 0001 or 00001.
- Jobs started with 2.5 keep their names when you resume them.

## Duplicates
- A newly saved file with exactly the same content as one you already have is reported in the run log, with both paths. **Both files are kept. NEXDROP never deletes a file.**

## Job manager
- Click a job in the queue to rename it, move it to the top or bottom, **hold** it, duplicate or remove it, or give it **its own folder, download mode and speed cap**.
- **Queue it:** a paused run can wait in the queue while the next job starts. It continues where it stopped when its turn comes.
- **Export** the queue to a file and **Import** it again. Import also reads plain link lists: `.txt`, `.csv`, `.m3u` and JSON.

## Speed
- **Download modes** (Settings → Speed): Auto, Balanced, Maximum speed, Low resource or Custom.
- The engine starts about 4 seconds faster, and every file is read once for all three checksums instead of three times. On a local test server: 100 small files 9.0 s → 4.1 s, eight 50 MB files 9.4 s → 3.9 s, 1000 small files 34 s → 25 s (single runs on Linux; Windows numbers will differ).

## History and sites
- History filters by age and file type, shows why a file failed, and finds **saved files that are no longer on disk**. **Export CSV** saves every matching row.
- **Site health** (Settings → Sites): how downloads from each site went over the last day, week or month, and the main reasons for failures. Worked out from your own history; nothing is sent anywhere.
- The file list shows the failure category ("Link expired", "Server busy", "Deleted" ...) with advice on what to do.

## Backup and support (Settings → App)
- **Back up / Restore** your settings, queue, watched threads, history and learned names. Sign-ins, cookies and API keys are never in a backup. Restoring keeps your current files as `.before-restore` copies.
- **Save diagnostics** writes a text report for bug reports with tokens, passwords, cookies, e-mail addresses and your user name removed.

## Security and safety
- Saved site cookies are encrypted for your Windows user (Electron safe storage, DPAPI for engine session files). Older plain files are encrypted the first time they are read. Cookie copies made for a run are removed when the run ends, including after a crash.
- Quitting NEXDROP pauses a running download properly instead of killing it.
- A locked history database is no longer mistaken for a damaged one; a damaged one is moved aside with its journal, never deleted.

## Upgrading
Unzip 3.0.0 over your 2.5.0 folder, or into a new folder and copy `engine\appdata` across. Settings, queue, history and sign-ins carry over. The bundled engine configuration is replaced by the new version automatically (your settings are in the app, not in that file).

# NEXDROP 2.5.0

## Smart file names
- **Hash-like names get a real title.** Files named like `a9c82fa8.jpg` or `IMG_8403.png` take the title of what they belong to: the file's own title on the host, the post's caption or title, the album, the thread or the creator. For example `Summer Photoshoot-001.jpg`. Settings → Smart naming → "Enable smart filename detection" (on by default).
- **Nothing is made up.** Every possible title gets a score. Site names, "Download"/"Watch" pages, sign-in and error pages, adverts, "here are the links", thank-you and credit replies and questions to other members are rejected. If no title scores well enough, the file keeps its own name. Real names like `Beach day.jpg` are kept ("Preserve meaningful original filenames").
- **The linking post wins.** A file on Bunkr, Cyberdrop, GoFile and similar hosts that was linked from a SimpCity (or other forum) post is named after that post, not the host's random album name. Each post's files get that post's title, never a neighbour's.
- **Captions are tidied.** Lead-ins ("Here is my new ..."), calls to action ("Check my profile ..."), trailing thanks, picture counts, emoji and hashtag runs are taken off.
- **Per-file titles** from ImgBB and other Chevereto hosts, Imgur, Flickr and RedGifs, and tweet and TikTok captions.
- **Numbers follow the page order** across the whole job (`-001`, `-002` ...), even when files finish out of order, and the same names come back after a restart. Settings → Smart naming → "Sequence numbering": suffix, prefix or off.
- Options: filename strategy (automatic, original, media title, post title, album title), folder naming, maximum filename length, website name in the folder.

## Folders
- **Folder for each title.** A renamed file goes into a folder named after its title: `Leaked set pics-193.jpg` is saved in `Leaked set pics`. Files that keep their own name stay in the thread's or album's folder. Settings → Smart naming → "Folder for each title" (on by default).
- **No more "Loose Files" folders.** A file linked on its own goes into the folder of the thread that linked it, or straight into your download folder.

## New, all optional and on this PC only
- **Read titles inside files** (off by default): when nothing on the site names a file, use the title stored inside it, from video and audio tags, photo titles or the one folder inside a zip. Nothing is extracted or uploaded.
- **Learn from my renames** (on): rename a file in History, and later files with the same title from the same post, album or thread get your name. Settings → Smart naming → Learned names shows them and can forget them.
- **Explain file names** (off): writes a `.naming.log` beside each run log with every title considered, its score and why it was picked or rejected. Links are written without query strings, and no cookies or tokens are written.

## Measured
On the labelled test cases in `tests/` (`python tests/naming_accuracy.py`), the right title was picked for 98% of 102 cases, 100% of 44 held-out cases and 95% of 21 more held-out cases (the earlier engine: 60%, 50%, 48%). No file was given another post's title.

# NEXDROP 2.4.3

From the 2026-10-25 23:18 (Coomer) and 23:21 (Kemono) runs on 2.4.2.

## Fixed
- **Coomer and Kemono runs no longer hang when the file servers can't be reached.** The profiles were read fine, but every file download to `n1`–`n4.coomer.st` and `n2`–`n4.kemono.cr` timed out before the server answered, so a big profile sat for an hour without saving anything. After 8 timeouts in a row on one site, NEXDROP now skips that site's other files at once and marks them failed, so **Retry unfinished** can get them later. Other sites in the same run carry on.
- The end-of-run summary explains it: the site opened but its file servers never answered, which almost always means your internet provider or network blocks them. Open one of the failed links in your browser. If it doesn't load there either, turn on a VPN or set a proxy (Settings → Speed → Proxy, "Use the proxy for": everything), then use Retry unfinished.
- **Blender files.** `.blend` files on Dropbox (and `.blend1`, `.unitypackage`, `.safetensors`, `.procreate`, `.aseprite`, `.torrent`, `.sketch`) were rejected as "Invalid File Extension". They download now.

## Not fixable from NEXDROP
- `patreon.com/<creator>/posts?tag=...` links inside Kemono posts point at the creator's Patreon page itself. Kemono already archives those posts, so they are left alone.
- `of.tv` and `onlyfans.com` links inside Coomer posts have no downloader. MEGA files that were deleted (410 Gone) can't be fetched.

# NEXDROP 2.4.2

## Fixed
- **Coomer and Kemono profiles.** Links like `https://coomer.st/onlyfans/user/<name>` and `https://kemono.cr/patreon/user/<id>` were rejected with "No handler accepted this page" and nothing was downloaded. NEXDROP now downloads Coomer and Kemono creator profiles, single posts, searches (`/posts?q=...`) and favorites (with your sign-in cookies), page by page through every post. Old `coomer.su`, `coomer.party`, `kemono.su` and `kemono.party` links work too.
- Coomer and Kemono are asked for at most 2 pages a second, so a large profile doesn't get your IP rate-limited.

# NEXDROP 2.4.1

Every error in the 2026-10-25 run log was checked. These are fixed or have a workaround now.

## SimpCity: staying under its rate limit
- **Every SimpCity request waits its turn.** Pages and attachments go out at most 2 at once and 2 seconds apart (Settings → Speed → "Seconds between SimpCity requests", 1 to 30). Thread pages keep their slower pace of one every 20 seconds. Other sites are not slowed down.
- **"Too many requests" pauses SimpCity instead of hammering it.** On a 429 (or a 503 that says when to come back) NEXDROP pauses all SimpCity requests, for the time SimpCity asks or 30 s, then 60 s, 120 s ..., and tries again. After 5 in a row it stops contacting SimpCity for the rest of the run and says so, so your IP address doesn't get blocked.
- **The SimpCity session is reused.** SimpCity's cookies (your sign-in and the bot-check clearance) are kept for the next run, so each run doesn't look like a new visitor solving a new bot check. Cookies you export under Settings → Sign-ins always win over the saved ones.
- **Proxy for SimpCity only.** Settings → Speed → "Use the proxy for" → SimpCity only: SimpCity pages, attachments and its FlareSolverr bot check go through the proxy, and every other site downloads directly at full speed.

## Album folders match the file names
- With "Name files after the album" on, the album's folder now has the same name as its files: `Peachuu Tae\Peachuu Tae 001.png` instead of `Peachuu Tae 8TwlNRmn (Bunkr)\Peachuu Tae 001.png`. Thread, profile and "Loose Files" folders keep the site name. Turn the setting off to get the old folder names back. "Folder for each post" (dated post folders) works as before.

## Fixed
- **Old Coomer links.** Links to `c1.coomer.party` ... `c6.coomer.party` (and `coomer.su`, `kemono.party`, `kemono.su`) failed with "getaddrinfo failed", because those server names no longer exist. They now download from `coomer.st` / `kemono.cr`, which serve the same `/data/...` files.
- **Fileditch.** Fileditch's new download links (`walk10ksteps.xyz/...`) were rejected with "422 Unable to extract a valid download URL". They download now, with the file's real name.
- **Pornhub profiles on regional sites.** `jp.pornhub.com/model/...` failed with "422 ... h1 tag not found". The profile's folder now falls back to the name in the link and its videos are found.
- Every engine error report contained `repr-error "'DownloadClient' object has no attribute '_supports_ranges'"`, which hid part of the real error. Fixed.
- **Passwords.** A password-protected Cyberfile link now says how to add the password: put `?password=THE_PASSWORD` at the end of the link.
- A file whose server name no longer exists is reported as such at the end of the run ("DNS lookup failed"), instead of as a generic failure.

## Not fixable from NEXDROP
- Deleted files (404) on GoFile, GoonBox/jpg.church 2022 images and removed Pornhub videos.
- X searches (`x.com/search?...`), which X only shows to signed-in users.
- Fantia, Fansly, OnlyFans, link-in-bio pages (mfco.link, mond.how, b23.tv, lu.ma) and Amazon wishlists have no downloader.
- Bunkr's `cdn.cr` servers sometimes stall a video ("Timeout" or "ClientPayloadError"). Retry unfinished picks those up.

# NEXDROP 2.4.0

## New
- **Check for updates.** NEXDROP asks GitHub for the newest release when it starts and twice a day, and shows a banner when there is one. **Update now** downloads the Windows zip, checks it against the release's SHA-256 checksum, and restarts into the new version. Settings, sign-ins, history and downloads are kept. Settings → App → "Check for updates" checks by hand; the automatic check can be turned off there.
- **Keep running in the tray.** Settings → App → "Keep running in the tray when closed": closing the window keeps downloads and watched threads going. The tray menu opens NEXDROP, pauses, checks watched threads, opens the download folder or quits. The tray icon's tooltip shows the run's progress.
- **Start with Windows.** Settings → App. With the tray option on, NEXDROP starts in the tray, so watched threads are checked without opening the window.
- **Add copied links automatically.** Settings → App. Links copied in other programs go into the links box. Nothing starts until you press Start.
- **Send to NEXDROP from the browser.** A Chrome / Edge / Brave extension is in the new `browser-extension` folder. Right-click a page, link, image or video and choose "Download ... with NEXDROP", or click its toolbar button. Turn on Settings → App → "Open links sent from the browser" first; `browser-extension\README.txt` explains how to add the extension. Links only land in the links box.
- **Download hours.** Settings → Speed → "Only start downloads at certain times": links started outside those hours, and watched-thread checks, wait in the queue and start when the hours begin. Press Start with an empty links box to start the queue now anyway.
- **Quiet hours.** Settings → Speed → "Slower in quiet hours": runs that start in those hours use a lower speed cap.
- **Proxy.** Settings → Speed → Proxy: an `http://` or `https://` proxy for every download. Telegram account downloads connect directly.
- **History.** Settings → History lists every file from earlier runs. Search by file name, link, site or the link you pasted, filter by site and result, show a file in its folder, or add its link again.
- **Name files after the album.** Files from an album (Bunkr, GoFile, Cyberdrop and other album pages) are saved as the album's name and their place on the album page, like `Peachuu Tae 001.png`, instead of `IMG_8916.png`. Single files keep their own name. Settings → "Name files after the album" (on by default).
- **Folder for each post is optional.** X, TikTok, Bluesky, Patreon, Kemono and similar sites put each post in a dated folder like `2024-12-30 - 1873747839563755753`. Turn off Settings → "Folder for each post" to save a profile's files together in its folder. Gallery sites (GirlsReleased, WordPress posts) keep the set's name as the folder either way.

## Fixed
- The Start and Pause buttons were wired up twice, so one click on Start could queue the same links a second time while the run began, and Pause showed "Stopping…" instead of "Pausing…".
- Clicking a "Download finished" notification brings back a hidden or minimised window.

## Project
- The bundled patched Cyberdrop-DL 10.10.0 engine is unchanged.
- `tools/engine-upstream-report.py` compares the bundled engine with a newer Cyberdrop-DL release on PyPI and lists which files changed upstream only, which need merging with a NEXDROP patch, and which are new sites. It only writes a report and never changes `engine/`.

# NEXDROP 2.3.4

Every failure in the earlier run logs was checked. These are fixed:

## Fixed
- **TikTok sounds.** A post's background sound often failed with 403 Forbidden, because TikTok's US file server refuses many countries. NEXDROP now gets the same sound file from TikTok's worldwide music server.
- **Bunkr storage links.** A copied Bunkr storage link (`*.scdn.st/storage/...`) stops working once it expires (403). The end of the run now says so and suggests pasting the Bunkr page link (`bunkr.cr/f/...`) or using Retry unfinished.
- **SimpCity attachments.** When an attachment returns SimpCity's own error page and the automatic retry fails again, the end of the run keeps the accurate explanation (removed, or your account can't view it) instead of switching to "came back as a web page".

## Not fixable from NEXDROP
- Old GoonBox / jpg.church images from 2022 (`simp1.cuckcapital.cr/images/2022/...`) are gone from every GoonBox server (404).
- Pixeldrain files locked behind a captcha need a browser or a paid Pixeldrain key. NEXDROP won't get around captchas.
- SimpCity attachments that SimpCity won't show your account, deleted GoFile, 1fichier and X links, MEGA's free transfer quota, and X searches, which X only shows to signed-in users.

# NEXDROP 2.3.3

## Fixed
- **Resume continues the paused run instead of looking like a restart.** The file list, the saved count, the downloaded total and the activity log stay as they were at the pause. Files saved before the pause stay "Saved" (not "Already saved"), and unfinished files go back to Waiting. Downloading itself already continued correctly: finished files were skipped and half-finished files carried on from where they stopped. NEXDROP still reads the links again from the start to find the rest, and the activity log now says so.
- A half-finished file that continues after Resume no longer counts its earlier bytes again in the downloaded total, and its speed no longer jumps when it starts.

# NEXDROP 2.3.2

## New
- Monotirou search and tag links: paste `https://monotirou.com/?q=<words>` or `https://monotirou.com/tags/<tag>` to download every video the search or tag lists.

## Fixed
- Pixeldrain files that Pixeldrain locks behind a captcha now say so (open the link in a browser, or add a Pixeldrain API key from a paid account) instead of failing with a hotlinking 403.
- GoonBox links of the form `goonbox.cr/images/<file>` are tried as files. Dead ones now show as failed with the server's answer instead of "No downloader for this kind of link".
- The run log no longer contains the Telegram API hash and session. It only notes that the Telegram sign-in is in use. Run logs from earlier versions in `engine\appdata\logs\runs` may contain them; delete those if you share logs.

## Changed
- User-visible engine messages, the engine's default folder names and its command-line help say NexDrop instead of cyberdrop-dl. The command-line help still names Cyberdrop-DL as the engine, and the GPL-3.0 notice is unchanged.

## Project
- The README now covers every feature, credits Cyberdrop-DL and the other projects NEXDROP uses, and shows screenshots.
- `docs/SUPPORTED-SITES.md` lists all 226 sites the engine supports, with their domains and link types, generated from the engine itself.
- The Monotirou crawler is now recorded in `engine/vendor-patches` with the other engine changes.

# NEXDROP 2.3.1

## Fixed
- Monotirou profile and individual video links now route correctly.
- Downloads use the media URL returned by the video API instead of guessing a CDN filename from a numeric ID.
- Profile pagination uses the API's timestamp-and-ID cursor. Repeated pages stop with a clear error instead of looping.
- Missing or malformed API metadata is reported as a scrape error.

# NEXDROP 2.3.0

## New: Telegram
- **Public channels and posts, no account needed.** Paste `t.me/<channel>`, `t.me/s/<channel>` or `t.me/<channel>/<post>` (telegram.me and telegram.dog work too). NEXDROP reads Telegram's public web preview and saves photos, videos, round videos and voice notes, oldest post first, numbered in channel order, into `<Channel name> (Telegram)`. A post link saves the whole album; add `?single` for just that file. Files already saved are skipped on the next run, even though Telegram's file links change on every visit.
- **Sign in with your Telegram account for everything else** (Settings > Sign-ins > Telegram): files over the public preview's size limit, documents (zip, pdf, apk, ...), channels and groups you've joined, private links (`t.me/c/<id>/<post>`) and invite links of chats you're in. Enter your own API ID and API hash from my.telegram.org, your phone number, then the code Telegram sends you (and your two-step verification password if you use one). The session is encrypted with Windows like the other sign-ins and never shown; Check confirms which account is signed in. NEXDROP only reads and downloads: it never posts, joins or leaves anything.
- Without the sign-in, files the public preview leaves out show as Skipped with "sign in to Telegram", and the end of the run says how many there were.
- Telegram downloads use the same file list, progress bars, numbering, history, file-type filters, type folders and Pause as everything else. Up to 4 files download at once per account; cryptg (bundled) does Telegram's encryption in C for speed.
- **Whole private channels and groups**: paste `t.me/c/<id>` (a message link without the last number), or just the chat ID as Telegram shows it, for example `-1001988700745`. `t.me/...` links without `https://` are accepted too.
- A `t.me` channel link found inside a forum post is not followed: pulling a whole channel from a promo link would be a surprise. Paste the channel link to get it.

## Fixed
- Files whose links carry a one-time token (Telegram and many CDNs) could appear twice in the list: once numbered and Waiting, once downloading without its number. NEXDROP now strips tokens and signatures from links before showing or recording them, so each file is one row, and tokens no longer end up in logs or history.
- The total downloaded size stopped counting during the automatic retry pass, because each engine pass counts from zero.

# NEXDROP 2.2.0

## New
- **Queue.** While a run is going, paste more links and press **Add to queue**. They start one after another. The queue is shown under the links box, items can be removed, and it is kept if NEXDROP closes. With an empty links box, **Start** runs the queue.
- **Pause and resume, even after closing NEXDROP.** Stop is now **Pause**. A paused run, or one interrupted by closing the window or a crash, shows a **Resume** bar the next time NEXDROP opens. Resume skips files that already finished (also for runs started with Download again) and continues half-downloaded files from where they stopped.
- **Watched threads.** After running a forum thread, **Watch for new posts** adds it to Settings > Watched (or paste a thread link there). NEXDROP checks each watched thread every 1–24 hours while it is open, starting from the last post it saw, so only new posts are scanned. Checks wait in the queue behind your own runs. Each shows when it was last checked and how many new files it found.
- **Taskbar progress and a "finished" notification.** The NEXDROP taskbar button fills up during a run (red if files failed), and a Windows notification appears when a run finishes or pauses while the window isn't in front. Clicking it brings NEXDROP back.
- **Automatic retry for temporary errors.** Files that fail because a server is busy (503, 502, 500, 504), rate-limited (429) or timed out are tried once more 45 seconds after the run, by re-opening the album, post or thread they came from. The log reports how many the retry saved. On by default (Settings > Downloads).
- **File types and folders** (Settings > Downloads): choose Images, Videos, Audio and Other files; skip images smaller than a size (thumbnails, icons); and optionally sort new files into Images, Videos, Audio and Other folders inside their usual folder. Files left out by a filter show as Skipped with the reason and are not written to history, so turning the type back on downloads them.

## Fixed
- A second engine pass in the same run overwrote that run's engine log. The retry pass now writes its own `.retry.engine.log`.
- A link with a trailing colon in an engine message created a duplicate row.
- Files a page listed but the engine never attempted could stay "Waiting" after a run finished.
- The new strip under the links box no longer leaves a gap above the file list.

## Project
- The full source is now in the GitHub repository, including the patched engine, with tests that run on Windows through GitHub Actions on every push.

# NEXDROP 2.1.3

From the third SimpCity run (allecakes: 4,457 files, 17.3 GB saved, 82 failed).

## Faster
- **Bunkr downloaded one file at a time per server.** The engine's Bunkr handler locks each CDN server to one download. This thread's 1,675 Bunkr files (11 GB) sat on two servers, so from 12:34 to 13:18 the run crawled at 0.2–6 MB/s after peaking at 43 MB/s. Now 3 connections per server (Settings > Speed > Connections per server, 1–6). If a server answers 429 or 503, that server alone drops back to 1 connection and the log says so.
- **TikTok sounds are optional and off by default.** 913 of the 1,850 TikTok files were each post's background sound saved as a separate .mp3, and all 11 TikTok failures (403 Forbidden) were these. Videos keep their audio. Turn them back on under Settings > Downloads > TikTok sounds.

## Clearer
- SimpCity's "Oops! We ran into some problems." page is the forum's own error page (the attachment was removed, or the account can't view it), not a bot check. NEXDROP no longer retries it through FlareSolverr, and the summary says what it means and how to check it in a browser.
- Temporary server errors (503, 502, 429) get their own line suggesting Retry unfinished later.

# NEXDROP 2.1.2

From the second SimpCity run (allecakes, 1,158 files).

## Fixed
- **SimpCity attachments failed with "Invalid Content Type".** The sign-in cookie was valid, but DDoS-Guard's short-lived clearance cookies had expired. The engine retries through FlareSolverr when scanning pages, but not when downloading files, so every attachment got the bot-check page. Downloads now do the same: one FlareSolverr visit per site refreshes the clearance, and the file is fetched again. Tested against a stand-in protected site: 3 of 3 files saved after one clearance.
- When a site still sends a web page instead of a file, its page title is written to the log, so a login wall or a deleted attachment can be told apart from a bot check.
- **Turbo albums failed with `NameError: name 'aio' is not defined`.** A bug in the bundled engine's `crawler.py` affected every site that opens a task group (Turbo, GoonBox albums, Kemono, Kick, SpankBang and more). Fixed in `engine/vendor-patches`.
- **TikTok profiles failed with `Empty URL`** when a post had no music track. Fixed in `engine/vendor-patches`.
- "Failed with several errors" now lists the actual errors (for example `404 Not Found; 404 Not Found`).
- The thread summary showed as a raw `[SimpCity] Forum result ...` warning; it now reads "Scanned /threads/...: 104 posts, 797 files".
- 33 images on `simp1.cuckcapital.cr` returned 404: the host deleted them. The end-of-run summary now says so instead of suggesting a retry.

## Faster
- New defaults: Parallel, 16 downloads at once, 6 per site (were Page order, 2, 1). Settings saved by earlier versions are moved to these once. Limits raised to 32 at once and 16 per site.
- Global request cap raised from 25 to 100 per second. SimpCity's own slow scan rate is kept to avoid bans.
- Direct files handled by NEXDROP's own HTTP fallback now download several at a time instead of one by one.
- Measured on the same 29 MB: Page order 5.9 MB/s, new defaults 73 MB/s.

## Progress
- Every file has its own progress bar with downloaded / total size, current speed in MB/s, time left and a large percentage. The bar changes colour as it fills: copper, then brass, then green; red if it fails.
- Speed is the current speed (smoothed), not the average since the file started.
- Files that are fully received but still being written show "Saving" instead of sitting at 100%.
- Total speed in MB/s.

# NEXDROP 2.1.1

Fixes from the first real SimpCity run (allecakes, 797 files).

## Logs
- Every run now writes a complete log to `engine\appdata\logs\runs\<date>_<time>.log`: settings (no secrets), links, every phase and message, and one line per file with its number, name, link and result. The engine's own detailed log (requests, tracebacks) is written beside it as `.engine.log`. The 30 most recent runs are kept.
- **Open log** and **Logs folder** buttons in the Activity panel.
- FlareSolverr's output now goes to `engine\appdata\logs\flaresolverr.log`, and when it fails its last lines are shown in Activity.

## Fixed
- FlareSolverr gave up after 30 s, while its first start on Windows (it launches its own Chrome) can take longer. NEXDROP now starts it in the background as soon as the window opens, keeps it running between runs, and waits up to 150 s. A status dot in the Activity panel shows starting, ready or failed. Without it, TikTok hit a Turnstile wall and SimpCity attachments came back as an HTML challenge page ("Invalid Content Type").
- A whole-browser cookie export was handed to the engine as is (about 300 sites, including Google, Visa and banking) and every domain was logged. Only cookies for sites the engine supports, or for the pasted links, are now used: in the test, 4 of 260 sites. One summary line replaces the 300-line list.
- The engine database grew to about 525 MB (262 MB file plus 264 MB write-ahead log) holding about 1 MB of history. Cyberdrop-DL reserves 250 MB inside the database on every start. The reserve is now 16 MB, the existing file is compacted once, and the write-ahead log is folded back after each run. Measured: 262 MB to 17 MB, stable across runs.
- Clear next steps at the end of a run instead of raw engine errors: attachments that came back as a web page, MEGA's 509 bandwidth quota, linked threads skipped because Nested threads to follow is 0, bot-check blocks, and a hint to use Parallel for large threads in Page order.
- "Too many downloads queued" is shown once, as information. Skipped linked threads and unsupported link types are information, not warnings. Thread scans read "Scanned /threads/x: 104 posts, 797 files, 2 linked threads."

# NEXDROP 2.1.0

Built on 2.0.0. Every 2.0 feature is kept: source page order and numbering, forum page range, website sign-ins, job history, the source registry, and magnet rejection.

## Fixed

**Security**
- The HTTP fallback could write outside the download folder. A link ending in `..%2F..%2FStartup%2Fevil.jpg` was decoded into a path that climbed out of `Loose Files`. File names are now sanitised (no path separators, Windows reserved names, or invalid characters) and existing files are never overwritten.
- The private-address check did not apply to redirects. `urllib` followed redirects automatically, so a public link could redirect to `127.0.0.1` or a LAN address. Redirects are now followed by hand and every hop is checked.
- Removing a sign-in built a file path from an unchecked id, so a crafted id could delete any `.txt` file. Ids are now checked against the account list.
- Clicking a downloaded file opened it, which would run a downloaded program. Files are now revealed in Explorer; only folders open.
- The window now has a Content Security Policy, a single-instance lock (two windows would share one engine database), and external links open in the browser instead of being silently blocked.

**Downloads**
- Importing a cookie file always failed. `auth_cookies.py` read an `--probe` option that was never defined, so it crashed and the app reported "No cookies for this site were in that file".
- Cookie filtering dropped `#HttpOnly_` lines, which is where session and login cookies (including SimpCity's `ogaddgmetaprof_user`) live. JSON storage-state files are now accepted too.
- A changed fallback cookie file was ignored after the first run: `fallback.txt` was written once into the sign-in folder and never refreshed. Each run now builds a fresh cookie folder.
- A failed download was shown and recorded as Completed. Success was inferred from the progress bar closing, which also happens on failure. Success now comes only from the engine recording a saved file.
- Failures logged as `Download attempt (unsupported domain) Failed:` were not recognised.
- Warnings from the engine arrived as `warn` and were displayed as normal messages.
- Pages skipped from history ("already been scrapped") were counted as skipped files.
- HLS videos showed no progress.
- During parallel downloads only one file's progress updated per tick, so other bars looked frozen.
- Fallback files restarted numbering at 001 instead of continuing the engine's sequence, and ignored the "numbered filenames" setting.
- Stop killed the session outright, so history and the job database were not saved for files that had finished. Stop is now graceful: the engine is cancelled, history is saved, and a summary is reported. Unfinished files are marked Stopped, not Failed.
- Events from a stopped run could reach the next run, and a UTF-8 character split across two pipe reads was corrupted.
- Over 40 links were silently dropped; the limit is now 200 and you are told when it applies.
- A host that does not resolve said "Private and local addresses are blocked"; it now says it could not be resolved.
- The order manifest grew without limit; it is now trimmed.
- If FlareSolverr could not start (for example, antivirus blocked or quarantined the exe), the whole run crashed. It now warns and continues without it.

## Faster
- Progress is sent as one batch of changed files four times a second instead of one message per chunk per file.
- The file list only creates the rows you can see. 5,000 files render with about 23 row elements and scroll at 60 fps.
- Activity log updates are batched per frame and capped at 500 visible lines.
- The window no longer freezes while Python runs: the site check (up to 2 minutes), cookie import, sign-in check, the "downloaded before" lookup and startup job recovery all run in the background or without Python.
- The job database uses WAL mode and a quick startup check.
- The window log is written through one stream and rotated at 5 MB.
- Interface images went from 1.26 MB to 230 KB.

## Redesigned window
- Copper on basalt, taken from the NEXDROP logo. Rows fill with copper as they download and cool to verdigris (copper's patina) when saved; rust marks failures.
- One command bar: links box, Start or Stop, Settings. Order, page range and Download again sit right below it.
- A four-step phase track: Prepare, Find files, Download, Finish.
- Downloads are the main view, with a running tally (files saved, speed, size, time), tabs for Active, Saved, Skipped and Unfinished, and a site filter.
- Recent links appear on the empty screen and can be added again with one click.
- "Downloaded before" asks once: Get new files only, or Download everything again.
- Settings moved to a side panel with four tabs: Downloads, Speed, Sign-ins, Sites.
- Removed controls that did nothing: Live Browser, Update extractors, Headless, Resolve links, Same-host links, Chrome CDP, Storage state, Performer, Crawl jobs, Resolve workers, Delay, Max pages, resume-from-page, and the cookie prompt on every launch.
- Ctrl+Enter starts, Ctrl+, opens settings, links or a .txt file can be dropped on the window.
- Parallel modes allow up to 8 downloads and 4 per site (was 4 and 2). Page order still uses 1.

## Build
- `windows/build-electron-portable.sh` now copies every v2 file (credential store, sign-in list, the four helper modules) and FlareSolverr. The previous script would have produced a build that failed at startup.
- Added `tests/` (Python and Node).
