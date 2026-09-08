"""Contract-first checks for the loopback-only control-room API."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from collections.abc import Callable, Mapping
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import cast

import pytest

import migration_rehearsal.control_room_http as control_room_http
from migration_rehearsal.control_room_adapter import EngineBusyError, RehearsalResponse
from migration_rehearsal.control_room_http import create_server

_STATIC_NAMES = ("app.css", "app.js", "favicon.svg", "index.html", "site.webmanifest")


class _AdmissionSocket:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.timeouts: list[float] = []
        self.shutdown_calls: list[int] = []
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def sendall(self, value: bytes) -> None:
        self.sent.extend(value)

    def shutdown(self, how: int) -> None:
        self.shutdown_calls.append(how)

    def close(self) -> None:
        self.closed = True


class _FailingHandler:
    calls = 0

    def __init__(self, *_args: object) -> None:
        type(self).calls += 1
        raise RuntimeError("handler failed")


def _failing_handler() -> type[BaseHTTPRequestHandler]:
    return cast(type[BaseHTTPRequestHandler], _FailingHandler)


def _write_static_bundle(static_root: Path) -> None:
    static_root.mkdir()
    manifest = []
    for asset in _STATIC_NAMES:
        content = f"asset:{asset}".encode()
        (static_root / asset).write_bytes(content)
        manifest.append(
            {
                "path": asset,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    (static_root / "asset-manifest.json").write_text(
        json.dumps({"schema_version": 1, "files": manifest}), encoding="utf-8"
    )


def _copy_control_room_data(root: Path) -> None:
    target = root / "control-room-data"
    target.mkdir(parents=True)
    for name in ("fixtures-v1.json", "historical-phase-b-v1.json"):
        target.joinpath(name).write_bytes(
            Path.cwd().joinpath("control-room-data", name).read_bytes()
        )


def _runner(scenario_id: str) -> Mapping[str, object]:
    return {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "verdict": "LOCAL_REHEARSAL_PASSED",
        "verdict_label": "Local rehearsal passed",
        "run_id": "a" * 64,
    }


def test_request_admission_precedes_parser_and_thread_creation() -> None:
    _FailingHandler.calls = 0
    server = control_room_http._Server(("127.0.0.1", 0), _failing_handler())
    server._request_slots = threading.BoundedSemaphore(1)
    request = _AdmissionSocket()
    try:
        assert server._request_slots.acquire(blocking=False)
        server.process_request(cast(socket.socket, request), ("127.0.0.1", 49152))

        response = bytes(request.sent)
        assert response.startswith(b"HTTP/1.0 503 Service Unavailable\r\n")
        assert b'"code":"BUSY"' in response
        assert b"Content-Security-Policy: default-src 'self'" in response
        assert b"Connection: close\r\n" in response
        assert request.timeouts == [5]
        assert request.shutdown_calls == [socket.SHUT_WR]
        assert request.closed is True
        assert _FailingHandler.calls == 0
    finally:
        server._request_slots.release()
        server.server_close()


def test_admission_slot_is_released_when_thread_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = control_room_http._Server(("127.0.0.1", 0), _failing_handler())
    server._request_slots = threading.BoundedSemaphore(1)

    def fail_after_registration(_thread: threading.Thread) -> None:
        assert server._request_slots.acquire(blocking=False) is False
        raise RuntimeError("thread start failed")

    try:
        with monkeypatch.context() as scoped_patch:
            scoped_patch.setattr(threading.Thread, "start", fail_after_registration)
            with pytest.raises(RuntimeError, match="thread start failed"):
                server.process_request(
                    cast(socket.socket, _AdmissionSocket()), ("127.0.0.1", 49152)
                )
        assert server._request_slots.acquire(blocking=False)
        server._request_slots.release()
    finally:
        server.server_close()


def test_admission_slot_is_released_after_handler_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FailingHandler.calls = 0
    server = control_room_http._Server(("127.0.0.1", 0), _failing_handler())
    server._request_slots = threading.BoundedSemaphore(1)
    request = _AdmissionSocket()
    monkeypatch.setattr(server, "handle_error", lambda *_args: None)
    try:
        assert server._request_slots.acquire(blocking=False)
        server.process_request_thread(cast(socket.socket, request), ("127.0.0.1", 49152))
        assert _FailingHandler.calls == 1
        assert request.closed is True
        assert server._request_slots.acquire(blocking=False)
        server._request_slots.release()
    finally:
        server.server_close()


def test_request_admission_capacity_is_per_server() -> None:
    first = control_room_http._Server(("127.0.0.1", 0), _failing_handler())
    second = control_room_http._Server(("127.0.0.1", 0), _failing_handler())
    try:
        for _ in range(control_room_http._MAX_HTTP_REQUESTS):
            assert first._request_slots.acquire(blocking=False)
        assert first._request_slots.acquire(blocking=False) is False
        assert second._request_slots.acquire(blocking=False)
        second._request_slots.release()
        for _ in range(control_room_http._MAX_HTTP_REQUESTS):
            first._request_slots.release()
    finally:
        first.server_close()
        second.server_close()


def test_server_preloads_closed_static_fixture_and_historical_models(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _copy_control_room_data(root)
    static_root = tmp_path / "static"
    _write_static_bundle(static_root)
    original_app = (static_root / "app.js").read_bytes()
    server = create_server(root=root, port=0, static_root=static_root, rehearsal_runner=_runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    address = f"{host}:{port}"
    (static_root / "app.js").write_text("tampered", encoding="utf-8")
    (root / "control-room-data/fixtures-v1.json").write_text("{}", encoding="utf-8")
    (root / "control-room-data/historical-phase-b-v1.json").write_text("{}", encoding="utf-8")
    try:
        connection = HTTPConnection(address)
        connection.request("GET", "/app.js", headers={"Host": address})
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == original_app

        connection = HTTPConnection(address)
        connection.request("GET", "/api/v1/scenarios", headers={"Host": address})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["default_scenario_id"] == "small-shop"

        connection = HTTPConnection(address)
        connection.request("GET", "/api/v1/evidence/phase-b", headers={"Host": address})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["report_outcome"] == "ALL_GATES_PASSED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_server_rejects_static_manifest_tamper_and_invalid_fixture_at_startup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _copy_control_room_data(root)
    static_root = tmp_path / "static"
    _write_static_bundle(static_root)
    (static_root / "app.js").write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="static asset"):
        create_server(root=root, port=0, static_root=static_root, rehearsal_runner=_runner)

    (root / "control-room-data/fixtures-v1.json").write_text("{}", encoding="utf-8")
    other_static = tmp_path / "other-static"
    _write_static_bundle(other_static)
    with pytest.raises(RuntimeError, match="fixture"):
        create_server(root=root, port=0, static_root=other_static, rehearsal_runner=_runner)


@pytest.fixture
def api_server(tmp_path: Path) -> tuple[str, Callable[[], None]]:
    static_root = tmp_path / "static"
    _write_static_bundle(static_root)
    server = create_server(
        root=Path.cwd(), port=0, static_root=static_root, rehearsal_runner=_runner
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)

    def close() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"{host}:{port}", close


def test_get_scenarios_and_only_loopback_host(api_server: tuple[str, Callable[[], None]]) -> None:
    address, close = api_server
    try:
        connection = HTTPConnection(address)
        connection.request("GET", "/api/v1/scenarios", headers={"Host": address})
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        assert body["default_scenario_id"] == "small-shop"
        assert response.getheader("Content-Security-Policy") == (
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors "
            "'none'; form-action 'none'; connect-src 'self'; img-src 'self'; style-src "
            "'self'; script-src 'self'"
        )
    finally:
        close()


def test_post_requires_exact_origin_and_passes_only_allowlisted_scenario(
    api_server: tuple[str, Callable[[], None]],
) -> None:
    address, close = api_server
    try:
        payload = b'{"scenario_id":"small-shop"}'
        connection = HTTPConnection(address)
        connection.request(
            "POST",
            "/api/v1/rehearsals",
            body=payload,
            headers={
                "Host": address,
                "Origin": f"http://{address}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        assert body["scenario_id"] == "small-shop"
    finally:
        close()


def test_invalid_fixture_is_a_stable_422_and_runner_is_not_called(
    api_server: tuple[str, Callable[[], None]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    address, close = api_server
    called = False

    def forbidden_runner(_scenario: str) -> Mapping[str, object]:
        nonlocal called
        called = True
        raise AssertionError("runner must not be called")

    # The fixture creates a server with a benign runner; this separate instance tests rejection.
    static_root = tmp_path / "rejected-static"
    _write_static_bundle(static_root)
    server = create_server(
        root=Path.cwd(),
        port=0,
        static_root=static_root,
        rehearsal_runner=forbidden_runner,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    rejected_address = f"{host}:{port}"
    try:
        payload = b'{"scenario_id":"invalid-negative-amount"}'
        connection = HTTPConnection(rejected_address)
        connection.request(
            "POST",
            "/api/v1/rehearsals",
            body=payload,
            headers={
                "Host": rejected_address,
                "Origin": f"http://{rejected_address}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 422
        assert body["verdict"] == "INPUT_REJECTED"
        assert body["error"]["field"] == "amount_cents"
        assert called is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        close()


def test_post_rejects_transfer_encoding_before_runner(
    api_server: tuple[str, Callable[[], None]],
) -> None:
    address, close = api_server
    try:
        connection = HTTPConnection(address)
        connection.request(
            "POST",
            "/api/v1/rehearsals",
            body=b"0\r\n\r\n",
            headers={
                "Host": address,
                "Origin": f"http://{address}",
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"]["code"] == "INVALID_REQUEST"
    finally:
        close()


def test_busy_engine_is_503_not_internal_error(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    _write_static_bundle(static_root)
    server = create_server(
        root=Path.cwd(),
        port=0,
        static_root=static_root,
        rehearsal_runner=lambda _scenario: (_ for _ in ()).throw(EngineBusyError()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    address = f"{host}:{port}"
    try:
        body = b'{"scenario_id":"small-shop"}'
        connection = HTTPConnection(address)
        connection.request(
            "POST",
            "/api/v1/rehearsals",
            body=body,
            headers={
                "Host": address,
                "Origin": f"http://{address}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        assert response.status == 503
        assert json.loads(response.read())["error"]["code"] == "BUSY"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("write_error", "expected_reason"),
    (
        (BrokenPipeError(), "client_disconnected"),
        (OSError("write failed"), "response_write_failed"),
    ),
)
def test_post_delivery_failure_records_the_exact_adapter_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_error: OSError,
    expected_reason: str,
) -> None:
    static_root = tmp_path / "static"
    _write_static_bundle(static_root)
    _copy_control_room_data(tmp_path)
    nonce = "0123456789abcdef"
    recorded: list[tuple[Path, str, str]] = []
    delivered = threading.Event()

    def fail_body(_stream: object, _raw: bytes) -> None:
        raise write_error

    def record(root: Path, run_nonce: str, reason: str) -> None:
        recorded.append((root, run_nonce, reason))
        delivered.set()

    monkeypatch.setattr(control_room_http, "_write_body", fail_body)
    monkeypatch.setattr(control_room_http, "record_delivery_failure", record)
    server = create_server(
        root=tmp_path,
        port=0,
        static_root=static_root,
        rehearsal_runner=lambda scenario: RehearsalResponse(_runner(scenario), nonce),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    address = f"{host}:{port}"
    body = b'{"scenario_id":"small-shop"}'
    try:
        with socket.create_connection((host, port), timeout=2) as client:
            client.sendall(
                (
                    "POST /api/v1/rehearsals HTTP/1.0\r\n"
                    f"Host: {address}\r\nOrigin: http://{address}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode("ascii")
                + body
            )
            client.recv(4096)
        assert delivered.wait(timeout=2)
        assert recorded == [(tmp_path, nonce, expected_reason)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_options_and_head_apply_host_guard_and_security_headers(
    api_server: tuple[str, Callable[[], None]],
) -> None:
    address, close = api_server
    try:
        connection = HTTPConnection(address)
        connection.request("OPTIONS", "/api/v1/scenarios", headers={"Host": address})
        response = connection.getresponse()
        assert response.status == 405
        assert response.getheader("Connection") == "close"
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        response.read()
        connection = HTTPConnection(address)
        connection.request("HEAD", "/", headers={"Host": "example.test"})
        response = connection.getresponse()
        assert response.status == 421
        assert response.getheader("Content-Security-Policy") is not None
    finally:
        close()


def test_raw_duplicate_host_and_pipelining_do_not_reach_a_second_request(
    api_server: tuple[str, Callable[[], None]],
) -> None:
    address, close = api_server
    host, port_text = address.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port_text)), timeout=2) as client:
            client.sendall(
                (
                    f"GET /healthz HTTP/1.0\r\nHost: {address}\r\nHost: attacker.test\r\n\r\n"
                    f"GET /api/v1/scenarios HTTP/1.0\r\nHost: {address}\r\n\r\n"
                ).encode("ascii")
            )
            response = client.recv(4096)
        assert b"421 Misdirected Request" in response
        assert response.count(b"HTTP/1.0") == 1
    finally:
        close()


@pytest.mark.parametrize(
    "raw_request, expected",
    [
        ("GET /%2e%2e/secret HTTP/1.0\r\nHost: {host}\r\n\r\n", b"404 Not Found"),
        ("GET /..%5csecret HTTP/1.0\r\nHost: {host}\r\n\r\n", b"404 Not Found"),
        ("PATCH / HTTP/1.0\r\nHost: {host}\r\n\r\n", b"405 Method Not Allowed"),
        (
            "POST /api/v1/rehearsals HTTP/1.0\r\nHost: {host}\r\n"
            "Origin: http://{host}\r\nContent-Type: application/json\r\n"
            "Content-Length: 1\r\nContent-Length: 1\r\n\r\n{{}}",
            b"400 Bad Request",
        ),
    ],
)
def test_raw_request_matrix_is_closed(
    api_server: tuple[str, Callable[[], None]], raw_request: str, expected: bytes
) -> None:
    address, close = api_server
    host, port_text = address.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port_text)), timeout=2) as client:
            client.sendall(raw_request.format(host=address).encode("ascii"))
            response = client.recv(4096)
        assert expected in response
        assert b"Content-Security-Policy:" in response
        assert b"Connection: close" in response
    finally:
        close()


@pytest.mark.parametrize("method", ("TRACE", "CONNECT", "BREW"))
@pytest.mark.parametrize("host_mode", ("valid", "missing", "duplicate"))
def test_unknown_http_methods_use_closed_json_and_host_gate(
    api_server: tuple[str, Callable[[], None]], method: str, host_mode: str
) -> None:
    address, close = api_server
    host, port_text = address.rsplit(":", 1)
    if host_mode == "valid":
        host_headers = f"Host: {address}\r\n"
        expected = b"405 Method Not Allowed"
    elif host_mode == "missing":
        host_headers = ""
        expected = b"421 Misdirected Request"
    else:
        host_headers = f"Host: {address}\r\nHost: attacker.test\r\n"
        expected = b"421 Misdirected Request"
    try:
        with socket.create_connection((host, int(port_text)), timeout=2) as client:
            client.sendall(f"{method} / HTTP/1.0\r\n{host_headers}\r\n".encode("ascii"))
            response = client.recv(4096)
        assert expected in response
        assert b"Content-Type: application/json; charset=utf-8" in response
        assert b"Content-Security-Policy:" in response
        assert b"Connection: close" in response
        assert b"<!DOCTYPE HTML>" not in response
    finally:
        close()


def test_parser_error_uses_closed_json_and_security_headers(
    api_server: tuple[str, Callable[[], None]],
) -> None:
    address, close = api_server
    host, port_text = address.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port_text)), timeout=2) as client:
            client.sendall(b"NOT A VALID REQUEST\r\n\r\n")
            response = client.recv(4096)
        assert b"400 Bad Request" in response
        assert b"Content-Type: application/json; charset=utf-8" in response
        assert b"Content-Security-Policy:" in response
        assert b"<!DOCTYPE HTML>" not in response
    finally:
        close()
