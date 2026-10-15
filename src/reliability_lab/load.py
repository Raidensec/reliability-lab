from __future__ import annotations

import csv
import math
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from reliability_lab.database import utc_now

MAX_REQUESTS = 10_000
MAX_CONCURRENCY = 64
MAX_TIMEOUT_SECONDS = 10.0
CSV_FIELDS = ("timestamp", "cohort", "latency_ms", "status")


class LoadEvidenceError(ValueError):
    pass


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class Observation:
    timestamp: str
    cohort: str
    latency_ms: float
    status: int


@dataclass(frozen=True)
class LoadConfig:
    url: str
    cohort: str
    request_count: int
    concurrency: int
    timeout: float


def validate_load_parameters(config: LoadConfig) -> None:
    parsed = urlsplit(config.url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("URL must be an HTTP(S) origin without embedded credentials")
    _ = parsed.port
    if parsed.path != "/api/orders" or parsed.query or parsed.fragment:
        raise ValueError("URL path must be /api/orders without a query or fragment")
    if config.cohort not in ("stable", "candidate"):
        raise ValueError("cohort must be stable or candidate")
    if type(config.request_count) is not int or not 1 <= config.request_count <= MAX_REQUESTS:
        raise ValueError(f"requests must be between 1 and {MAX_REQUESTS}")
    if type(config.concurrency) is not int or not 1 <= config.concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    if not math.isfinite(config.timeout) or not 0.1 <= config.timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must be between 0.1 and {MAX_TIMEOUT_SECONDS:g} seconds")


def run_load(config: LoadConfig) -> list[Observation]:
    validate_load_parameters(config)
    with ThreadPoolExecutor(
        max_workers=config.concurrency, thread_name_prefix="reliability-load"
    ) as pool:
        futures = [
            pool.submit(observe_request, config.url, config.cohort, config.timeout)
            for _ in range(config.request_count)
        ]
        observations = [future.result() for future in futures]
    return observations


def observe_request(url: str, cohort: str, timeout: float) -> Observation:
    timestamp = utc_now()
    # run_load validates the origin as HTTP(S) before scheduling observations.
    # nosec B310
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")  # noqa: S310  # nosec B310
    opener = urllib.request.build_opener(RejectRedirects)
    monotonic_start = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        status = 0
    elapsed_ms = max(0.0, (time.perf_counter() - monotonic_start) * 1000)
    return Observation(timestamp, cohort, round(elapsed_ms, 3), status)


def write_csv_evidence(path: Path, observations: list[Observation]) -> None:
    if not observations:
        raise LoadEvidenceError("Refusing to write an empty load evidence file")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as evidence_file:
        writer = csv.DictWriter(evidence_file, fieldnames=CSV_FIELDS, extrasaction="raise")
        writer.writeheader()
        for observation in observations:
            writer.writerow(asdict(observation))
