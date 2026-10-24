# NEXDROP

One interface. Multiple sources. Real-time control.

NEXDROP is a standalone Windows x64 download manager. It integrates the patched Cyberdrop-DL 10.10.0 engine and uses an HTTP fallback for supported direct-file URLs.

## Download

Download `NexDrop-windows-x64.zip` from the latest [release](https://github.com/Saiki1997/NexDrop/releases) and check it against the SHA-256 checksum published with it. Extract the whole folder and run `NexDrop.exe`.

To update an existing NEXDROP folder, close NEXDROP and extract the new zip over it. Settings, sign-ins, history and downloads in `engine\appdata` and `downloads` are kept.

## What it does

- Paste thread, album, profile or file links and press **Start**. Files from SimpCity and the other forums and file hosts the bundled engine supports are saved in page order (`001_`, `002_`…), many at a time.
- Every file has its own progress bar with size, percentage and speed in MB/s.
- **Queue** more links while a run is going. **Pause** keeps a run; **Resume** continues it, even after closing NEXDROP.
- **Watch** forum threads: NEXDROP rechecks them every few hours while it is open and downloads only new posts.
- Bot-check pages (DDoS-Guard, Cloudflare) are solved by the bundled FlareSolverr, which starts in the background.
- Temporary server errors are retried automatically. Files already saved are skipped.
- Filter by file type or minimum image size, and optionally sort into Images, Videos, Audio and Other folders.
- Sign in with API tokens, a MEGA account or per-site cookie files. Secrets are encrypted with Windows credential protection.
- Every run writes a readable log to `engine\appdata\logs\runs`.

## How it is built

`NexDrop.exe` is Electron 42. It opens the window from `resources\app` and starts the bundled Python (`runtime\python`) to run `desktop\cairn_session.py`, which drives the patched Cyberdrop-DL 10.10.0 engine through `engine\bridge.py`. ffmpeg (`runtime\ffmpeg`) handles HLS video and FlareSolverr (`runtime\flaresolverr`) handles bot checks. Settings, history and logs stay in `engine\appdata`; files are saved to `downloads\nexdrop` unless you choose another folder.

NEXDROP has no hosted website, remote API or cloud database. It only connects to the sites you download from.

## System requirements

- 64-bit Windows 10 or Windows 11
- Nothing else to install: Python, ffmpeg and FlareSolverr are bundled

## Troubleshooting

- Keep `NexDrop.exe` beside `resources`, `desktop`, `engine` and `runtime`. A short folder path avoids the Windows 260-character limit.
- `NexDrop.exe` is not code-signed. If SmartScreen warns, choose **More info**, then **Run anyway**. If antivirus quarantines a runtime file, restore it.
- For a failed run, press **Open log** in the Activity panel. The run log and the engine's detailed log are in `engine\appdata\logs\runs`; FlareSolverr's output is in `engine\appdata\logs\flaresolverr.log`.

## Testing status

The Python and Node tests run on Windows (GitHub Actions) on every push. Downloads, pause and resume, the queue, automatic retry and filters were tested end to end in the Electron window on Linux, and real SimpCity runs of over 4,000 files were made on Windows with 2.1.x.

## Source code

This repository holds the full source of the Windows build:

| Folder | What it is |
| --- | --- |
| `electron/` | The NEXDROP window: main process, preload bridge, and the page in `renderer/` |
| `desktop/` | Python that runs one download session, sign-in cookies, job history, the site registry |
| `engine/bridge.py` | Adapter between NEXDROP and the bundled engine |
| `engine/vendor/` | Patched Cyberdrop-DL 10.10.0 (GPL-3.0), with the fixes from `engine/vendor-patches/` applied |
| `windows/` | `build-electron-portable.sh` and the pinned Python requirements |
| `tests/` | Python and Node tests, run on Windows by GitHub Actions on every push |

What changed in each version is in `CHANGELOG.md`.

### Running the tests

```
python -m pip install -r windows/requirements-win.txt
python tests/test_desktop.py
node --test tests/format.test.js
```

### Building the portable folder

`windows/build-electron-portable.sh` downloads Electron 42.2.0 for Windows and assembles the
portable folder. It expects the embeddable Python, ffmpeg and FlareSolverr in
`release/NexDrop/runtime/` (they are too large for this repository; take them from a release zip).

## Licensing

NEXDROP includes Cyberdrop-DL (GPL-3.0-only); its patched source is in `engine/vendor`, and this repository is licensed under GPL-3.0 (`LICENSE`). The interface started from Cairn (MIT, `electron/LICENSE-CAIRN.txt`). The Archivo and Manrope fonts are under the SIL Open Font License (`electron/renderer/fonts`). Each release also has `NexDrop-source.zip`, the source it was built from.

## Issues

Please report reproducible errors, startup failures, and compatibility issues through the repository's [Issues](https://github.com/Saiki1997/NexDrop/issues) section. Do not include passwords, authentication cookies, or private account information.
