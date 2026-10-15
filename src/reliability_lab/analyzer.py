from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from reliability_lab.load import CSV_FIELDS, Observation

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
MAX_EVIDENCE_ROWS = 100_000


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class CohortSummary:
    requests: int
    failed_requests: int
    error_rate: float
    p95_latency_ms: float


def analyze_canary(
    path: Path,
    minimum_samples: int = 20,
    maximum_error_rate: float = 0.01,
    maximum_p95_regression: float = 1.25,
) -> dict[str, object]:
    validate_policy(minimum_samples, maximum_error_rate, maximum_p95_regression)
    observations = read_observations(path)
    cohorts = split_cohorts(observations)
    summaries = {name: summarize_cohort(rows) for name, rows in cohorts.items()}
    decision, reasons = decide(
        summaries, minimum_samples, maximum_error_rate, maximum_p95_regression
    )
    return {
        "decision": decision,
        "reasons": reasons,
        "policy": policy_summary(minimum_samples, maximum_error_rate, maximum_p95_regression),
        **analysis_evidence(path, summaries),
    }


def analysis_evidence(path: Path, summaries: dict[str, CohortSummary]) -> dict[str, object]:
    return {
        "cohorts": {name: asdict(summary) for name, summary in summaries.items()},
        "evidence_file": str(path.resolve()),
        "claim": "This decision covers only the supplied observations and policy thresholds.",
    }


def policy_summary(
    minimum_samples: int, maximum_error_rate: float, maximum_regression: float
) -> dict[str, int | float]:
    return {
        "minimum_samples_per_cohort": minimum_samples,
        "maximum_candidate_error_rate": maximum_error_rate,
        "maximum_candidate_p95_regression": maximum_regression,
    }


def validate_policy(
    minimum_samples: int, maximum_error_rate: float, maximum_regression: float
) -> None:
    maximum_samples = MAX_EVIDENCE_ROWS // 2
    if type(minimum_samples) is not int or not 1 <= minimum_samples <= maximum_samples:
        raise ValueError(f"minimum_samples must be between 1 and {maximum_samples}")
    if (
        isinstance(maximum_error_rate, bool)
        or not isinstance(maximum_error_rate, (int, float))
        or not math.isfinite(maximum_error_rate)
        or not 0 <= maximum_error_rate <= 1
    ):
        raise ValueError("maximum_error_rate must be between 0 and 1")
    if (
        isinstance(maximum_regression, bool)
        or not isinstance(maximum_regression, (int, float))
        or not math.isfinite(maximum_regression)
        or not 0 < maximum_regression <= 10
    ):
        raise ValueError("maximum_p95_regression must be greater than 0 and at most 10")


def read_observations(path: Path) -> list[Observation]:
    if not path.is_file() or path.stat().st_size == 0:
        raise EvidenceError("Evidence file is missing or empty")
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise EvidenceError(f"Evidence file exceeds {MAX_EVIDENCE_BYTES} bytes")
    try:
        with path.open("r", encoding="utf-8", newline="") as evidence_file:
            reader = csv.DictReader(evidence_file)
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise EvidenceError(f"CSV header must be exactly: {','.join(CSV_FIELDS)}")
            observations = parse_rows(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise EvidenceError("Evidence file must be valid UTF-8 CSV") from exc
    if not observations:
        raise EvidenceError("Evidence file contains no observations")
    return observations


def parse_rows(reader: csv.DictReader) -> list[Observation]:
    observations: list[Observation] = []
    for index, row in enumerate(reader, start=2):
        if index > MAX_EVIDENCE_ROWS + 1:
            raise EvidenceError(f"Evidence exceeds {MAX_EVIDENCE_ROWS} observations")
        if None in row or any(row.get(field) is None for field in CSV_FIELDS):
            raise EvidenceError(f"Evidence row {index} has missing or extra fields")
        observations.append(parse_observation(row, index))
    return observations


def parse_observation(row: dict[str, str], line_number: int) -> Observation:
    cohort = row["cohort"]
    if cohort not in ("stable", "candidate"):
        raise EvidenceError(f"Evidence row {line_number} has an unknown cohort")
    timestamp = row["timestamp"]
    try:
        observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        latency = float(row["latency_ms"])
        status = int(row["status"])
    except (ValueError, OverflowError) as exc:
        raise EvidenceError(f"Evidence row {line_number} contains an invalid value") from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() != UTC.utcoffset(observed_at):
        raise EvidenceError(f"Evidence row {line_number} timestamp must be UTC")
    if not math.isfinite(latency) or not 0 <= latency <= 600_000:
        raise EvidenceError(f"Evidence row {line_number} latency_ms is outside the supported range")
    if status != 0 and not 100 <= status <= 599:
        raise EvidenceError(f"Evidence row {line_number} status must be 0 or an HTTP status")
    return Observation(timestamp, cohort, latency, status)


def split_cohorts(observations: list[Observation]) -> dict[str, list[Observation]]:
    cohorts = {
        name: [row for row in observations if row.cohort == name]
        for name in ("stable", "candidate")
    }
    missing = [name for name, rows in cohorts.items() if not rows]
    if missing:
        raise EvidenceError(f"Evidence is missing cohort: {', '.join(missing)}")
    return cohorts


def summarize_cohort(observations: list[Observation]) -> CohortSummary:
    failed = sum(is_bad_availability(row.status) for row in observations)
    latencies = sorted(row.latency_ms for row in observations)
    p95_index = max(0, math.ceil(0.95 * len(latencies)) - 1)
    return CohortSummary(
        len(observations), failed, failed / len(observations), latencies[p95_index]
    )


def is_bad_availability(status: int) -> bool:
    return status == 0 or 100 <= status < 200 or status >= 500


def decide(
    summaries: dict[str, CohortSummary],
    minimum_samples: int,
    maximum_error_rate: float,
    maximum_regression: float,
) -> tuple[str, list[str]]:
    stable = summaries["stable"]
    candidate = summaries["candidate"]
    if stable.requests < minimum_samples or candidate.requests < minimum_samples:
        return "inconclusive", ["Both cohorts must meet the minimum sample count."]
    reasons = []
    if candidate.error_rate > maximum_error_rate:
        reasons.append("Candidate error rate exceeds the configured maximum.")
    if candidate.p95_latency_ms > stable.p95_latency_ms * maximum_regression:
        reasons.append("Candidate p95 latency exceeds the stable-relative policy.")
    return (
        ("fail", reasons)
        if reasons
        else ("pass", ["Candidate met the configured sample, error, and latency policy."])
    )
