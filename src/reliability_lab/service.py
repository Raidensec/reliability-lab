from __future__ import annotations

import hmac
import json
import logging
import sqlite3
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

from reliability_lab.database import (
    create_order,
    database_ready,
    initialize_database,
    list_orders,
    order_json,
)
from reliability_lab.metrics import BusinessMetrics

MAX_BODY_BYTES = 16_384
MAX_CUSTOMER_LENGTH = 120
MAX_AMOUNT_CENTS = 1_000_000_000
MAX_FAULT_TTL_SECONDS = 30


class InvalidRequest(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class FaultController:
    def __init__(self) -> None:
        self._lock = Lock()
        self._expires_at: float | None = None

    def activate_unavailable(self, ttl_seconds: int) -> None:
        if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= MAX_FAULT_TTL_SECONDS:
            raise InvalidRequest(f"ttl_seconds must be between 1 and {MAX_FAULT_TTL_SECONDS}")
        with self._lock:
            self._expires_at = time.monotonic() + ttl_seconds

    def clear(self) -> None:
        with self._lock:
            self._expires_at = None

    def is_active(self) -> bool:
        with self._lock:
            if self._expires_at is None:
                return False
            if time.monotonic() >= self._expires_at:
                self._expires_at = None
                return False
            return True


class Application:
    def __init__(self, database_path: Path, operator_token: str | None) -> None:
        self.database_path = database_path
        self.operator_token = validate_operator_token(operator_token)
        self.faults = FaultController()
        self.metrics = BusinessMetrics()
        initialize_database(database_path)

    def business_response(self, method: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        if method not in ("GET", "POST"):
            return 405, {"error": "method_not_allowed"}
        if self.faults.is_active():
            return 503, {"error": "injected_unavailability"}
        if method == "GET":
            try:
                orders = list_orders(self.database_path)
                return 200, {"orders": [order_json(order) for order in orders]}
            except sqlite3.Error:
                logging.exception("Order query failed")
                return 503, {"error": "storage_unavailable"}
        return self.create_order_response(body)

    def create_order_response(self, body: bytes | None) -> tuple[int, dict[str, Any]]:
        try:
            fields = parse_order_body(body)
            order = create_order(self.database_path, fields["customer"], fields["amount_cents"])
            return 201, {"order": order_json(order)}
        except InvalidRequest as exc:
            return 400, {"error": "invalid_request", "detail": str(exc)}
        except sqlite3.Error:
            logging.exception("Order persistence failed")
            return 503, {"error": "storage_unavailable"}

    def operator_response(
        self, path: str, method: str, headers: Any, body: bytes | None
    ) -> tuple[int, dict[str, Any]]:
        if self.operator_token is None:
            return 404, {"error": "not_found"}
        if method != "POST":
            return 405, {"error": "method_not_allowed"}
        if not authorized(headers.get("Authorization"), self.operator_token):
            return 401, {"error": "unauthorized"}
        if path == "/operator/fault":
            return self.activate_fault_response(body)
        if path == "/operator/clear":
            self.faults.clear()
            return 200, {"cleared": True}
        return 404, {"error": "not_found"}

    def activate_fault_response(self, body: bytes | None) -> tuple[int, dict[str, Any]]:
        try:
            payload = parse_json_body(body)
            if set(payload) != {"kind", "ttl_seconds"} or payload["kind"] != "unavailable":
                raise InvalidRequest("kind must be unavailable and ttl_seconds is required")
            ttl_seconds = payload["ttl_seconds"]
            if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
                raise InvalidRequest("ttl_seconds must be an integer")
            self.faults.activate_unavailable(ttl_seconds)
            return 202, {"kind": "unavailable", "ttl_seconds": ttl_seconds}
        except InvalidRequest as exc:
            return 400, {"error": "invalid_request", "detail": str(exc)}


class ReliabilityRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args: Any, application: Application, **kwargs: Any) -> None:
        self.application = application
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        self.dispatch_request()

    def do_POST(self) -> None:
        self.dispatch_request()

    def do_PUT(self) -> None:
        self.dispatch_request()

    def do_PATCH(self) -> None:
        self.dispatch_request()

    def do_DELETE(self) -> None:
        self.dispatch_request()

    def dispatch_request(self) -> None:
        path = urlsplit(self.path).path
        if path == "/metrics" and self.command == "GET":
            self.write_metrics()
            return
        started_at = time.perf_counter()
        try:
            body = self.read_request_body() if self.needs_json_body(path) else None
            status, response = self.route_request(path, body)
        except InvalidRequest as exc:
            status, response = exc.status, {"error": "invalid_request", "detail": str(exc)}
        if path == "/api/orders":
            self.application.metrics.observe(self.command, status, time.perf_counter() - started_at)
        self.write_json(status, response)

    def needs_json_body(self, path: str) -> bool:
        return self.command == "POST" and path in ("/api/orders", "/operator/fault")

    def route_request(self, path: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        if path == "/healthz" and self.command == "GET":
            return 200, {"status": "live"}
        if path == "/readyz" and self.command == "GET":
            return self.readiness_response()
        if path == "/api/orders":
            return self.application.business_response(self.command, body)
        if path.startswith("/operator/"):
            return self.application.operator_response(path, self.command, self.headers, body)
        return 404, {"error": "not_found"}

    def readiness_response(self) -> tuple[int, dict[str, Any]]:
        try:
            ready = database_ready(self.application.database_path)
            if not ready:
                return 503, {"status": "not_ready", "dependency": "sqlite"}
            return 200, {"status": "ready", "dependency": "sqlite"}
        except sqlite3.Error:
            logging.exception("Readiness database check failed")
            return 503, {"status": "not_ready", "dependency": "sqlite"}

    def write_metrics(self) -> None:
        content = self.application.metrics.render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def read_request_body(self) -> bytes:
        content_type = (
            self.headers.get("Content-Type", "").split(";", maxsplit=1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise InvalidRequest("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isascii() or not raw_length.isdecimal() or len(raw_length) > 8:
            raise InvalidRequest("Content-Length must be a valid nonnegative integer")
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            raise InvalidRequest("Request body is too large", 413)
        return self.rfile.read(length)

    def write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


def parse_order_body(body: bytes | None) -> dict[str, str | int]:
    payload = parse_json_body(body)
    if set(payload) != {"customer", "amount_cents"}:
        raise InvalidRequest("JSON must contain customer and amount_cents only")
    customer = payload["customer"]
    amount_cents = payload["amount_cents"]
    if not isinstance(customer, str) or not customer.strip() or len(customer) > MAX_CUSTOMER_LENGTH:
        raise InvalidRequest(f"customer must contain 1 to {MAX_CUSTOMER_LENGTH} characters")
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
        raise InvalidRequest("amount_cents must be an integer")
    if not 1 <= amount_cents <= MAX_AMOUNT_CENTS:
        raise InvalidRequest(f"amount_cents must be between 1 and {MAX_AMOUNT_CENTS}")
    return {"customer": customer.strip(), "amount_cents": amount_cents}


def parse_json_body(body: bytes | None) -> dict[str, Any]:
    if body is None:
        raise InvalidRequest("A JSON request body is required")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidRequest("Request body must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidRequest("JSON request body must be an object")
    return payload


def authorized(header: str | None, token: str) -> bool:
    expected = f"Bearer {token}"
    return header is not None and header.isascii() and hmac.compare_digest(header, expected)


def validate_operator_token(token: str | None) -> str | None:
    if token is None or len(token) == 0:
        return None
    if len(token) < 32 or any(not 33 <= ord(character) <= 126 for character in token):
        raise ValueError("RELIABILITY_OPERATOR_TOKEN must be at least 32 visible ASCII characters")
    return token


def create_server(
    database_path: Path,
    operator_token: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> ThreadingHTTPServer:
    validate_server_binding(host, port)
    application = Application(database_path, operator_token)
    handler = partial(ReliabilityRequestHandler, application=application)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def validate_server_binding(host: str, port: int) -> None:
    if (
        not isinstance(host, str)
        or not host
        or host.strip() != host
        or any(character.isspace() for character in host)
    ):
        raise ValueError("host must be a nonempty name or address without whitespace")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
