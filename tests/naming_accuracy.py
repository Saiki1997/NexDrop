"""Measure smart naming V1 (2.5) against V2 on the labelled cases in naming_dataset.py.

    python tests/naming_accuracy.py            # table + per-case misses
    python tests/naming_accuracy.py --json     # machine-readable summary
    python tests/naming_accuracy.py --holdout  # the held-out cases instead (--holdout2: the second set)

Metrics, per engine:
  correct      the chosen title is the labelled one (case and punctuation ignored)
  wrong assoc  the chosen title is labelled as wrong for that file (another post, an advert, a site name)
  fallback     the engine kept the original name because nothing was trustworthy
  collisions   two files of one folder ending with the same name (numbering on, then off)
  overhead     microseconds per naming decision on this machine
  resume       the same names come back after a restart that reloads the job's manifest
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "vendor"))
sys.path.insert(0, str(ROOT / "tests"))

from bs4 import BeautifulSoup  # noqa: E402

from cyberdrop_dl import smart_naming as v2  # noqa: E402
from cyberdrop_dl import source_order  # noqa: E402
import naming_dataset  # noqa: E402
import naming_holdout  # noqa: E402
import naming_holdout2  # noqa: E402
from naming_dataset import ORIGINAL  # noqa: E402

CASES = naming_holdout2.CASES if "--holdout2" in sys.argv else naming_holdout.CASES if "--holdout" in sys.argv else naming_dataset.CASES

v1 = importlib.import_module("naming_v1.smart_naming_v1")


def norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", text or "").casefold()).strip()


def _soup(meta: dict[str, str]) -> BeautifulSoup:
    tags = []
    for key, value in meta.items():
        if key == "html_title":
            continue
        attr = "property" if key.startswith("og:") else "name"
        tags.append(f'<meta {attr}="{key}" content="{value}">')
    title = f"<title>{meta['html_title']}</title>" if meta.get("html_title") else ""
    return BeautifulSoup(f"<html><head>{title}{''.join(tags)}</head><body></body></html>", "html.parser")


def _naming_for(engine, case: dict) -> dict[str, str]:
    naming = dict(case["naming"])
    if engine is v1:
        for key in case["v2_only"]:
            naming.pop(key, None)  # V1 adapters never sent these
    return naming


def decide(engine, case: dict) -> tuple[str, str]:
    """(title, source) the engine picks for one case."""
    settings = engine.NamingSettings.from_payload({})
    page_url = "https://example.invalid/none"
    if case["page"]:
        page_url, meta = case["page"]
        if meta:
            engine.note_page(page_url, _soup(meta))
    stem, _ext = source_order.split_name(case["file"], None)
    if engine.is_multipart_archive(case["file"]):
        return case["file"], "original filename"
    title, source, _confidence = engine.choose_title(stem, _naming_for(engine, case), page_url, settings)
    return title, source


def judge(case: dict, title: str, source: str) -> dict:
    stem, _ext = source_order.split_name(case["file"], None)
    expect = case["expect"]
    if expect == "<keep>":
        ok = title == case["file"]
    elif expect == ORIGINAL:
        ok = norm(title) == norm(stem)
    else:
        accepted = expect if isinstance(expect, tuple) else (expect,)
        ok = any(norm(title) == norm(e) for e in accepted)
    wrong = any(norm(title) == norm(w) or (norm(w) and norm(title).startswith(norm(w))) for w in case["wrong"])
    fallback = source == "original filename" and expect not in (ORIGINAL, "<keep>")
    return {"ok": ok, "wrong": wrong, "fallback": fallback, "kept_original": source == "original filename"}


def accuracy(engine) -> dict:
    rows = []
    for case in CASES:
        title, source = decide(engine, case)
        rows.append({"id": case["id"], "category": case["category"], "title": title, "source": source,
                     "expect": case["expect"], **judge(case, title, source)})
    total = len(rows)
    by_category: dict[str, list[int]] = {}
    for row in rows:
        hit = by_category.setdefault(row["category"], [0, 0])
        hit[0] += row["ok"]
        hit[1] += 1
    return {
        "cases": total,
        "correct": sum(r["ok"] for r in rows),
        "wrong_association": sum(r["wrong"] for r in rows),
        "fallback": sum(r["kept_original"] for r in rows),
        "missed_title": sum(r["fallback"] for r in rows),
        "by_category": by_category,
        "rows": rows,
    }


def overhead(engine, repeat: int = 20) -> float:
    settings = engine.NamingSettings.from_payload({})
    work = [(source_order.split_name(c["file"], None)[0], _naming_for(engine, c),
             c["page"][0] if c["page"] else "https://example.invalid/none") for c in CASES]
    start = time.perf_counter()
    for _ in range(repeat):
        for stem, naming, page_url in work:
            engine.choose_title(stem, naming, page_url, settings)
    return (time.perf_counter() - start) / (repeat * len(work)) * 1e6


def collisions_and_resume(engine) -> dict:
    """Name every case as one job (each case's folder = its category), then again after a restart."""
    out = {}
    for mode in ("suffix", "off"):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.jsonl"
            settings = engine.NamingSettings.from_payload({"numbering": mode})
            first = _name_job(engine, settings, manifest)
            second = _name_job(engine, settings, manifest)
            names: dict[str, set[str]] = {}
            clashes = 0
            for (folder, name) in first.values():
                bucket = names.setdefault(folder, set())
                if name.casefold() in bucket:
                    clashes += 1
                bucket.add(name.casefold())
            same = sum(first[k] == second[k] for k in first)
            out[mode] = {"collisions": clashes, "resume_same": same, "files": len(first)}
    return out


def _name_job(engine, settings, manifest: Path) -> dict[str, tuple[str, str]]:
    source_order.reset(str(manifest), "accuracy-job")
    token = engine.SETTINGS.set(settings)
    mode = source_order.NUMBER_MODE.set(settings.numbering)
    result = {}
    try:
        for index, case in enumerate(CASES):
            url = f"https://cdn.example.invalid/{index}/{case['file']}"
            page_url = case["page"][0] if case["page"] else "https://example.invalid/none"
            if case["page"] and case["page"][1]:
                engine.note_page(page_url, _soup(case["page"][1]))
            decision = engine.name_file(url, case["file"], None, _naming_for(engine, case), page_url,
                                        Path("C:/Downloads") / case["category"], lambda n: n)
            row = {"url": url, "sequence": decision.number, "name": decision.filename, "title": decision.title,
                   "source": decision.source, "confidence": decision.confidence, "strategy": decision.strategy,
                   "naming": settings.fingerprint(), "job": "accuracy-job"}  # what the app writes per file
            with manifest.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
            result[url] = (case["category"], decision.filename)
    finally:
        engine.SETTINGS.reset(token)
        source_order.NUMBER_MODE.reset(mode)
    return result


def main() -> int:
    report = {}
    for label, engine in (("V1 (2.5)", v1), ("V2", v2)):
        acc = accuracy(engine)
        acc["overhead_us"] = round(overhead(engine), 1)
        acc["jobs"] = collisions_and_resume(engine)
        report[label] = acc
    if "--json" in sys.argv:
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in report.items()}, indent=2))
        return 0
    cases = len(CASES)
    print(f"{cases} labelled cases\n")
    print(f"{'':28}{'V1 (2.5)':>14}{'V2':>14}")
    for key, label in (("correct", "correct title"), ("wrong_association", "wrong association"),
                       ("fallback", "kept original name"), ("missed_title", "missed a wanted title")):
        a, b = report["V1 (2.5)"][key], report["V2"][key]
        print(f"{label:28}{a:>7} {a / cases:>5.0%}{b:>8} {b / cases:>5.0%}")
    print(f"{'overhead per file (µs)':28}{report['V1 (2.5)']['overhead_us']:>14}{report['V2']['overhead_us']:>14}")
    for mode in ("suffix", "off"):
        a, b = report["V1 (2.5)"]["jobs"][mode], report["V2"]["jobs"][mode]
        print(f"{'collisions, numbering ' + mode:28}{a['collisions']:>14}{b['collisions']:>14}")
        print(f"{'same names after resume, ' + mode:28}{a['resume_same']:>9}/{a['files']:<4}{b['resume_same']:>9}/{b['files']:<4}")
    print("\nBy category (correct / cases):")
    cats = report["V2"]["by_category"]
    for cat in cats:
        a, b = report["V1 (2.5)"]["by_category"][cat], cats[cat]
        print(f"  {cat:24}{a[0]:>4}/{a[1]:<4}{b[0]:>8}/{b[1]:<4}")
    print("\nV2 misses:")
    for row in report["V2"]["rows"]:
        if not row["ok"] or row["wrong"]:
            print(f"  {row['id']:10} got {row['title']!r} ({row['source']}), wanted {row['expect']!r}"
                  f"{'  WRONG ASSOCIATION' if row['wrong'] else ''}")
    print("\nV1 misses:")
    for row in report["V1 (2.5)"]["rows"]:
        if not row["ok"] or row["wrong"]:
            print(f"  {row['id']:10} got {row['title']!r} ({row['source']}), wanted {row['expect']!r}"
                  f"{'  WRONG ASSOCIATION' if row['wrong'] else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
