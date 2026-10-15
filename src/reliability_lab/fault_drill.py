from __future__ import annotations

import json
import urllib.request
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from reliability_lab.database import utc_now
from reliability_lab.load import (
    LoadConfig,
    Observation,
    RejectRedirects,
    run_load,
    validate_load_parameters,
)
from reliability_lab.service import validate_operator_token


class FaultDrillError(ValueError):
    pass


@dataclass(frozen=True)
class FaultDrillConfig:
    url: str
    request_count: int
    concurrency: int
    timeout: float
    ttl_seconds: int = 10


@dataclass
class FaultDrillState:
    started_at: str
    injected: bool = False
    observations: list[Observation] = field(default_factory=list)
    drill_error: str | None = None
    cleanup_succeeded: bool = False
    cleanup_error: str | None = None
    recovery: list[Observation] = field(default_factory=list)
    recovery_error: str | None = None


def run_fault_drill(config: FaultDrillConfig, token: str) -> dict[str, object]:
    validate_fault_config(config)
    if validate_operator_token(token) is None:
        raise ValueError("RELIABILITY_OPERATOR_TOKEN must be set for a fault drill")
    state = FaultDrillState(started_at=utc_now())
    try:
        inject_and_measure(config, token, state)
    finally:
        clear_fault(config, token, state)
    measure_recovery(config, state)
    return fault_report(config, state)


def validate_fault_config(config: FaultDrillConfig) -> None:
    validate_load_parameters(load_config(config))
    if type(config.ttl_seconds) is not int or not 1 <= config.ttl_seconds <= 30:
        raise ValueError("fault TTL must be between 1 and 30 seconds")


def load_config(config: FaultDrillConfig) -> LoadConfig:
    return LoadConfig(
        config.url, "candidate", config.request_count, config.concurrency, config.timeout
    )


def inject_and_measure(config: FaultDrillConfig, token: str, state: FaultDrillState) -> None:
    try:
        response = post_operator(
            config.url,
            "/operator/fault",
            token,
            {"kind": "unavailable", "ttl_seconds": config.ttl_seconds},
        )
        if response != {"kind": "unavailable", "ttl_seconds": config.ttl_seconds}:
            raise FaultDrillError("Fault endpoint did not confirm the requested fault")
        state.injected = True
        state.observations = run_load(load_config(config))
    except (OSError, ValueError) as exc:
        state.drill_error = type(exc).__name__


def clear_fault(config: FaultDrillConfig, token: str, state: FaultDrillState) -> None:
    try:
        response = post_operator(config.url, "/operator/clear", token, None)
        if response != {"cleared": True}:
            raise FaultDrillError("Fault clear endpoint did not confirm cleanup")
        state.cleanup_succeeded = True
    except (OSError, ValueError) as exc:
        state.cleanup_error = type(exc).__name__


def measure_recovery(config: FaultDrillConfig, state: FaultDrillState) -> None:
    if not state.cleanup_succeeded:
        return
    try:
        state.recovery = run_load(recovery_config(config))
    except (OSError, ValueError) as exc:
        state.recovery_error = type(exc).__name__


def recovery_config(config: FaultDrillConfig) -> LoadConfig:
    recovery_count = min(5, max(1, config.request_count // 4))
    return LoadConfig(
        config.url,
        "candidate",
        recovery_count,
        min(config.concurrency, recovery_count),
        config.timeout,
    )


def fault_report(config: FaultDrillConfig, state: FaultDrillState) -> dict[str, object]:
    return {
        "command": "fault-drill",
        "started_at": state.started_at,
        "completed_at": utc_now(),
        "fault_kind": "unavailable",
        "fault_ttl_seconds": config.ttl_seconds,
        **fault_measurements(state),
        "observations": [
            asdict(observation) for observation in state.observations + state.recovery
        ],
        "limitations": [
            "This drill validates one local or operator-authenticated service endpoint.",
            "It does not establish a production SLO or cross-region recovery behavior.",
        ],
    }


def fault_measurements(state: FaultDrillState) -> dict[str, object]:
    observations = state.observations
    recovery = state.recovery
    failures = sum(observation.status == 503 for observation in observations)
    recovered = bool(recovery) and all(200 <= observation.status < 300 for observation in recovery)
    passed = state.injected and failures > 0 and state.cleanup_succeeded and recovered
    return {
        "decision": "pass" if passed else "fail",
        "fault_injection_accepted": state.injected,
        "failure_observations": len(observations),
        "injected_503_observations": failures,
        "cleanup_succeeded": state.cleanup_succeeded,
        "cleanup_error_type": state.cleanup_error,
        "recovery_observations": len(recovery),
        "recovery_successes": sum(200 <= observation.status < 300 for observation in recovery),
        "recovery_error_type": state.recovery_error,
        "recovered": recovered,
        "drill_error_type": state.drill_error,
    }


def post_operator(
    url: str, path: str, token: str, payload: dict[str, object] | None
) -> dict[str, object]:
    origin = urlsplit(url)
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    # run_fault_drill validates the API URL as HTTP(S) before calling this helper.
    request = urllib.request.Request(  # noqa: S310
        f"{origin.scheme}://{origin.netloc}{path}",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(RejectRedirects)
    with opener.open(request, timeout=5.0) as response:  # noqa: S310  # nosec B310
        if response.status not in (200, 202):
            raise FaultDrillError(f"Operator endpoint returned HTTP {response.status}")
        document = json.loads(response.read())
    if not isinstance(document, dict):
        raise FaultDrillError("Operator endpoint returned a non-object JSON response")
    return document
