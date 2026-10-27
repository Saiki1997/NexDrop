"""Large-queue benchmark (3.1): real downloads from a local server, measured, nothing simulated.

    python tests/bench_large.py [--root <a NexDrop checkout>] [--files 100,1000,10000] [--json out.json]

Each workload is N direct .jpg files of 64 KB from tests/local_server.py on 127.0.0.1, downloaded by
the real desktop session (desktop/cairn_session.py with the engine), as the app runs them: one
run reads at most 200 links, so 1,000 files are 5 runs and 10,000 files are 50 runs, one after
another like the queue. Every saved file is compared byte for byte with what the server sent.

Measured per workload: wall time, files per second, CPU seconds of the session and engine
(getrusage of finished children), the highest combined memory (RSS) of the session and its
children sampled every 0.2 s, the memory of the last run compared with the first (growth), the
events the session sent (what the window has to process), and the History database size.
A throughput run downloads 8 files of 64 MB at the end.

--root points at another checkout (for example 3.0.0) to measure it the same way. Only
127.0.0.1 is contacted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from local_server import LocalServer, content  # noqa: E402

PER_RUN = 200
SMALL = 64 * 1024
BIG = 64 * 1024 * 1024

HARNESS = """
import signal, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "desktop"))
import cairn_session
cairn_session.blocked_reason = lambda host: None  # the test server is on 127.0.0.1
signal.signal(signal.SIGTERM, cairn_session._on_signal)
raise SystemExit(cairn_session.main())
"""


def tree_rss(pid: int) -> int:
    import psutil

    try:
        proc = psutil.Process(pid)
        procs = [proc, *proc.children(recursive=True)]
    except psutil.Error:
        return 0
    total = 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except psutil.Error:
            pass
    return total


def one_run(root: Path, appdata: Path, urls: list[str], folder: Path) -> dict:
    env = {**os.environ, "NEXDROP_APPDATA": str(appdata), "PYTHONIOENCODING": "utf-8",
           "NEXDROP_FLARE_MANAGED": "1", "NEXDROP_RETRY_WAIT": "0"}
    config = {"urls": urls, "downloadsDir": str(folder), "orderMode": "parallel", "maxDownloads": 8, "perHost": 8,
              "smartNames": False, "flareSolver": False, "redownload": True, "autoRetry": False}
    proc = subprocess.Popen([sys.executable, "-c", HARNESS, str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, cwd=str(root))
    proc.stdin.write(json.dumps(config) + "\n")
    proc.stdin.flush()
    peak = 0
    done = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not done.is_set():
            peak = max(peak, tree_rss(proc.pid))
            done.wait(0.2)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    events = 0
    event_bytes = 0
    finished: dict = {}
    for line in proc.stdout:
        events += 1
        event_bytes += len(line.encode("utf-8"))
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "finished":
            finished = event
            break
    proc.stdin.close()
    proc.wait(120)
    done.set()
    sampler.join(2)
    return {"peak": peak, "events": events, "eventBytes": event_bytes, "saved": int(finished.get("saved") or 0),
            "failed": int(finished.get("failed") or 0)}


def workload(root: Path, server: LocalServer, count: int, size: int, tag: str) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"nexdrop-bench-{tag}-"))
    appdata, folder = work / "appdata", work / "downloads"
    appdata.mkdir()
    old_appdata = root / "engine" / "appdata"  # versions before 3.1 ignore NEXDROP_APPDATA
    names = [f"{tag}{i:05d}.jpg" for i in range(count)]
    urls = [server.url("ok", name, size=size) for name in names]
    old_db = old_appdata / "jobs.sqlite"
    old_size = old_db.stat().st_size if old_db.is_file() else 0  # never deleted: report what this workload added
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    runs = []
    for at in range(0, count, PER_RUN):
        run_started = time.monotonic()
        runs.append(one_run(root, appdata, urls[at:at + PER_RUN], folder))
        runs[-1]["seconds"] = time.monotonic() - run_started
    wall = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    good = bad = 0
    saved: dict[str, list[Path]] = {}
    for path in folder.rglob("*.jpg"):  # names get the run's sequence number: "w100-00007-008.jpg"
        saved.setdefault(re.sub(r"-\d+$", "", path.stem) + path.suffix, []).append(path)
    for name in names:
        hits = saved.get(name, [])
        if len(hits) == 1 and hits[0].read_bytes() == content(name, size):
            good += 1
        else:
            bad += 1
    new_db = appdata / "jobs.sqlite"
    history = new_db.stat().st_size if new_db.is_file() else (old_db.stat().st_size - old_size if old_db.is_file() else 0)
    result = {
        "files": count,
        "fileSize": size,
        "runs": len(runs),
        "wallSeconds": round(wall, 2),
        "filesPerSecond": round(count / wall, 1),
        "megabytesPerSecond": round(count * size / wall / 1e6, 1),
        "cpuSeconds": round((after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime), 2),
        "firstRunSeconds": round(runs[0]["seconds"], 1),
        "lastRunSeconds": round(runs[-1]["seconds"], 1),
        "peakMemoryMB": round(max(r["peak"] for r in runs) / 1e6, 1),
        "firstRunPeakMB": round(runs[0]["peak"] / 1e6, 1),
        "lastRunPeakMB": round(runs[-1]["peak"] / 1e6, 1),
        "events": sum(r["events"] for r in runs),
        "eventMB": round(sum(r["eventBytes"] for r in runs) / 1e6, 2),
        "reportedSaved": sum(r["saved"] for r in runs),
        "reportedFailed": sum(r["failed"] for r in runs),
        "verifiedGood": good,
        "verifiedBad": bad,
        "historyMB": round(history / 1e6, 2),
    }
    shutil.rmtree(work, ignore_errors=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(HERE.parent))
    parser.add_argument("--files", default="100,1000,10000")
    parser.add_argument("--big", type=int, default=8, help="64 MB files for the throughput run (0 skips it)")
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    out = {"root": str(root), "python": sys.version.split()[0], "cpus": os.cpu_count(), "workloads": []}
    with LocalServer() as server:
        for count in [int(x) for x in args.files.split(",") if x.strip()]:
            row = workload(root, server, count, SMALL, f"w{count}-")
            out["workloads"].append(row)
            print(json.dumps(row), flush=True)
        if args.big:
            row = workload(root, server, args.big, BIG, "big-")
            out["throughput"] = row
            print(json.dumps(row), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
