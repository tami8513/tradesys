"""Cross-process safe JSON state files.

On the server the dashboard and the scheduler are separate processes sharing one
disk. A store that loads its file once and later rewrites the whole thing will
silently erase whatever the other process wrote in between — positions vanish,
cash resets, lessons disappear. Every read-modify-write goes through `locked()`,
and readers re-read when the file changed.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def locked(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive advisory lock on `<path>.lock`, held for the block."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _acquire(handle)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not lock {path} within {timeout}s")
                time.sleep(0.05)
        yield
    finally:
        try:
            _release(handle)
        finally:
            handle.close()


if os.name == "nt":  # pragma: no cover - exercised on Windows dev machines
    import msvcrt

    def _acquire(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _release(handle) -> None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _acquire(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def signature(path: Path) -> tuple[int, int, int] | None:
    """Changes on every write, even two writes within one timestamp tick.

    Modification time alone is not enough: on Windows two quick writes can share
    a timestamp, and a reader would miss the second. Every atomic write creates a
    new file, so its file id changes too.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)
