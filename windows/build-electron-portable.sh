#!/bin/sh
# Build a portable Windows x64 folder: Electron shell + Cairn UI + patched engine.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
VER="${ELECTRON_VERSION:-42.2.0}"
CACHE="$ROOT/release/cache"
ZIP="$CACHE/electron-v${VER}-win32-x64.zip"
OUT="${NEXDROP_OUT:-$ROOT/artifacts}"
STAGE="$OUT/NexDrop-windows-x64"
URL="https://github.com/electron/electron/releases/download/v${VER}/electron-v${VER}-win32-x64.zip"

mkdir -p "$CACHE"
if [ ! -f "$ZIP" ]; then
  echo "Downloading Electron ${VER} win32-x64"
  curl -fL --retry 3 -o "$ZIP.partial" "$URL"
  mv "$ZIP.partial" "$ZIP"
fi

rm -rf "$STAGE"
mkdir -p "$STAGE"
unzip -q "$ZIP" -d "$STAGE"
mv "$STAGE/electron.exe" "$STAGE/NexDrop.exe"
rm -f "$STAGE/resources/default_app.asar"

APP="$STAGE/resources/app"
mkdir -p "$APP/renderer"
cp "$ROOT"/electron/*.js "$ROOT/electron/package.json" "$APP/"
cp "$ROOT/desktop/auth-catalog.json" "$APP/auth-catalog.json"
cp -a "$ROOT/electron/renderer/." "$APP/renderer/"
rm -f "$APP/renderer/preview-api.js" "$APP/renderer/finish-text.js"

mkdir -p "$STAGE/engine/appdata" "$STAGE/desktop" "$STAGE/downloads/nexdrop" "$STAGE/licenses"
cp "$ROOT/engine/bridge.py" "$STAGE/engine/bridge.py"
cp "$ROOT/engine/user-config.yaml" "$STAGE/engine/user-config.yaml"
cp "$ROOT"/desktop/*.py "$ROOT"/desktop/*.json "$STAGE/desktop/"
mkdir -p "$STAGE/docs"
cp -r "$ROOT/docs/site-assessments" "$STAGE/docs/site-assessments"
python3 - <<PY
import os, shutil
from pathlib import Path
src = Path("$ROOT/engine/vendor")
dst = Path("$STAGE/engine/vendor")
for dirpath, dirnames, names in os.walk(src):
    dirnames[:] = [name for name in dirnames if name != "__pycache__"]
    target = dst / Path(dirpath).relative_to(src)
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name.endswith(".pyc"):
            continue
        shutil.copy2(Path(dirpath) / name, target / name)
# NEXDROP fixes to the bundled engine (see engine/vendor-patches/README.md)
patches = Path("$ROOT/engine/vendor-patches")
for path in patches.rglob("*.py"):
    dest = dst / path.relative_to(patches)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
PY

# Telegram sign-in and account downloads (Telethon, pinned)
sh "$ROOT/windows/telegram-libs.sh" "$STAGE/engine/thirdparty"

if [ -d "$ROOT/release/NexDrop/runtime/python" ]; then
  mkdir -p "$STAGE/runtime"
  cp -a "$ROOT/release/NexDrop/runtime/python" "$STAGE/runtime/python"
fi
if [ -d "$ROOT/release/NexDrop/runtime/flaresolverr" ]; then
  mkdir -p "$STAGE/runtime"
  cp -a "$ROOT/release/NexDrop/runtime/flaresolverr" "$STAGE/runtime/flaresolverr"
fi
if [ -d "$ROOT/release/NexDrop/runtime/ffmpeg" ]; then
  mkdir -p "$STAGE/runtime"
  cp -a "$ROOT/release/NexDrop/runtime/ffmpeg" "$STAGE/runtime/ffmpeg"
fi

cp "$ROOT/windows/NOTICE.txt" "$STAGE/NOTICE.txt" 2>/dev/null || true
if [ -f "$ROOT/release/NexDrop/licenses/GPL-3.0.txt" ]; then
  cp "$ROOT/release/NexDrop/licenses/GPL-3.0.txt" "$STAGE/licenses/GPL-3.0.txt"
fi
cp "$ROOT/electron/LICENSE-CAIRN.txt" "$STAGE/licenses/Cairn-MIT.txt"
cp "$ROOT/windows/ELECTRON-README.txt" "$STAGE/README.txt"
cp "$ROOT/CHANGELOG.md" "$STAGE/CHANGELOG.md" 2>/dev/null || true
cp "$ROOT"/electron/renderer/fonts/*-OFL.txt "$STAGE/licenses/" 2>/dev/null || true

python3 - <<PY
import hashlib, os
from pathlib import Path
root = Path("$STAGE")
lines = []
for dirpath, dirnames, names in os.walk(root):
    dirnames[:] = [name for name in dirnames if name != "__pycache__"]
    for name in sorted(names):
        if name == "SHA256SUMS.txt":
            continue
        path = Path(dirpath) / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
(root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
manifest = root.parent / "NexDrop-windows-x64-MANIFEST.txt"
manifest.write_text(
    "NexDrop Windows x64 portable folder\n"
    f"electron {os.environ.get('ELECTRON_VERSION', '$VER')}\n"
    "interface NEXDROP 2.1\n"
    "engine cyberdrop-dl 10.10.0 patched\n"
    f"files {len(lines)}\n",
    encoding="utf-8",
)
print(f"files {len(lines)}")
PY

(
  cd "$OUT"
  rm -f NexDrop-windows-x64.zip
  python3 - <<PY
import os, zipfile
from pathlib import Path
root = Path("$STAGE")
dest = Path("$OUT/NexDrop-windows-x64.zip")
level = int(os.environ.get("NEXDROP_ZIP_LEVEL", "6"))
count = 0
with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level) as zf:
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for name in names:
            path = Path(dirpath) / name
            zf.write(path, Path("NexDrop-windows-x64") / path.relative_to(root))
            count += 1
print(f"zip files {count} bytes {dest.stat().st_size}")
PY
  sha256sum NexDrop-windows-x64.zip > NexDrop-windows-x64-SHA256SUMS.txt
)
echo "Portable folder: $STAGE"
