from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from reliability_lab.analyzer import analyze_canary
from reliability_lab.database import utc_now
from reliability_lab.fault_drill import FaultDrillConfig, run_fault_drill
from reliability_lab.load import LoadConfig, Observation, run_load, write_csv_evidence
from reliability_lab.recovery import (
    backup_database,
    restore_database,
    run_recovery_drill,
    write_json_exclusive,
)
from reliability_lab.service import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reliability-lab")
    commands = parser.add_subparsers(dest="command", required=True)
    add_serve_command(commands)
    add_backup_restore_commands(commands)
    add_load_command(commands)
    add_fault_drill_command(commands)
    add_analyzer_command(commands)
    add_recovery_command(commands)
    return parser


def add_serve_command(commands) -> None:
    serve = commands.add_parser("serve", help="run the SQLite-backed business API")
    serve.add_argument("--database", type=Path)
    serve.add_argument("--host", default=os.getenv("RELIABILITY_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("RELIABILITY_PORT", "8000")))


def add_backup_restore_commands(commands) -> None:
    backup = commands.add_parser("backup", help="write a consistent SQLite snapshot and manifest")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--manifest", type=Path)
    restore = commands.add_parser(
        "restore", help="verify a backup and restore it to a new database"
    )
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    restore.add_argument("--manifest", type=Path)


def add_load_command(commands) -> None:
    load = commands.add_parser("load", help="record bounded HTTP observations as CSV")
    add_load_arguments(load)
    load.add_argument("--cohort", choices=("stable", "candidate"), default="candidate")
    load.add_argument("--output", type=Path, required=True)


def add_fault_drill_command(commands) -> None:
    drill = commands.add_parser("fault-drill", help="inject bounded 503s and verify recovery")
    add_load_arguments(drill)
    drill.add_argument("--ttl-seconds", type=int, default=10)
    drill.add_argument("--output", type=Path, required=True)


def add_analyzer_command(commands) -> None:
    analyzer = commands.add_parser("analyze-canary", help="compare measured stable/candidate CSV")
    analyzer.add_argument("--input", type=Path, required=True)
    analyzer.add_argument("--min-samples", type=int, default=20)
    analyzer.add_argument("--max-error-rate", type=float, default=0.01)
    analyzer.add_argument("--max-p95-regression", type=float, default=1.25)


def add_recovery_command(commands) -> None:
    recovery = commands.add_parser("recovery-drill", help="create, back up, and restore local data")
    recovery.add_argument("--output-dir", type=Path, default=Path("evidence"))


def add_load_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", required=True)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=2.0)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        return dispatch(arguments)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def dispatch(arguments: argparse.Namespace) -> int:
    handlers = {
        "serve": serve,
        "backup": backup,
        "restore": restore,
        "load": load,
        "fault-drill": fault_drill,
        "analyze-canary": analyze,
        "recovery-drill": recovery_drill,
    }
    return handlers[arguments.command](arguments)


def serve(arguments: argparse.Namespace) -> int:
    database = arguments.database or configured_database_path()
    server = create_server(
        database,
        operator_token=os.getenv("RELIABILITY_OPERATOR_TOKEN"),
        host=arguments.host,
        port=arguments.port,
    )
    host, port = server.server_address[:2]
    print_json(
        {"started_at": utc_now(), "listening_on": f"{host}:{port}", "database": str(database)}
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def backup(arguments: argparse.Namespace) -> int:
    result = backup_database(arguments.database, arguments.output, arguments.manifest)
    print_json(asdict(result))
    return 0


def restore(arguments: argparse.Namespace) -> int:
    result = restore_database(arguments.backup, arguments.output, arguments.manifest)
    print_json(asdict(result))
    return 0


def load(arguments: argparse.Namespace) -> int:
    config = LoadConfig(
        arguments.url,
        arguments.cohort,
        arguments.requests,
        arguments.concurrency,
        arguments.timeout,
    )
    observations = run_load(config)
    write_csv_evidence(arguments.output, observations)
    print_json(load_report(arguments.output, observations))
    return 0


def load_report(path: Path, observations: list[Observation]) -> dict[str, object]:
    statuses: dict[str, int] = {}
    for observation in observations:
        status = str(observation.status)
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "command": "load",
        "evidence_file": str(path.resolve()),
        "observations": len(observations),
        "cohort": observations[0].cohort,
        "status_counts": statuses,
        "first_request_started_at": min(row.timestamp for row in observations),
        "last_request_started_at": max(row.timestamp for row in observations),
    }


def fault_drill(arguments: argparse.Namespace) -> int:
    token = os.getenv("RELIABILITY_OPERATOR_TOKEN", "")
    config = FaultDrillConfig(
        arguments.url,
        arguments.requests,
        arguments.concurrency,
        arguments.timeout,
        arguments.ttl_seconds,
    )
    report = run_fault_drill(config, token)
    write_json_exclusive(arguments.output, report)
    print_json(report)
    return 0 if report["decision"] == "pass" else 1


def analyze(arguments: argparse.Namespace) -> int:
    report = analyze_canary(
        arguments.input,
        arguments.min_samples,
        arguments.max_error_rate,
        arguments.max_p95_regression,
    )
    print_json(report)
    decision = report["decision"]
    if decision == "pass":
        return 0
    if decision == "fail":
        return 1
    if decision == "inconclusive":
        return 2
    raise KeyError(decision)


def recovery_drill(arguments: argparse.Namespace) -> int:
    report = run_recovery_drill(arguments.output_dir)
    print_json(report)
    return 0 if report["decision"] == "pass" else 1


def configured_database_path() -> Path:
    return Path(os.getenv("RELIABILITY_DB_PATH", "data/reliability.db")).expanduser()


def print_json(document: object) -> None:
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
