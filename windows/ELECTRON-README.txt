NEXDROP 2.3
One interface. Multiple sources. Real-time control.

This folder is the portable Windows x64 build. NexDrop.exe is the Electron
shell. It opens the NEXDROP window from the local files in resources\app. It
does not open Chrome, Edge, or a hosted website, and it does not use a cloud
backend.

Layout
  NexDrop.exe                 Electron desktop shell, renamed
  resources\app               NEXDROP window, preload bridge, main process
  desktop\cairn_session.py    runs one download session on the engine
  desktop\auth_cookies.py     per-site cookie files (Netscape or JSON)
  desktop\job_store.py        local job history (jobs.sqlite)
  desktop\source_registry.py  unsupported-link log and site check
  engine\bridge.py            NexDrop download engine adapter
  engine\vendor               patched engine source (do not replace)
  engine\thirdparty           Telethon, for the Telegram sign-in
  engine\appdata              settings, history, logs, Electron profile
  runtime\python              embeddable CPython for Windows
  runtime\ffmpeg              ffmpeg and ffprobe, used for HLS video
  runtime\flaresolverr        FlareSolverr, used for bot-check pages
  downloads\nexdrop           default save folder
  licenses                    GPL-3.0 (Cyberdrop-DL), Cairn MIT, font OFL

Run
  1. Verify SHA256SUMS.txt.
  2. Extract the whole folder. Keep NexDrop.exe beside resources, engine,
     desktop, and runtime.
  3. Run NexDrop.exe. Only one window opens at a time.

Using it
  Paste links (one per line) and press Start, or Ctrl+Enter. You can also
  drop a .txt file of links onto the window, or use Import.
  Page order downloads one file at a time in the order the post lists them.
  Parallel keeps that numbering but downloads several at once. Fastest takes
  whatever finishes first.
  Pages limits forum threads that use /page-N. Leave the second box empty to
  continue to the last page. Other sites ignore it.
  Files already saved are skipped. If a link was downloaded before, NEXDROP
  asks whether to get new files only or download everything again. Tick
  Download again to skip the question.
  Stop finishes cleanly: files already saved stay saved, the rest are marked
  Stopped, and Retry unfinished puts them back in the links box.
  Magnet links and .torrent files are rejected; this build has no BitTorrent
  client.
  Direct files no site handler accepts are fetched over HTTP, up to 80 MB.
  Private, loopback, and link-local addresses are refused, including after a
  redirect.

Settings (the sliders button, or Ctrl+,)
  Downloads   save folder, numbered filenames, deep scan, FlareSolverr,
              nested thread depth
  Speed       downloads at once (1-8), per site (1-4), attempts, speed cap,
              timeouts
  Sign-ins    Telegram account, API tokens (GoFile, Pixeldrain, Nova,
              Real-Debrid), MEGA email and password, SimpCity or one other
              site by cookie file, plus cookie files used for every site

Telegram
  Public channels need no account: paste t.me/<channel> or a post link.
  For large files, documents and chats you've joined (t.me/c/... links),
  open Settings > Sign-ins > Telegram. Create an app once at
  my.telegram.org/apps, enter its API ID and API hash and your phone number,
  press Send code, then type the code Telegram sends you. NEXDROP only reads
  and downloads; it never posts or joins anything.
  Sites       site handler check and the unsupported-link report

Sign-ins and cookies
  Tokens and the MEGA password are encrypted with Electron safeStorage (the
  Windows credential protection) and are never shown again.
  Cookie files may be Netscape cookies.txt or a storage_state.json. A site
  sign-in keeps only that site's cookies, including HttpOnly session cookies.
  The SimpCity login cookie is ogaddgmetaprof_user.
  NEXDROP does not decrypt Chrome's cookie database or read passwords and
  payment data from your browser. Export a cookies.txt for the site instead.

Data
  Settings, history, job database and logs stay in engine\appdata next to
  NexDrop.exe. They stay inside this portable app folder.
  engine\appdata\logs\gui.log    window log (kept under 5 MB, one backup)
  engine\appdata\runs.json       links you ran, shown as Recent links
  engine\appdata\jobs.sqlite     per-file job history
  logs\unsupported-sources.csv   links no handler accepted

Troubleshooting
  If Windows SmartScreen warns about the unsigned executable, choose More
  info, then Run anyway. If an antivirus tool quarantines a runtime file,
  restore it. A short folder path avoids the Windows 260-character limit.
  "The download engine adapter is missing" means the desktop folder was not
  extracted beside NexDrop.exe.
  "Could not start the bundled Python" means runtime\python\python.exe is
  missing.
  SimpCity requests are rate-limited by the patched crawler. A thread can sit
  quiet for a while after "Crawling" and still be working.
