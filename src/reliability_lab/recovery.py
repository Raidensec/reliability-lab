from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from reliability_lab.database import create_order, initialize_database, summarize_database, utc_now


class RecoveryError(ValueError):
    pass


@dataclass(frozen=True)
class BackupResult:
    source_identity: str
    backup_identity: str
    manifest_identity: str
    record_count: int
    records_sha256: str
    database_sha256: str
    elapsed_ms: float


@dataclass(frozen=True)
class RestoreResult:
    backup_identity: str
    restore_identity: str
    record_count: int
    records_sha256: str
    elapsed_ms: float


@dataclass(frozen=True)
class RecoveryDrillState:
    backup: BackupResult | None
    restore: RestoreResult | None
    error_type: str | None
    elapsed_ms: float


def backup_database(source: Path, destination: Path, manifest: Path | None = None) -> BackupResult:
    started_at = time.perf_counter()
    source = source.resolve()
    destination = destination.resolve()
    manifest_path = (manifest or default_manifest_path(destination)).resolve()
    validate_backup_paths(source, destination, manifest_path)
    return write_backup(source, destination, manifest_path, started_at)


def write_backup(
    source: Path, destination: Path, manifest_path: Path, started_at: float
) -> BackupResult:
    created = False
    try:
        create_backup_file(source, destination)
        created = True
        result = build_backup_result(source, destination, manifest_path, started_at)
        write_json_exclusive(manifest_path, backup_manifest(result))
    except (OSError, RecoveryError, sqlite3.Error):
        if created:
            remove_sqlite_files(destination)
        raise
    return result


def build_backup_result(
    source: Path, destination: Path, manifest_path: Path, started_at: float
) -> BackupResult:
    summary = summarize_database(destination)
    return BackupResult(
        str(source),
        str(destination),
        str(manifest_path),
        summary.record_count,
        summary.records_sha256,
        file_sha256(destination),
        elapsed_milliseconds(started_at),
    )


def restore_database(
    backup: Path,
    destination: Path,
    manifest: Path | None = None,
) -> RestoreResult:
    started_at = time.perf_counter()
    backup = backup.resolve()
    destination = destination.resolve()
    manifest_path = (manifest or default_manifest_path(backup)).resolve()
    validate_restore_paths(backup, destination, manifest_path)
    expected = load_backup_manifest(manifest_path)
    verify_backup(backup, expected)
    create_backup_file(backup, destination)
    try:
        return verify_restore(backup, destination, expected, started_at)
    except (OSError, RecoveryError, sqlite3.Error):
        remove_sqlite_files(destination)
        raise


def validate_backup_paths(source: Path, destination: Path, manifest: Path) -> None:
    if not source.is_file():
        raise RecoveryError(f"Source database does not exist: {source}")
    if source == destination or source == manifest or destination == manifest:
        raise RecoveryError("Source, backup, and manifest paths must be distinct")
    if destination.exists() or manifest.exists():
        raise FileExistsError("Backup and manifest destinations must not already exist")


def validate_restore_paths(backup: Path, destination: Path, manifest: Path) -> None:
    if not backup.is_file() or not manifest.is_file():
        raise RecoveryError("Restore requires an existing backup database and manifest")
    if backup == destination or manifest == destination:
        raise RecoveryError("Backup, restore destination, and manifest paths must be distinct")
    if destination.exists():
        raise FileExistsError(f"Restore destination already exists: {destination}")


def create_backup_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb"):
        pass
    try:
        with closing(
            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5.0)
        ) as source_connection:
            with closing(sqlite3.connect(destination, timeout=5.0)) as target_connection:
                source_connection.backup(target_connection)
    except (OSError, sqlite3.Error):
        remove_sqlite_files(destination)
        raise


def backup_manifest(result: BackupResult) -> dict[str, str | int | float]:
    return {
        "format_version": 1,
        "created_at": utc_now(),
        "source_identity": result.source_identity,
        "backup_identity": result.backup_identity,
        "record_count": result.record_count,
        "records_sha256": result.records_sha256,
        "database_sha256": result.database_sha256,
        "backup_elapsed_ms": result.elapsed_ms,
    }


def load_backup_manifest(manifest: Path) -> dict[str, str | int | float]:
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"Backup manifest could not be read: {manifest}") from exc
    validate_manifest_structure(document)
    validate_manifest_identity(document)
    validate_manifest_summary(document)
    return document


