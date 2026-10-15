from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from threading import Lock

ROUTE = "/api/orders"
METHODS = ("GET", "POST", "OTHER")
STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx", "other")
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


@dataclass
class HistogramStats:
    bucket_counts: list[int]
    count: int = 0
    total_seconds: float = 0.0


class BusinessMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._histograms: dict[tuple[str, str], HistogramStats] = {}

    def observe(self, method: str, status: int, elapsed_seconds: float) -> None:
        bounded_method = method if method in METHODS else "OTHER"
        status_class = status_class_name(status)
        with self._lock:
            self._requests[(ROUTE, bounded_method, status_class)] += 1
            stats = self._histograms.setdefault(
                (ROUTE, bounded_method), HistogramStats([0] * len(LATENCY_BUCKETS))
            )
            stats.count += 1
            stats.total_seconds += elapsed_seconds
            for index, bucket in enumerate(LATENCY_BUCKETS):
                if elapsed_seconds <= bucket:
                    stats.bucket_counts[index] += 1

    def render(self) -> str:
        with self._lock:
            snapshots = {
                labels: HistogramStats(stats.bucket_counts.copy(), stats.count, stats.total_seconds)
                for labels, stats in self._histograms.items()
            }
            return render_metrics(self._requests.copy(), snapshots)


def status_class_name(status: int) -> str:
    if 200 <= status < 300:
        return "2xx"
    if 300 <= status < 400:
        return "3xx"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return "other"


def render_metrics(
    requests: dict[tuple[str, str, str], int],
    histograms: dict[tuple[str, str], HistogramStats],
) -> str:
    lines = [
        "# HELP reliability_business_requests_total Bounded business API response counts.",
        "# TYPE reliability_business_requests_total counter",
    ]
    for labels, count in sorted(requests.items()):
        rendered = format_labels(labels[0], labels[1], status=labels[2])
        lines.append(f"reliability_business_requests_total{rendered} {count}")
    lines.extend(
        [
            "# HELP reliability_business_request_duration_seconds Business API response duration.",
            "# TYPE reliability_business_request_duration_seconds histogram",
        ]
    )
    for (route, method), observations in sorted(histograms.items()):
        append_histogram(lines, route, method, observations)
    return "\n".join(lines) + "\n"


def append_histogram(lines: list[str], route: str, method: str, stats: HistogramStats) -> None:
    for index, bucket in enumerate(LATENCY_BUCKETS):
        lines.append(
            f"reliability_business_request_duration_seconds_bucket"
            f"{format_labels(route, method, bucket=str(bucket))} {stats.bucket_counts[index]}"
        )
    lines.append(
        f"reliability_business_request_duration_seconds_bucket"
        f"{format_labels(route, method, bucket='+Inf')} {stats.count}"
    )
    lines.append(
        f"reliability_business_request_duration_seconds_sum{format_labels(route, method)} "
        f"{stats.total_seconds:.9f}"
    )
    labels = format_labels(route, method)
    lines.append(f"reliability_business_request_duration_seconds_count{labels} {stats.count}")


def format_labels(
    route: str,
    method: str,
    status: str | None = None,
    bucket: str | None = None,
) -> str:
    rendered = f'route="{route}",method="{method}"'
    if status is not None:
        rendered += f',status="{status}"'
    if bucket is not None:
        rendered += f',le="{bucket}"'
    return "{" + rendered + "}"
