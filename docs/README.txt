NEXDROP
One interface. Multiple sources. Real-time control.

This folder is the current Windows build. NexDrop.exe is the Electron shell.
The PySide6 experiment is not included. The window is the local Cairn interface.
It does not open Chrome, Edge, or a hosted website, and it does not use a cloud backend.

Layout
  NexDrop.exe                 Electron desktop shell, renamed
  resources\app               NEXDROP window, preload bridge, main process
  desktop\cairn_session.py    maps run events onto the engine
  engine\bridge.py            patched Cyberdrop-DL 10.10.0 adapter
  engine\vendor               patched engine source (do not replace)
  engine\appdata              settings, history, logs, Electron profile
  runtime\python              embeddable CPython for Windows
  runtime\ffmpeg              ffmpeg and ffprobe, used for HLS video
  downloads\nexdrop           default save folder
  licenses\GPL-3.0.txt        Cyberdrop-DL
  licenses\Cairn-MIT.txt      Cairn interface
  SHA256SUMS.txt
  UI-COMPARISON.md

Run
  1. Verify SHA256SUMS.txt.
  2. Extract the whole folder. Keep NexDrop.exe beside resources, engine,
     desktop, and runtime.
  3. Run NexDrop.exe.

The window title is NEXDROP. Paste public http(s) URLs and press Run. File types,
domain folders, and sorting come from engine\user-config.yaml. Sorting is off in
that file, so files stay in a folder per site. Completed URLs are stored in
engine\appdata\completed-urls.json and skipped on the next run unless you choose
Start from beginning.

FlareSolverr 3.5.2 is included in runtime\flaresolverr. The FlareSolverr checkbox
starts it on 127.0.0.1:8191 when it is not already running, then the engine uses
it for DDoS-Guard pages such as SimpCity. It is stopped when the run ends.
Leave the box checked. A cookies file is still required for posts that only
appear after login. The SimpCity crawler is registered for simpcity.cr and
simpcity.su.
Start page and Last page apply to forum threads that use /page-N. Page 10
through 25 includes both pages. Leave Last empty to stop when the forum has
no next page. Other sites ignore those boxes. Magnet links and .torrent files
are rejected with an unsupported message. See SOURCE-COMPATIBILITY.txt.
Direct files the engine will not accept are transferred over HTTP, up to 80 MB.
Private, loopback, and link-local addresses are rejected.

Settings, history, and engine logs stay in engine\appdata next to NexDrop.exe.
They are not written to %APPDATA%\cyberdrop-dl.

The Windows DNS line is a fallback to the system resolver, not a crash.
SimpCity requests are rate-limited by the patched crawler. A thread can sit
quiet for a while after "Starting patched Cyberdrop-DL" and still be working.
Cookies with brackets in the name are skipped. The rest of the cookie file is
still used. SimpCity login, when the thread requires it, is the cookie
ogaddgmetaprof_user.

What this build does not do
  There is no Playwright live-page capture, so the Live Browser pane stays on
  standby. FlareSolverr is not started. Update does not install gallery-dl or
  yt-dlp; the downloader is the bundled engine.

Troubleshooting
  If Windows SmartScreen warns about the unsigned executable, choose More info,
  then Run anyway. If an antivirus tool quarantines a runtime file, restore it.
  A short folder path avoids the Windows 260-character limit.
  "The download engine adapter is missing" means desktop\cairn_session.py was
  not extracted beside NexDrop.exe.
  "Bundled Python" failures mean runtime\python\python.exe is missing.
  Engine logs are in engine\appdata\logs and engine\appdata\state.
