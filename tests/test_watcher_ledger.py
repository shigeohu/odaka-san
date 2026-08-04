"""Watcher, ledger, and state-machine tests (CLAUDE.md sections 10, 13, 14)."""

from __future__ import annotations

import os
import shutil
from decimal import Decimal

import pytest

from maxone_loop.ledger import (
    DeviceLock,
    Ledger,
    LedgerError,
    RecordingAlreadyConsumed,
    Transition,
)
from maxone_loop.state import (
    ACTIVE,
    InvalidTransition,
    State,
    can_transition,
    check_transition,
    resume_state,
)
from maxone_loop.watcher import MetricsWatcher, list_candidates


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- watcher ---------------------------------------------------------------


def test_growing_file_is_not_stable(watch_dir, runnable_config, reference_workbook):
    config = runnable_config()
    clock = FakeClock()
    watcher = MetricsWatcher(watch_dir, config.experiment.metrics_watcher, clock=clock)

    path = watch_dir / "metrics_data_20260101_000000.xlsx"
    path.write_bytes(b"partial")
    assert watcher.poll() == []

    # Still being written: size changes, so the stability timer restarts.
    clock.advance(60)
    path.write_bytes(b"partial and then some more")
    assert watcher.poll() == []

    clock.advance(60)
    assert watcher.poll() == [path]


def test_excel_lock_files_are_ignored(watch_dir, runnable_config):
    config = runnable_config()
    (watch_dir / "~$metrics_data_20260101_000000.xlsx").write_bytes(b"lock")
    (watch_dir / ".metrics_data_20260101_000001.xlsx").write_bytes(b"hidden")
    (watch_dir / "metrics_data_20260101_000002.xlsx").write_bytes(b"real")

    names = [c.path.name for c in list_candidates(watch_dir, config.experiment.metrics_watcher)]
    assert names == ["metrics_data_20260101_000002.xlsx"]


def test_non_matching_names_are_ignored(watch_dir, runnable_config):
    config = runnable_config()
    (watch_dir / "something_else.xlsx").write_bytes(b"x")
    (watch_dir / "metrics_data_20260101_000000.csv").write_bytes(b"x")

    assert list_candidates(watch_dir, config.experiment.metrics_watcher) == []


def test_timeout_returns_none(watch_dir, runnable_config):
    config = runnable_config()
    clock = FakeClock()
    watcher = MetricsWatcher(
        watch_dir,
        config.experiment.metrics_watcher,
        clock=clock,
        sleep=lambda seconds: clock.advance(seconds),
    )
    assert watcher.wait_for_workbook(deadline_seconds=30) is None


def test_duplicate_events_yield_one_path(watch_dir, runnable_config, reference_workbook):
    config = runnable_config()
    clock = FakeClock()
    watcher = MetricsWatcher(watch_dir, config.experiment.metrics_watcher, clock=clock)

    destination = watch_dir / reference_workbook.name
    shutil.copy(reference_workbook, destination)

    watcher.poll()
    clock.advance(60)
    first = watcher.poll()
    second = watcher.poll()  # the same unchanged file, polled again

    assert first == [destination]
    assert second == [destination]  # the watcher reports; the ledger deduplicates


# --- ledger ----------------------------------------------------------------


def test_recording_identity_can_only_be_consumed_once(tmp_path):
    with Ledger(tmp_path / "state") as ledger:
        ledger.consume_recording(
            experiment_id="E1",
            folder_path="/data/P1/Network/000001",
            workbook_sha256="a" * 64,
            workbook_filename="metrics_data_20260101_000000.xlsx",
            cycle_index=0,
        )
        with pytest.raises(RecordingAlreadyConsumed):
            ledger.consume_recording(
                experiment_id="E1",
                folder_path="/data/P1/Network/000001",
                # A different workbook hash: a re-export legitimately repeats
                # earlier recordings, and that must still be rejected.
                workbook_sha256="b" * 64,
                workbook_filename="metrics_data_20260101_010000.xlsx",
                cycle_index=1,
            )


def test_same_recording_in_a_different_experiment_is_allowed(tmp_path):
    with Ledger(tmp_path / "state") as ledger:
        for experiment in ("E1", "E2"):
            ledger.consume_recording(
                experiment_id=experiment,
                folder_path="/data/P1/Network/000001",
                workbook_sha256="a" * 64,
                workbook_filename="w.xlsx",
                cycle_index=0,
            )
        assert ledger.consumed_folder_paths("E1") == frozenset({"/data/P1/Network/000001"})


def test_dry_run_stimulations_do_not_count_toward_the_cumulative_limit(tmp_path):
    with Ledger(tmp_path / "state") as ledger:
        for status in ("DRY_RUN", "DRY_RUN", "DELIVERED"):
            ledger.record_stimulation(
                experiment_id="E1",
                cycle_index=0,
                amplitude_mV=Decimal("100"),
                protocol_id="p",
                status=status,
                receipt=None,
            )
        assert ledger.cumulative_stimulations("E1") == 1