def validate_manifest_structure(document: object) -> None:
    required = {
        "format_version",
        "created_at",
        "source_identity",
        "backup_identity",
        "record_count",
        "records_sha256",
        "database_sha256",
        "backup_elapsed_ms",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise RecoveryError("Backup manifest fields are incomplete or unsupported")


def validate_manifest_identity(document: dict[str, object]) -> None:
    if type(document["format_version"]) is not int or document["format_version"] != 1:
        raise RecoveryError("Backup manifest format_version is unsupported")
    if not isinstance(document["backup_identity"], str) or not document["backup_identity"]:
        raise RecoveryError("Backup manifest backup_identity is invalid")


def validate_manifest_summary(document: dict[str, object]) -> None:
    if type(document["record_count"]) is not int or document["record_count"] < 0:
        raise RecoveryError("Backup manifest record_count is invalid")
    for key in ("created_at", "source_identity", "records_sha256", "database_sha256"):
        if not isinstance(document[key], str) or not document[key]:
            raise RecoveryError(f"Backup manifest {key} is invalid")
    duration = document["backup_elapsed_ms"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise RecoveryError("Backup manifest backup_elapsed_ms is invalid")


def verify_backup(backup: Path, expected: dict[str, str | int | float]) -> None:
    summary = summarize_database(backup)
    if file_sha256(backup) != expected["database_sha256"]:
        raise RecoveryError("Backup file checksum does not match its manifest")
    if summary.record_count != expected["record_count"]:
        raise RecoveryError("Backup record count does not match its manifest")
    if summary.records_sha256 != expected["records_sha256"]:
        raise RecoveryError("Backup record checksum does not match its manifest")


def verify_restore(
    backup: Path,
    destination: Path,
    expected: dict[str, str | int | float],
    started_at: float,
) -> RestoreResult:
    backup_summary = summarize_database(backup)
    restored_summary = summarize_database(destination)
    if (
        restored_summary != backup_summary
        or restored_summary.record_count != expected["record_count"]
    ):
        raise RecoveryError("Restored database records do not match the verified backup")
    return RestoreResult(
        str(backup),
        str(destination),
        restored_summary.record_count,
        restored_summary.records_sha256,
        elapsed_milliseconds(started_at),
    )


def default_manifest_path(database_path: Path) -> Path:
    return database_path.with_name(database_path.name + ".manifest.json")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as database_file:
        for chunk in iter(lambda: database_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_exclusive(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            created = True
            json.dump(document, output, indent=2, sort_keys=True)
            output.write("\n")
    except (OSError, TypeError, ValueError):
        if created:
            path.unlink(missing_ok=True)
        raise


def remove_sqlite_files(database_path: Path) -> None:
    for path in (
        database_path,
        Path(str(database_path) + "-shm"),
        Path(str(database_path) + "-wal"),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def elapsed_milliseconds(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def run_recovery_drill(output_root: Path) -> dict[str, object]:
    started_at = time.perf_counter()
    run_directory = create_drill_directory(output_root.resolve())
    state = perform_recovery_drill(run_directory, started_at)
    evidence = recovery_evidence(run_directory, state)
    write_json_exclusive(run_directory / "recovery-drill.json", evidence)
    return evidence


def perform_recovery_drill(run_directory: Path, started_at: float) -> RecoveryDrillState:
    source = run_directory / "source.db"
    backup = run_directory / "backup.db"
    restored = run_directory / "restored.db"
    backup_result = None
    restore_result = None
    error_type = None
    try:
        initialize_database(source)
        create_order(source, "Recovery drill sample", 4250)
        backup_result = backup_database(source, backup)
        restore_result = restore_database(backup, restored)
        if backup_result.records_sha256 != restore_result.records_sha256:
            raise RecoveryError("Recovery drill restored a different record checksum")
    except (OSError, RecoveryError, sqlite3.Error) as exc:
        error_type = type(exc).__name__
    return RecoveryDrillState(
        backup_result, restore_result, error_type, elapsed_milliseconds(started_at)
    )


def create_drill_directory(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    run_directory = output_root / f"recovery-drill-{uuid.uuid4().hex}"
    run_directory.mkdir()
    return run_directory


def recovery_evidence(run_directory: Path, state: RecoveryDrillState) -> dict[str, object]:
    evidence = asdict(state)
    evidence.update(
        {
            "command": "recovery-drill",
            "decision": "pass" if state.error_type is None else "fail",
            "completed_at": utc_now(),
            "run_directory": str(run_directory),
            "verified_record_count": state.restore.record_count if state.restore else None,
            "verified_records_sha256": state.restore.records_sha256 if state.restore else None,
            "limitations": [
                "Observed local SQLite recovery timing is not a contractual RTO or RPO.",
                "The drill does not validate cloud storage, failover, or cross-region recovery.",
            ],
        }
    )
    return evidence
