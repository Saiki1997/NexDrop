"""Compare the bundled engine with a newer Cyberdrop-DL release. Read-only.

    python tools/engine-upstream-report.py                 # newest release on PyPI
    python tools/engine-upstream-report.py --to 10.13.0 --out report.md

Downloads the stock wheels of the bundled version (10.10.0) and the target version from PyPI
and sorts every file of the engine into:

- changed upstream only: NEXDROP never touched it, so the new file can be taken as it is;
- changed upstream and by NEXDROP: needs a careful merge, NEXDROP's change must be kept;
- changed by NEXDROP only: nothing to do;
- new upstream (often new site crawlers) and removed upstream.

It never writes into engine/: the patched engine stays exactly as it is. Only the report is written,
to the terminal or to --out.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "engine" / "vendor"
PATCHES = ROOT / "engine" / "vendor-patches"
PACKAGE = "cyberdrop-dl-patched"
BASE_VERSION = "10.10.0"
PREFIX = "cyberdrop_dl/"


def pypi(name: str) -> dict:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=30) as response:
        return json.load(response)


def wheel_files(meta: dict, version: str) -> dict[str, bytes]:
    files = meta.get("releases", {}).get(version) or []
    wheel = next((f for f in files if f.get("filename", "").endswith(".whl")), None)
    if not wheel:
        raise SystemExit(f"{PACKAGE} {version} has no wheel on PyPI.")
    with urllib.request.urlopen(wheel["url"], timeout=120) as response:
        data = response.read()
    expected = (wheel.get("digests") or {}).get("sha256")
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit(f"The {version} wheel does not match PyPI's SHA-256.")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()
                if name.startswith(PREFIX) and not name.endswith("/") and "__pycache__" not in name}


def local_files() -> dict[str, bytes]:
    folder = VENDOR / "cyberdrop_dl"
    return {f"{PREFIX}{path.relative_to(folder).as_posix()}": path.read_bytes()
            for path in folder.rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def _norm(data: bytes | None) -> bytes | None:
    return None if data is None else data.replace(b"\r\n", b"\n")


def classify(base: dict[str, bytes], target: dict[str, bytes], local: dict[str, bytes]) -> dict[str, list[str]]:
    """Pure comparison so it can be tested without the network."""
    groups: dict[str, list[str]] = {"both": [], "upstream": [], "nexdrop": [], "added": [], "removed": [], "nexdrop_new": []}
    for name in sorted(set(base) | set(target) | set(local)):
        b, t, mine = _norm(base.get(name)), _norm(target.get(name)), _norm(local.get(name))
        if b is None and t is not None:
            groups["added"].append(name)
        elif b is not None and t is None:
            groups["removed"].append(name)
        elif b is None and t is None:
            groups["nexdrop_new"].append(name)
        else:
            upstream_changed = b != t
            mine_changed = mine is not None and mine != b
            if upstream_changed and mine_changed:
                groups["both"].append(name)
            elif upstream_changed:
                groups["upstream"].append(name)
            elif mine_changed:
                groups["nexdrop"].append(name)
    return groups


def changed_lines(old: bytes, new: bytes) -> int:
    a = old.decode("utf-8", "replace").splitlines()
    b = new.decode("utf-8", "replace").splitlines()
    return sum(1 for line in difflib.unified_diff(a, b, lineterm="", n=0) if line[:1] in "+-" and line[:3] not in ("+++", "---"))


def report(target_version: str) -> str:
    meta = pypi(PACKAGE)
    target_version = target_version or meta["info"]["version"]
    base = wheel_files(meta, BASE_VERSION)
    target = wheel_files(meta, target_version) if target_version != BASE_VERSION else base
    local = local_files()
    groups = classify(base, target, local)
    patched = {f"{PREFIX}{p.relative_to(PATCHES / 'cyberdrop_dl').as_posix()}" for p in (PATCHES / "cyberdrop_dl").rglob("*.py")}

    def size(name: str) -> str:
        return f"{changed_lines(base[name], target[name])} lines changed upstream" if name in base and name in target else ""

    out = [f"# Cyberdrop-DL {BASE_VERSION} → {target_version}", "",
           "Read-only report. Nothing in engine/ was changed.", ""]
    sections = [
        ("both", "Changed upstream and by NEXDROP: merge by hand, keep the NEXDROP patch"),
        ("upstream", "Changed upstream only: can be taken as they are"),
        ("added", "New upstream (often new sites)"),
        ("removed", "Removed upstream"),
        ("nexdrop", "Changed by NEXDROP only: nothing to do"),
        ("nexdrop_new", "Files only NEXDROP has"),
    ]
    for key, title in sections:
        names = groups[key]
        out.append(f"## {title} ({len(names)})")
        out.append("")
        for name in names:
            notes = [size(name) if key in ("both", "upstream") else ""]
            if name in patched:
                notes.append("recorded in engine/vendor-patches")
            text = ", ".join(n for n in notes if n)
            out.append(f"- `{name}`" + (f" ({text})" if text else ""))
        if not names:
            out.append("- none")
        out.append("")
    crawlers = [n for n in groups["added"] if n.startswith(f"{PREFIX}crawlers/")]
    out.append(f"Summary: {len(groups['upstream'])} files to take, {len(groups['both'])} to merge, "
               f"{len(crawlers)} new crawler files, {len(groups['removed'])} removed.")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", default="", help="Cyberdrop-DL version to compare with (default: newest on PyPI)")
    parser.add_argument("--out", default="", help="write the report here instead of printing it")
    args = parser.parse_args()
    text = report(args.to)
    if args.out:
        out = Path(args.out).resolve()
        if out == VENDOR or VENDOR in out.parents or (ROOT / "engine") in out.parents:
            raise SystemExit("The report is never written inside engine/.")
        out.write_text(text, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
