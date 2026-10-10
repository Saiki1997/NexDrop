# NEXDROP

One interface. Multiple sources. Real-time control.

NEXDROP is a standalone Windows x64 download manager. It integrates the patched Cyberdrop-DL 10.10.0 engine and uses an HTTP fallback for supported direct-file URLs.

## Download

Visit the [GitHub Releases](https://github.com/Saiki1997/NexDrop/releases) page and download `NexDrop-windows-x64.zip`, or all of the `NexDrop-windows-x64-part*.zip` files together with `RESTORE.txt`.

## Installation

1. Download the release archive.
2. Verify the file against the published SHA-256 checksum.
3. Extract it. If you downloaded split parts, extract every part into the same folder, following `RESTORE.txt`.
4. Run `NexDrop.exe`.

## Startup

`NexDrop.exe` opens NEXDROP's own desktop window. It does not open Chrome, Edge, or another external browser. The window uses the WebView2 runtime bundled in `runtime\webview2`. Closing the window stops the local runtime.

## Architecture

The executable starts the bundled Python runtime on `app\host.py`. That host listens only on `127.0.0.1` and serves the interface from local files in `app\ui`. Downloads go through `engine\bridge.py` and the patched Cyberdrop-DL 10.10.0 tree in `engine\vendor`. Settings and engine history stay in `engine\appdata`. Saved files stay in `downloads\nexdrop`.

Internet access is required for the downloads you start and for the sites those downloads depend on. The application does not use a hosted website, a remote API, a cloud database, or a browser login.

## System requirements

- 64-bit Windows 10 or Windows 11
- No separate Python installation is required by the portable package

## Troubleshooting

Keep `NexDrop.exe` beside `app`, `engine`, and `runtime`. A short folder path avoids the Windows 260-character path limit. If Windows SmartScreen or antivirus quarantines the unsigned executable or a runtime file, restore it and run again. Startup errors name the missing piece: the Python runtime, `app\host.py`, or `runtime\webview2`.

## Verification status

Preview testing has previously confirmed a real download and history-based duplicate skipping. The packaged Windows executable has not yet been tested on Windows; successful operation is not guaranteed until that testing is completed.

## Licensing

See the included GPL-3.0 and third-party license notices for licensing terms and source-code information. `NexDrop-source.zip` is the corresponding source archive.

## Issues

Please report reproducible errors, startup failures, and compatibility issues through the repository's [Issues](https://github.com/Saiki1997/NexDrop/issues) section. Do not include passwords, authentication cookies, or private account information.