def test_transitions_are_persisted_in_order(tmp_path):
    with Ledger(tmp_path / "state") as ledger:
        ledger.record_transition(
            "E1", Transition(previous_state=State.IDLE, next_state=State.PREFLIGHT)
        )
        ledger.record_transition(
            "E1",
            Transition(
                previous_state=State.PREFLIGHT,
                next_state=State.BASELINE_RECORDING,
                cycle_index=0,
                applied_amplitude_mV=Decimal("0"),
            ),
        )
        rows = ledger.transitions("E1")

    assert [r["next_state"] for r in rows] == ["PREFLIGHT", "BASELINE_RECORDING"]
    assert rows[1]["applied_amplitude_mV"] == "0"


def test_last_state_survives_reopen(tmp_path):
    directory = tmp_path / "state"
    with Ledger(directory) as ledger:
        ledger.record_transition(
            "E1", Transition(previous_state=State.IDLE, next_state=State.STIMULATING)
        )
    with Ledger(directory) as reopened:
        assert reopened.last_state("E1") is State.STIMULATING


# --- device lock -----------------------------------------------------------


def test_only_one_process_may_hold_the_device_lock(tmp_path):
    first = DeviceLock(tmp_path / "state")
    first.acquire()
    try:
        second = DeviceLock(tmp_path / "state")
        with pytest.raises(LedgerError):
            second.acquire()
    finally:
        first.release()

    # Released, so it can be taken again.
    third = DeviceLock(tmp_path / "state")
    third.acquire()
    third.release()


# --- state machine ---------------------------------------------------------


def test_fault_is_reachable_from_every_active_state():
    for state in ACTIVE:
        assert can_transition(state, State.FAULT)


def test_fault_and_recovery_are_never_left_automatically():
    for terminal in (State.FAULT, State.RECOVERY_REQUIRED, State.COMPLETE):
        assert resume_state(terminal) is terminal
        for target in State:
            assert not can_transition(terminal, target)


@pytest.mark.parametrize("state", sorted(ACTIVE, key=lambda s: s.value))
def test_restart_from_any_active_state_requires_recovery(state):
    assert resume_state(state) is State.RECOVERY_REQUIRED


def test_illegal_transition_raises():
    with pytest.raises(InvalidTransition):
        check_transition(State.IDLE, State.STIMULATING)


def test_baseline_cannot_reach_stimulating_directly():
    """Cycle 0 must pass through the metrics wait before anything is applied."""
    assert not can_transition(State.BASELINE_RECORDING, State.STIMULATING)


# --- exports arrive in their own subfolder ---------------------------------


def test_workbook_in_an_export_subfolder_is_found(watch_dir, runnable_config, reference_workbook):
    """MaxLab Live saves each export into a folder named with the export time."""
    config = runnable_config()
    clock = FakeClock()
    watcher = MetricsWatcher(watch_dir, config.experiment.metrics_watcher, clock=clock)

    export = watch_dir / "20260409_150425"
    export.mkdir()
    destination = export / reference_workbook.name
    shutil.copy(reference_workbook, destination)

    watcher.poll()
    clock.advance(60)
    assert watcher.poll() == [destination]


def test_non_recursive_watching_ignores_subfolders(watch_dir, runnable_config, reference_workbook):
    config = runnable_config(**{"experiment.metrics_watcher.recursive": False})
    export = watch_dir / "20260409_150425"
    export.mkdir()
    shutil.copy(reference_workbook, export / reference_workbook.name)

    assert list_candidates(watch_dir, config.experiment.metrics_watcher) == []


def test_hidden_export_folder_is_ignored(watch_dir, runnable_config, reference_workbook):
    """An export still being written can sit under a temporary directory."""
    config = runnable_config()
    for folder in (".in_progress", "~$tmp", "_partial"):
        staging = watch_dir / folder
        staging.mkdir()
        shutil.copy(reference_workbook, staging / reference_workbook.name)

    assert list_candidates(watch_dir, config.experiment.metrics_watcher) == []


def test_several_exports_are_all_discovered(watch_dir, runnable_config, reference_workbook):
    config = runnable_config()
    clock = FakeClock()
    watcher = MetricsWatcher(watch_dir, config.experiment.metrics_watcher, clock=clock)

    for stamp in ("20260409_150425", "20260409_161500"):
        export = watch_dir / stamp
        export.mkdir()
        shutil.copy(reference_workbook, export / f"metrics_data_{stamp}.xlsx")

    watcher.poll()
    clock.advance(60)
    assert len(watcher.poll()) == 2
