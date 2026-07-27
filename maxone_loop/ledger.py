"""Transactional SQLite ledger and the OS-level device lock.

Two invariants matter more than the rest:

* ``consumed_recordings`` has a primary key on ``(experiment_id, folder_path)``.
  Duplicate filesystem events, a re-exported workbook, and a restart all funnel
  through the same INSERT, so a second stimulation for one recording is a
  constraint violation rather than a race that has to be reasoned about.
* The device lock is an ``flock`` held for the process lifetime. Advisory locks
  are released by the kernel when the holder dies, so a crashed run does not
  leave the device permanently unavailable.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from . import ANALYSIS_VERSION, __version__
from .state import State

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    experiment_id       TEXT PRIMARY KEY,
    created_utc         TEXT NOT NULL,
    mode                TEXT NOT NULL,
    config_fingerprint  TEXT NOT NULL,
    source_hashes       TEXT NOT NULL,
    protocol_id         TEXT,
    git_commit          TEXT,
    software_version    TEXT NOT NULL,
    analysis_version    INTEGER NOT NULL,
    maxlab_live_version TEXT,
    api_version         TEXT
);

CREATE TABLE IF NOT EXISTS transitions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id       TEXT NOT NULL,
    utc                 TEXT NOT NULL,
    cycle_index         INTEGER,
    previous_state      TEXT NOT NULL,
    next_state          TEXT NOT NULL,
    workbook_path       TEXT,
    workbook_sha256     TEXT,
    folder_path         TEXT,
    applied_amplitude_mV TEXT,
    protocol_id         TEXT,
    metrics_json        TEXT,
    qc_json             TEXT,
    decision            TEXT,
    reason_code         TEXT
);

CREATE TABLE IF NOT EXISTS consumed_recordings (
    experiment_id       TEXT NOT NULL,
    folder_path         TEXT NOT NULL,
    workbook_sha256     TEXT NOT NULL,
    workbook_filename   TEXT NOT NULL,
    cycle_index         INTEGER NOT NULL,
    consumed_utc        TEXT NOT NULL,
    PRIMARY KEY (experiment_id, folder_path)
);

CREATE TABLE IF NOT EXISTS stimulations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id       TEXT NOT NULL,
    cycle_index         INTEGER NOT NULL,
    utc                 TEXT NOT NULL,
    amplitude_mV        TEXT NOT NULL,
    protocol_id         TEXT NOT NULL,
    status              TEXT NOT NULL,
    receipt_json        TEXT
);

CREATE INDEX IF NOT EXISTS ix_transitions_run ON transitions (experiment_id, id);
"""


class LedgerError(Exception):
    pass


