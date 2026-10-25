#!/bin/sh
# Build the portable Windows release zip from an earlier release zip plus this source tree.
#
#   windows/make-release.sh <base NexDrop-windows-x64.zip> <output folder>
#
# The base zip supplies the large runtime pieces that are not in git (Electron,
# embeddable Python, ffmpeg, FlareSolverr). Everything NEXDROP-specific comes
# from this repository. Writes NexDrop-windows-x64.zip, NexDrop-source.zip and
# SHA256SUMS.txt to the output folder.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
BASE="$1"
OUT="$2"
mkdir -p "$OUT"
OUT="$(CDPATH= cd -- "$OUT" && pwd)"
WORK="$OUT/build"
rm -rf "$WORK"
mkdir -p "$WORK"
unzip -q "$BASE" -d "$WORK"
S="$WORK/NexDrop-windows-x64"
A="$S/resources/app"
test -f "$S/NexDrop.exe" || { echo "The base zip has no NexDrop-windows-x64/NexDrop.exe" >&2; exit 1; }

# Window (replace completely so files removed from the app don't linger)
rm -rf "$A"
mkdir -p "$A"
cp "$ROOT"/electron/*.js "$ROOT/electron/package.json" "$A/"
cp "$ROOT/desktop/auth-catalog.json" "$A/"
cp -r "$ROOT/electron/renderer" "$A/renderer"
rm -f "$S/UI-COMPARISON.md"

# Python side and engine
cp "$ROOT"/desktop/*.py "$ROOT/desktop/auth-catalog.json" "$S/desktop/"
cp "$ROOT/engine/bridge.py" "$ROOT/engine/user-config.yaml" "$S/engine/"
rm -rf "$S/engine/vendor"
cp -r "$ROOT/engine/vendor" "$S/engine/vendor"
# Telegram sign-in and account downloads (Telethon, pinned)
sh "$ROOT/windows/telegram-libs.sh" "$S/engine/thirdparty"

# Browser extension (load unpacked; see browser-extension/README.txt)
rm -rf "$S/browser-extension"
cp -r "$ROOT/browser-extension" "$S/browser-extension"

# Docs and licences
cp "$ROOT/windows/ELECTRON-README.txt" "$S/README.txt"
cp "$ROOT/CHANGELOG.md" "$S/CHANGELOG.md"
mkdir -p "$S/licenses"
cp "$ROOT"/electron/renderer/fonts/*-OFL.txt "$S/licenses/"
cp "$ROOT/electron/LICENSE-CAIRN.txt" "$S/licenses/Cairn-MIT.txt"

# Clean state: no caches, no user data
find "$S" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$S/engine/appdata" "$S/downloads" "$S/logs"
mkdir -p "$S/engine/appdata" "$S/downloads/nexdrop"

python3 - "$WORK" "$OUT" <<'PY'
import hashlib, os, sys, zipfile
from pathlib import Path
work, out = Path(sys.argv[1]), Path(sys.argv[2])
os.chdir(work)
root = Path("NexDrop-windows-x64")
lines = []
for dirpath, dirnames, names in os.walk(root):
    dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
    for name in sorted(names):
        if name == "SHA256SUMS.txt":
            continue
        path = Path(dirpath) / name
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {path.relative_to(root).as_posix()}")
(root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
dest = out / "NexDrop-windows-x64.zip"
count = 0
# Electron's files are dated 1980-01-01; allow that instead of failing.
with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as zf:
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        if not names and not dirnames:
            zf.write(dirpath, dirpath + "/")
        for name in sorted(names):
            zf.write(Path(dirpath) / name)
            count += 1
with zipfile.ZipFile(dest) as zf:
    bad = zf.testzip()
    if bad:
        sys.exit(f"Corrupt member in the zip: {bad}")
print(f"NexDrop-windows-x64.zip: {count} files, {dest.stat().st_size:,} bytes")
PY

VERSION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" "$ROOT/electron/package.json")"
git -C "$ROOT" archive --format=zip --prefix="NexDrop-$VERSION/" -o "$OUT/NexDrop-source.zip" HEAD
( cd "$OUT" && sha256sum NexDrop-windows-x64.zip NexDrop-source.zip > SHA256SUMS.txt && cat SHA256SUMS.txt )
rm -rf "$WORK"
