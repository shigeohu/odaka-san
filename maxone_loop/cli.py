"""Command line interface.

``run --mode armed`` is accepted only from the command line and only with the
environment gate set; it is never the default, and the ``.claude`` PreToolUse
hook blocks it from Claude Code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config.loader import ConfigError, config_fingerprint, load_config
from .config.models import AppConfig
from .metrics.qc import evaluate
from .metrics.extract import extract
from .metrics.selection import SelectionError, select_recording
from .metrics.workbook import MetricsWorkbook, WorkbookError
from .orchestrator import Orchestrator, OrchestratorError, check_armed_gate
from .config.loader import sha256_file
from .watcher import MetricsWatcher


def _load(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config)
    if getattr(args, "mode", None):
        config = config.model_copy(
            update={
                "experiment": config.experiment.model_copy(
                    update={
                        "experiment": config.experiment.experiment.model_copy(
                            update={"mode": args.mode}
                        )
                    }
                )
            }
        )
    return config


def _find_workbooks(directory: Path, config: AppConfig) -> list[Path]:
    """Locate workbooks the same way the watcher would.

    Exports land in their own timestamped subfolder (CLAUDE.md section 3.1), so
    replaying a directory of real exports has to descend into it.
    """
    pattern = config.metrics_schema.file_format.filename_glob
    matches = (
        directory.rglob(pattern)
        if config.experiment.metrics_watcher.recursive
        else directory.glob(pattern)
    )
    prefixes = config.experiment.metrics_watcher.ignore_filename_prefixes
    return sorted(
        p
        for p in matches
        if p.is_file()
        and not any(part.startswith(tuple(prefixes)) for part in p.relative_to(directory).parts)
    )


def cmd_validate_config(args: argparse.Namespace) -> int:
    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"configuration invalid:\n{exc}", file=sys.stderr)
        return 2

    print(f"configuration OK  ({config_fingerprint(config)[:16]})")
    print(f"  mode        : {config.experiment.experiment.mode}")
    print(f"  experiment  : {config.experiment.experiment.experiment_id}")
    for name, digest in sorted(config.source_hashes.items()):
        print(f"  {name:28s} {digest[:16]}")

    missing = config.missing_for_armed()
    if missing:
        print(f"\narmed mode blocked; {len(missing)} required setting(s) are null:")
        for path in missing:
            print(f"  - {path}")
    else:
        print("\nall required_before_armed settings are populated")

    if config.experiment.experiment.mode == "armed":
        try:
            check_armed_gate(config)
        except OrchestratorError as exc:
            print(f"\narmed gate closed: {exc.message}", file=sys.stderr)
            return 2
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Describe a workbook without deciding anything. Read-only."""
    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"configuration invalid:\n{exc}", file=sys.stderr)
        return 2

    path = Path(args.workbook)
    try:
        workbook = MetricsWorkbook(path, config.metrics_schema)
    except WorkbookError as exc:
        print(f"{exc.reason_code}: {exc.message}", file=sys.stderr)
        return 2

    digest = sha256_file(path)
    recordings = workbook.recordings()
    print(f"{path.name}  sha256={digest[:16]}  {len(recordings)} recording(s)")
    print("  note: the filename timestamp is the export time, not a recording time")

    sheets = config.metrics_schema.sheets
    absent = [k for k in sheets.optional if not workbook.has_sheet(k)]
    if absent:
        # Expected for a summary-metrics export, or when no Network Analysis
        # trial was included. Reported so the operator can tell the difference
        # between "exported less" and "exported wrong".
        print(f"  optional sheet(s) not in this export: {', '.join(absent)}")

    activity_type = config.metrics_schema.instance_join.activity_analysis_type
    without_activity = [
        r.identity.wellplate_id
        for r in recordings.values()
        if len(r.instances.get(activity_type, ())) != 1
    ]
    if without_activity:
        print(
            f"  WARNING: {len(without_activity)} recording(s) do not have exactly one "
            f"{activity_type!r} instance: {', '.join(sorted(without_activity))}"
        )

    for folder_path, recording in recordings.items():
        instances = recording.instances.get(activity_type, ())
        line = (
            f"  {recording.identity.wellplate_id:9s} run={recording.identity.assay_run_id:>7s} "
            f"well={recording.identity.well_number} "
            f"dur={recording.acquisition.duration_seconds} "
            f"{recording.start}..{recording.stop}"
        )
        if len(instances) == 1:
            row = workbook.activity_row(instances[0])
            metric = config.metrics_schema.decision_metric.column
            area = config.metrics_schema.supporting_columns.activity_well_level.active_area_percent
            rate = workbook.as_float(row[metric], where=metric) if row else None
            active = workbook.as_float(row[area], where=area) if row else None
            line += f"  rate={rate} Hz  active_area={active} %"
        else:
            line += f"  [{len(instances)} {activity_type} instances]"
        print(line)
        if args.verbose:
            print(f"      {folder_path}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay recorded workbooks through selection, QC, and the policy."""
    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"configuration invalid:\n{exc}", file=sys.stderr)
        return 2

    if config.experiment.experiment.mode == "armed":
        print("replay refuses to run in armed mode", file=sys.stderr)
        return 2

    directory = Path(args.input)
    paths = _find_workbooks(directory, config)
    if not paths:
        print(f"no workbooks matching the configured glob in {directory}", file=sys.stderr)
        return 2

    schema = config.metrics_schema
    failures = 0
    for path in paths:
        try:
            workbook = MetricsWorkbook(path, schema)
            selection = select_recording(workbook, schema)
            analysis = extract(workbook, schema, selection, workbook_sha256=sha256_file(path))
            report = evaluate(analysis, schema)
        except (WorkbookError, SelectionError) as exc:
            print(f"{path.name}: {exc.reason_code}: {exc.message}")
            failures += 1
            continue

        verdict = "PASS" if report.passed else "REJECT " + ",".join(report.blocking_codes)
        print(
            f"{path.name}: {analysis.identity.wellplate_id} "
            f"rate={analysis.mean_firing_frequency_hz} Hz "
            f"area={analysis.active_area_percent} % -> {verdict}"
        )
        if not report.passed:
            failures += 1
        if args.json:
            print(json.dumps(analysis.to_record(), indent=2, default=str))

    return 1 if failures else 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"configuration invalid:\n{exc}", file=sys.stderr)
        return 2

    workbooks = None
    watcher = None
    if args.input:
        directory = Path(args.input)
        workbooks = _find_workbooks(directory, config)
    else:
        watch_directory = config.experiment.paths.metrics_watch_directory
        if watch_directory is None:
            print(
                "paths.metrics_watch_directory is null; set it or pass --input",
                file=sys.stderr,
            )
            return 2
        watcher = MetricsWatcher(watch_directory, config.experiment.metrics_watcher)

    orchestrator = Orchestrator(config, watcher=watcher)
    summary = orchestrator.run(workbooks=workbooks)

    print(f"experiment {summary.experiment_id}  mode={summary.mode}")
    print(f"final state : {summary.final_state.value}")
    print(f"reason      : {summary.reason_code}")
    for cycle in summary.cycles:
        rate = (
            cycle.analysis.mean_firing_frequency_hz if cycle.analysis is not None else None
        )
        print(
            f"  cycle {cycle.cycle_index}: {cycle.decision.action:9s} "
            f"{cycle.decision.reason_code:32s} rate={rate} "
            f"amplitude={cycle.decision.candidate_amplitude_mV}"
        )
    return 0 if summary.final_state.value == "COMPLETE" else 1


def build_parser() -> argparse.ArgumentParser:
    # --config is accepted both before and after the subcommand, so the
    # invocations documented in CLAUDE.md section 14 work as written.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config/experiment.yaml")

    parser = argparse.ArgumentParser(
        prog="maxone_loop", description=__doc__, parents=[common]
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", parents=[common])
    validate.add_argument("--mode", choices=["simulate", "dry_run", "armed"])
    validate.set_defaults(func=cmd_validate_config)

    inspect = subparsers.add_parser("inspect", parents=[common], help="describe a workbook")
    inspect.add_argument("workbook")
    inspect.add_argument("--verbose", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    replay = subparsers.add_parser("replay", parents=[common])
    replay.add_argument("--input", required=True)
    replay.add_argument("--json", action="store_true")
    replay.add_argument("--mode", choices=["simulate", "dry_run"])
    replay.set_defaults(func=cmd_replay)

    run = subparsers.add_parser("run", parents=[common])
    run.add_argument("--mode", choices=["simulate", "dry_run", "armed"], default="dry_run")
    run.add_argument("--input", help="replay a directory instead of watching one")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
