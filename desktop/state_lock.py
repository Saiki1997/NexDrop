"""A lock shared by every NexDrop process, for files that are read, changed and written back.

With several jobs at once (3.1) two sessions can finish together and both update
completed-urls.json or runs.json; without a lock the second write would drop the first one's
links. The lock is an OS file lock on `<appdata>/state.lock` (msvcrt on Windows, fcntl elsewhere),
so it is released by the OS even when a process is killed.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None
    import fcntl


@contextlib.contextmanager
def shared_state(folder: Path, timeout: float = 30.0):
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / "state.lock", "a+b")  # noqa: SIM115
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while True:
            try:
                if msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() > deadline:
                    break  # better a rare lost update than a session that never finishes
                time.sleep(0.05)
        yield locked
    finally:
        if locked:
            try:
                if msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


class RunningMarker:
    """`<appdata>/running/<slot>.pid` while a session runs, so database upkeep that needs the
    engine database to itself (VACUUM) only happens when no other job is running."""

    def __init__(self, folder: Path, slot: int) -> None:
        self.folder = folder / "running"
        self.path = self.folder / f"{slot}.pid"

    def __enter__(self) -> "RunningMarker":
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(os.getpid()), encoding="ascii")
        except OSError:
            pass
        return self

    def __exit__(self, *_) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def others(self) -> list[int]:
        """Process ids of other sessions that are still alive."""
        alive = []
        for path in self.folder.glob("*.pid"):
            if path == self.path:
                continue
            try:
                pid = int(path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                continue
            if _alive(pid):
                alive.append(pid)
            else:
                with contextlib.suppress(OSError):
                    path.unlink()  # left by a session that was killed
        return alive


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