class RecordingAlreadyConsumed(LedgerError):
    """A recording identity was offered twice within one experiment."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


@dataclass(frozen=True)
class Transition:
    previous_state: State
    next_state: State
    cycle_index: int | None = None
    workbook_path: str | None = None
    workbook_sha256: str | None = None
    folder_path: str | None = None
    applied_amplitude_mV: Decimal | None = None
    protocol_id: str | None = None
    metrics: dict[str, Any] | None = None
    qc: list[dict[str, Any]] | None = None
    decision: str | None = None
    reason_code: str | None = None


class Ledger:
    """Append-only provenance store for one state directory."""

    def __init__(self, state_directory: str | Path) -> None:
        self.directory = Path(state_directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "ledger.sqlite3"
        self._connection = sqlite3.connect(self.path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    # -- run header ---------------------------------------------------------

    def start_run(
        self,
        *,
        experiment_id: str,
        mode: str,
        config_fingerprint: str,
        source_hashes: dict[str, str],
        protocol_id: str | None,
        maxlab_live_version: str | None,
        api_version: str | None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs (experiment_id, created_utc, mode, config_fingerprint,
                                  source_hashes, protocol_id, git_commit, software_version,
                                  analysis_version, maxlab_live_version, api_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id) DO NOTHING
                """,
                (
                    experiment_id,
                    _utc(),
                    mode,
                    config_fingerprint,
                    json.dumps(source_hashes, sort_keys=True),
                    protocol_id,
                    _git_commit(),
                    __version__,
                    ANALYSIS_VERSION,
                    maxlab_live_version,
                    api_version,
                ),
            )

    # -- transitions --------------------------------------------------------

    def record_transition(self, experiment_id: str, transition: Transition) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO transitions (experiment_id, utc, cycle_index, previous_state,
                                         next_state, workbook_path, workbook_sha256,
                                         folder_path, applied_amplitude_mV, protocol_id,
                                         metrics_json, qc_json, decision, reason_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    _utc(),
                    transition.cycle_index,
                    transition.previous_state.value,
                    transition.next_state.value,
                    transition.workbook_path,
                    transition.workbook_sha256,
                    transition.folder_path,
                    None
                    if transition.applied_amplitude_mV is None
                    else str(transition.applied_amplitude_mV),
                    transition.protocol_id,
                    None if transition.metrics is None else json.dumps(transition.metrics, default=str),
                    None if transition.qc is None else json.dumps(transition.qc, default=str),
                    transition.decision,
                    transition.reason_code,
                ),
            )

    def last_state(self, experiment_id: str) -> State:
        row = self._connection.execute(
            "SELECT next_state FROM transitions WHERE experiment_id = ? ORDER BY id DESC LIMIT 1",
            (experiment_id,),
        ).fetchone()
        return State(row["next_state"]) if row else State.IDLE

    def transitions(self, experiment_id: str) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM transitions WHERE experiment_id = ? ORDER BY id", (experiment_id,)
            )
        )

    # -- recording idempotency ---------------------------------------------

    def consumed_folder_paths(self, experiment_id: str) -> frozenset[str]:
        rows = self._connection.execute(
            "SELECT folder_path FROM consumed_recordings WHERE experiment_id = ?",
            (experiment_id,),
        )
        return frozenset(row["folder_path"] for row in rows)

    def consume_recording(
        self,
        *,
        experiment_id: str,
        folder_path: str,
        workbook_sha256: str,
        workbook_filename: str,
        cycle_index: int,
    ) -> None:
        """Claim a recording identity. Raises if it was already claimed.

        This is the single choke point that makes duplicate filesystem events
        and re-exported workbooks harmless.
        """
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO consumed_recordings (experiment_id, folder_path,
                                                     workbook_sha256, workbook_filename,
                                                     cycle_index, consumed_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        experiment_id,
                        folder_path,
                        workbook_sha256,
                        workbook_filename,
                        cycle_index,
                        _utc(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RecordingAlreadyConsumed(
                f"recording {folder_path} was already consumed by experiment {experiment_id}"
            ) from exc

    # -- stimulations -------------------------------------------------------

    def record_stimulation(
        self,
        *,
        experiment_id: str,
        cycle_index: int,
        amplitude_mV: Decimal,
        protocol_id: str,
        status: str,
        receipt: dict[str, Any] | None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO stimulations (experiment_id, cycle_index, utc, amplitude_mV,
                                          protocol_id, status, receipt_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    cycle_index,
                    _utc(),
                    str(amplitude_mV),
                    protocol_id,
                    status,
                    None if receipt is None else json.dumps(receipt, default=str),
                ),
            )

    def cumulative_stimulations(self, experiment_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS n FROM stimulations
            WHERE experiment_id = ? AND status != 'DRY_RUN'
            """,
            (experiment_id,),
        ).fetchone()
        return int(row["n"])

    def stimulations(self, experiment_id: str) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM stimulations WHERE experiment_id = ? ORDER BY id", (experiment_id,)
            )
        )


class DeviceLock:
    """Exclusive advisory lock over the device. Only one owner at a time."""

    def __init__(self, state_directory: str | Path, name: str = "device.lock") -> None:
        directory = Path(state_directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / name
        self._handle = None

    def acquire(self) -> None:
        handle = self.path.open("w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LedgerError(
                f"device lock {self.path} is held by another process; only one process "
                "may own device control"
            ) from exc
        handle.write(str(_utc()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> DeviceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
