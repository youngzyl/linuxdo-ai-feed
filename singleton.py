"""Cross-process exclusivity for cycles.

The manual CLI (`run.py cycle`), the service scheduler and a cron watchdog can all be
alive at once. Two concurrent cycles would interleave their save() calls and lose
updates, so every cycle takes a non-blocking file lock first.
"""
from __future__ import annotations

import fcntl
from pathlib import Path


class CycleLock:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False
