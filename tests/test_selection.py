"""Recording selection tests (CLAUDE.md section 14, "Recording selection").

The reference workbook holds seven unrelated chips, so every one of these runs
against a genuinely ambiguous input.
"""

from __future__ import annotations

import pytest

from maxone_loop.metrics.selection import SelectionError, select_recording
from maxone_loop.metrics.workbook import MetricsWorkbook
from maxone_loop.reason_codes import (
    ACTIVITY_INSTANCE_NOT_FOUND,
    RECORDING_ALREADY_CONSUMED,
    RECORDING_AMBIGUOUS,
    RECORDING_NOT_FOUND,
)

P005157_PATH = "/home/mxwbio/Data/Odaka/CO_260206_260318/260409/P005157/Network/000029"


def _workbook(path, config):
    return MetricsWorkbook(path, config.metrics_schema)


def test_selects_exactly_one_of_seven(reference_workbook, runnable_config):
    config = runnable_config()
    selection = select_recording(_workbook(reference_workbook, config), config.metrics_schema)

    assert selection.recording.identity.wellplate_id == "P005157"
    assert selection.activity_instance == 3
    assert len(selection.considered) == 7


def test_folder_path_exact_selects_one(reference_workbook, runnable_config):
    config = runnable_config(
        **{
            "metrics_schema.recording_selection.strategy": "folder_path_exact",
            "metrics_schema.recording_selection.folder_path": P005157_PATH,
        }
    )
    selection = select_recording(_workbook(reference_workbook, config), config.metrics_schema)
    assert selection.recording.identity.folder_path == P005157_PATH


def test_zero_matches_faults(reference_workbook, runnable_config):
    config = runnable_config(
        **{"metrics_schema.recording_selection.wellplate_id": "P999999"}
    )
    with pytest.raises(SelectionError) as exc:
        select_recording(_workbook(reference_workbook, config), config.metrics_schema)
    assert exc.value.reason_code == RECORDING_NOT_FOUND


def test_multiple_matches_fault_rather_than_picking_one(reference_workbook, runnable_config):
    """A prefix covering the whole session day matches all seven recordings."""
    config = runnable_config(
        **{
            "metrics_schema.recording_selection.strategy": "folder_path_prefix",
            "metrics_schema.recording_selection.folder_path": (
                "/home/mxwbio/Data/Odaka/CO_260206_260318/260409/"
            ),
        }
    )
    with pytest.raises(SelectionError) as exc:
        select_recording(_workbook(reference_workbook, config), config.metrics_schema)
    assert exc.value.reason_code == RECORDING_AMBIGUOUS
    assert "P005157" in exc.value.message


def test_no_fallback_to_single_row(tmp_path, runnable_config):
    """Even with one recording present, a non-matching rule must not select it."""
    from maxone_loop.synthetic import SyntheticRecording, write_workbook

    config = runnable_config(
        **{"metrics_schema.recording_selection.wellplate_id": "SOMETHING-ELSE"}
    )
    path = write_workbook(
        tmp_path / "metrics_data_20260101_000000.xlsx",
        [SyntheticRecording("P005157", "000029", 0.5)],
    )
    with pytest.raises(SelectionError) as exc:
        select_recording(_workbook(path, config), config.metrics_schema)
    assert exc.value.reason_code == RECORDING_NOT_FOUND


def test_unconfigured_strategy_refuses_to_guess(reference_workbook, config_factory):
    config = config_factory()  # tracked config: strategy is null
    assert config.metrics_schema.recording_selection.strategy is None

    with pytest.raises(SelectionError) as exc:
        select_recording(_workbook(reference_workbook, config), config.metrics_schema)
    assert exc.value.reason_code == RECORDING_NOT_FOUND
    assert "refusing to guess" in exc.value.message


def test_already_consumed_recording_is_rejected(reference_workbook, runnable_config):
    config = runnable_config()
    with pytest.raises(SelectionError) as exc:
        select_recording(
            _workbook(reference_workbook, config),
            config.metrics_schema,
            consumed_folder_paths=frozenset({P005157_PATH}),
        )
    assert exc.value.reason_code == RECORDING_ALREADY_CONSUMED


def test_re_export_consumes_only_the_new_recording(tmp_path, runnable_config):
    """A workbook holding an old and a new recording yields only the new one."""
    from maxone_loop.synthetic import SyntheticRecording, write_workbook

    config = runnable_config(
        **{
            "metrics_schema.recording_selection.strategy": "folder_path_prefix",
            "metrics_schema.recording_selection.folder_path": "/home/mxwbio/Data/Synthetic/",
        }
    )
    old = SyntheticRecording("P1", "000001", 0.5)
    new = SyntheticRecording("P2", "000002", 0.6)
    path = write_workbook(tmp_path / "metrics_data_20260101_000000.xlsx", [old, new])

    # Both present -> ambiguous, as designed.
    with pytest.raises(SelectionError) as exc:
        select_recording(_workbook(path, config), config.metrics_schema)
    assert exc.value.reason_code == RECORDING_AMBIGUOUS

    # With the old one consumed, the rule still matches both, so it is still
    # ambiguous. Narrowing the rule is what resolves it, never the ledger.
    narrowed = runnable_config(
        **{
            "metrics_schema.recording_selection.strategy": "folder_path_exact",
            "metrics_schema.recording_selection.folder_path": new.path(),
        }
    )
    selection = select_recording(
        _workbook(path, narrowed),
        narrowed.metrics_schema,
        consumed_folder_paths=frozenset({old.path()}),
    )
    assert selection.recording.identity.wellplate_id == "P2"


def test_recording_without_activity_instance_faults(tmp_path, runnable_config):
    from openpyxl import load_workbook

    from maxone_loop.synthetic import SyntheticRecording, write_workbook

    config = runnable_config(
        **{
            "metrics_schema.recording_selection.strategy": "folder_path_prefix",
            "metrics_schema.recording_selection.folder_path": "/home/mxwbio/Data/Synthetic/",
        }
    )
    path = write_workbook(
        tmp_path / "metrics_data_20260101_000001.xlsx",
        [SyntheticRecording("P1", "000001", 0.5)],
    )
    book = load_workbook(path)
    sheet = book["Analysis Parameters"]
    for row in sheet.iter_rows(min_row=2):
        if row[1].value == "Activity Analysis":
            row[1].value = "ISI-N Burst Detector"
    book.save(path)

    with pytest.raises(SelectionError) as exc:
        select_recording(_workbook(path, config), config.metrics_schema)
    assert exc.value.reason_code == ACTIVITY_INSTANCE_NOT_FOUND
