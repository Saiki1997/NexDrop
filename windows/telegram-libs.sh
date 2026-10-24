#!/bin/sh
# Put Telethon (Telegram sign-in and account downloads) into <dest>, normally
# <release>/engine/thirdparty. Pure-Python wheels plus cryptg's Windows build,
# pinned by SHA-256, so the release does not depend on what PyPI serves later.
#
#   windows/telegram-libs.sh <dest>
set -eu
DEST="$1"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fetch() { # name version file sha256 [pip options]
  name="$1"; version="$2"; file="$3"; sum="$4"; shift 4
  python3 -m pip download --quiet --no-deps --dest "$WORK" "$name==$version" "$@"
  echo "$sum  $WORK/$file" | sha256sum -c --quiet -
}

fetch telethon 1.45.0 telethon-1.45.0-py3-none-any.whl 339e24c83aedc9f12cbe389264627edc49779e4289c535aa90aafc4ce62f7972
fetch rsa 4.9.1 rsa-4.9.1-py3-none-any.whl 68635866661c6836b8d39430f97a996acbd61bfa49406748ea243539fe239762
fetch pyasn1 0.6.4 pyasn1-0.6.4-py3-none-any.whl deda9277cfd454080ec40b207fb6df82206a3a2688735233cdcd8d3d565f088b
fetch pyaes 1.6.1 pyaes-1.6.1.tar.gz 02c1b1405c38d3c370b085fb952dd8bea3fadcee6411ad99f312cc129c536d8f --no-binary :all:
# cryptg does Telegram's AES in C: several times faster downloads than pure Python.
fetch cryptg 0.6.0 cryptg-0.6.0-cp313-cp313-win_amd64.whl 2b11d6ac297a63ba413cb79c97030cfb93763f041fd85755a488e0db13699336 \
  --only-binary :all: --platform win_amd64 --python-version 3.13 --implementation cp

rm -rf "$DEST"
mkdir -p "$DEST"
python3 - "$WORK" "$DEST" <<'PY'
import sys, tarfile, zipfile
from pathlib import Path
work, dest = Path(sys.argv[1]), Path(sys.argv[2])
for wheel in sorted(work.glob("*.whl")):
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(dest)
with tarfile.open(next(work.glob("pyaes-*.tar.gz"))) as tf:
    for member in tf.getmembers():
        parts = Path(member.name).parts
        if len(parts) > 1 and parts[1] in {"pyaes", "LICENSE.txt"} and member.isfile():
            member.name = str(Path(*parts[1:]))
            if parts[1] == "LICENSE.txt":
                member.name = "pyaes-LICENSE.txt"
            tf.extract(member, dest, filter="data")
PY
find "$DEST" -name __pycache__ -prune -exec rm -rf {} +
echo "Telethon and its helpers are in $DEST"
