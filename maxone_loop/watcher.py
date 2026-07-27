"""Stable-file detection in the metrics watch directory.

MaxLab Live writes a workbook incrementally, so a file that merely exists is
not a file that is finished. A candidate is accepted only after its size and
modification time have been unchanged for the configured interval.

The clock is injectable so the tests do not have to sleep.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .config.models import MetricsWatcherSection
from .config.loader import sha256_file


@dataclass(frozen=True)
class Candidate:
    path: Path
    size: int
    mtime: float


def _is_ignored(name: str, prefixes: Iterable[str]) -> bool:
    # "~$" is Excel's lock file; "." and "_" cover hidden and partial writes.
    return any(name.startswith(prefix) for prefix in prefixes)


def list_candidates(directory: Path, watcher: MetricsWatcherSection) -> list[Candidate]:
    """Workbooks in the directory that are eligible to be considered."""
    if not directory.is_dir():
        return []
    found: list[Candidate] = []
    for path in sorted(directory.glob(watcher.filename_glob)):
        if not path.is_file() or _is_ignored(path.name, watcher.ignore_filename_prefixes):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append(Candidate(path=path, size=stat.st_size, mtime=stat.st_mtime))
    return found


class MetricsWatcher:
    """Polls a directory and yields workbooks that have stopped changing."""

    def __init__(
        self,
        directory: str | Path,
        watcher: MetricsWatcherSection,
        *,
        clock: Callable[[], float] = _time.monotonic,
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        self.directory = Path(directory)
        self.config = watcher
        self._clock = clock
        self._sleep = sleep
        #: path -> (size, mtime, first time seen unchanged)
        self._seen: dict[Path, tuple[int, float, float]] = {}

    def poll(self) -> list[Path]:
        """One polling pass. Returns workbooks that are now stable."""
        stable: list[Path] = []
        now = self._clock()
        current = {c.path: c for c in list_candidates(self.directory, self.config)}

        for path in list(self._seen):
            if path not in current:
                del self._seen[path]

        for path, candidate in current.items():
            previous = self._seen.get(path)
            if previous is None or previous[0] != candidate.size or previous[1] != candidate.mtime:
                # Still being written, or seen for the first time.
                self._seen[path] = (candidate.size, candidate.mtime, now)
                continue
            if now - previous[2] >= self.config.stable_for_seconds:
                stable.append(path)
        return stable

    def wait_for_workbook(self, *, deadline_seconds: float | None = None) -> Path | None:
        """Block until one workbook is stable, or the timeout expires.

        Returns ``None`` on timeout; the caller decides whether that is a fault
        or a normal completion.
        """
        limit = (
            self.config.metrics_timeout_minutes * 60.0
            if deadline_seconds is None
            else deadline_seconds
        )
        started = self._clock()
        while True:
            stable = self.poll()
            if stable:
                return stable[0]
            if self._clock() - started >= limit:
                return None
            self._sleep(self.config.poll_interval_seconds)

    @staticmethod
    def digest(path: Path) -> str:
        return sha256_file(path)
