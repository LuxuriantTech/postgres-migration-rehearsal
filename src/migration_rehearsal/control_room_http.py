"""Strict small HTTP/1.0 loopback service for the migration control room."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Protocol, cast

from migration_rehearsal.control_room_adapter import (
    EngineBusyError,
    FixtureValidationError,
    RehearsalResponse,
    historical_evidence,
    record_delivery_failure,
    scenario_summaries,
)

Runner = Callable[[str], Mapping[str, object]]
_CSP: Final = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors "
    "'none'; form-action 'none'; connect-src 'self'; img-src 'self'; style-src "
    "'self'; script-src 'self'"
)
_STATIC: Final = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.css": "app.css",
    "/app.js": "app.js",
    "/favicon.svg": "favicon.svg",
    "/site.webmanifest": "site.webmanifest",
}
_ASSETS: Final = ("app.css", "app.js", "favicon.svg", "index.html", "site.webmanifest")
_MIMES: Final = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}
_MAX_HTTP_REQUESTS: Final = 8


class _BodyWriter(Protocol):
    def write(self, value: bytes) -> object: ...


def _write_body(stream: _BodyWriter, raw: bytes) -> None:
    stream.write(raw)


def _error(
    code: str, title: str, detail: str, recovery: str, *, cleanup: str = "not_needed"
) -> Mapping[str, object]:
    return {
        "schema_version": 1,
        "cleanup_state": cleanup,
        "error": {"code": code, "title": title, "detail": detail, "recovery": recovery},
    }


def _busy_response() -> bytes:
    body = json.dumps(
        _error(
            "BUSY",
            "Local rehearsal busy",
            "Wait for the current local request to finish.",
            "Try again after the current local request finishes.",
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = (
        "HTTP/1.0 503 Service Unavailable\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        f"Content-Security-Policy: {_CSP}\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Referrer-Policy: no-referrer\r\n"
        "Permissions-Policy: accelerometer=(), camera=(), geolocation=(), microphone=()\r\n"
        "Cross-Origin-Opener-Policy: same-origin\r\n"
        "Cache-Control: no-store\r\n"
        "\r\n"
    ).encode("ascii")
    return headers + body


_HTTP_BUSY_RESPONSE: Final = _busy_response()


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = _MAX_HTTP_REQUESTS

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
    ) -> None:
        self._request_slots = threading.BoundedSemaphore(_MAX_HTTP_REQUESTS)
        super().__init__(server_address, handler_class)

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        """Admit before ThreadingMixIn creates a handler thread or parses bytes."""
        if not self._request_slots.acquire(blocking=False):
            connection = request[1] if isinstance(request, tuple) else request
            try:
                connection.settimeout(5)
                connection.sendall(_HTTP_BUSY_RESPONSE)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            try:
                thread_registry = vars(self).get("_threads")
                reap = getattr(thread_registry, "reap", None)
                if callable(reap):
                    reap()
            finally:
                self._request_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def create_server(
    *, root: Path, port: int, static_root: Path, rehearsal_runner: Runner
) -> ThreadingHTTPServer:
    """Create a loopback-only server; the runner receives only a validated enum."""
    absolute_static_root = static_root.absolute()
    if (
        static_root.is_symlink()
        or not static_root.is_dir()
        or static_root.resolve(strict=True) != absolute_static_root
    ):
        raise RuntimeError("control-room static asset root is unavailable")
    manifest_path = static_root / "asset-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("control-room static asset manifest is unavailable")
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, ValueError) as error:
        raise RuntimeError("control-room static asset manifest is invalid") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "files"}
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("files"), list)
        or len(manifest["files"]) != len(_ASSETS)
    ):
        raise RuntimeError("control-room static asset manifest is invalid")
    if {path.name for path in static_root.iterdir()} != {*_ASSETS, "asset-manifest.json"}:
        raise RuntimeError("control-room static asset set is invalid")

    preloaded_static: dict[str, bytes] = {}
    for expected_name, entry in zip(_ASSETS, manifest["files"], strict=True):
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256", "size"}
            or entry.get("path") != expected_name
            or type(entry.get("size")) is not int
            or cast(int, entry["size"]) < 0
            or not isinstance(entry.get("sha256"), str)
        ):
            raise RuntimeError("control-room static asset manifest is invalid")
        asset = expected_name
        path = static_root / asset
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("control-room static asset is unavailable")
        raw = path.read_bytes()
        if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise RuntimeError("control-room static asset failed manifest validation")
        preloaded_static[asset] = raw

    preloaded_scenarios = scenario_summaries(root)
    try:
        preloaded_historical: Mapping[str, object] | None = historical_evidence(root)
    except RuntimeError:
        preloaded_historical = None
    run_slot = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = ""
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def __getattr__(self, name: str) -> object:
            if name.startswith("do_"):
                return self._unknown_method
            raise AttributeError(name)

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            """Replace inherited reflective HTML for request-parser failures."""
            del message, explain
            self.request_version = "HTTP/1.0"
            self.close_connection = True
            status = HTTPStatus.BAD_REQUEST
            try:
                candidate = HTTPStatus(code)
            except ValueError:
                candidate = HTTPStatus.BAD_REQUEST
            if 400 <= candidate.value < 500:
                status = candidate
            self._send_json(
                status,
                _error(
                    "INVALID_REQUEST",
                    "Invalid request",
                    "The local HTTP request could not be accepted.",
                    "Reload the control room.",
                ),
            )

        @property
        def _expected_host(self) -> str:
            address = cast(tuple[str, int], self.server.server_address)
            return f"127.0.0.1:{address[1]}"

        def _critical(self, name: str) -> list[str]:
            return self.headers.get_all(name, failobj=[])

        def _valid_host(self) -> bool:
            return self._critical("Host") == [self._expected_host]

        def _send_json(self, status: HTTPStatus, payload: Mapping[str, object]) -> None:
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self._security_headers("no-store")
            self.end_headers()
            if self.command != "HEAD":
                _write_body(self.wfile, raw)

        def _security_headers(self, cache: str) -> None:
            self.send_header("Connection", "close")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Permissions-Policy", "accelerometer=(), camera=(), geolocation=(), microphone=()"
            )
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cache-Control", cache)

        def _reject(self, status: HTTPStatus, code: str, title: str, detail: str) -> None:
            self._send_json(
                status, _error(code, title, detail, "Reset the control room and try again.")
            )

        def _request_safe(self) -> bool:
            if not self._valid_host():
                self._reject(
                    HTTPStatus.MISDIRECTED_REQUEST,
                    "REQUEST_HOST_REJECTED",
                    "Local host required",
                    "This control room accepts only its exact loopback host.",
                )
                return False
            if self.headers.get("Transfer-Encoding") is not None:
                self._reject(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_REQUEST",
                    "Unsupported request framing",
                    "Use a normal local browser request.",
                )
                return False
            return True

        def do_GET(self) -> None:
            if not self._request_safe():
                return
            if self.headers.get("Content-Type") is not None:
                self._reject(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_REQUEST",
                    "Unexpected content type",
                    "Reload the control room.",
                )
                return
            if self.path == "/healthz":
                self._send_json(
                    HTTPStatus.OK, {"schema_version": 1, "status": "ok", "scope": "loopback_only"}
                )
                return
            if self.path == "/api/v1/scenarios":
                self._send_json(HTTPStatus.OK, preloaded_scenarios)
                return
            if self.path == "/api/v1/evidence/phase-b":
                if preloaded_historical is None:
                    self._reject(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "HISTORICAL_EVIDENCE_UNAVAILABLE",
                        "Historical evidence unavailable",
                        "Use the local rehearsal instead.",
                    )
                else:
                    self._send_json(HTTPStatus.OK, preloaded_historical)
                return
            asset = _STATIC.get(self.path)
            if asset is None:
                self._reject(
                    HTTPStatus.NOT_FOUND,
                    "INVALID_REQUEST",
                    "Route not found",
                    "Return to the control room home page.",
                )
                return
            raw = preloaded_static[asset]
            self.send_response_only(HTTPStatus.OK)
            self.send_header("Content-Type", _MIMES[Path(asset).suffix])
            self.send_header("Content-Length", str(len(raw)))
            self._security_headers("no-cache")
            self.end_headers()
            _write_body(self.wfile, raw)

        def do_POST(self) -> None:
            if not self._request_safe():
                return
            if self.path != "/api/v1/rehearsals":
                self._reject(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "Method not allowed",
                    "Use the available local controls.",
                )
                return
            if self._critical("Content-Type") != ["application/json"]:
                self._reject(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "UNSUPPORTED_MEDIA_TYPE",
                    "JSON required",
                    "Use the control room form.",
                )
                return
            if self._critical("Origin") != [f"http://{self._expected_host}"]:
                self._reject(
                    HTTPStatus.FORBIDDEN,
                    "REQUEST_ORIGIN_REJECTED",
                    "Local origin required",
                    "Open this control room directly.",
                )
                return
            sites = self._critical("Sec-Fetch-Site")
            if sites not in ([], ["same-origin"]):
                self._reject(
                    HTTPStatus.FORBIDDEN,
                    "REQUEST_ORIGIN_REJECTED",
                    "Local origin required",
                    "Open this control room directly.",
                )
                return
            lengths = self._critical("Content-Length")
            if len(lengths) != 1 or not lengths[0].isdigit():
                self._reject(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_REQUEST",
                    "Invalid request length",
                    "Use the control room form.",
                )
                return
            length = int(lengths[0])
            if length > 4096:
                self._reject(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "BODY_TOO_LARGE",
                    "Request is too large",
                    "Choose one synthetic scenario.",
                )
                return
            raw = self.rfile.read(length)
            try:
                request = json.loads(raw, object_pairs_hook=_unique_object)
            except (ValueError, json.JSONDecodeError):
                self._reject(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_REQUEST",
                    "Invalid request",
                    "Choose one synthetic scenario.",
                )
                return
            if (
                not isinstance(request, dict)
                or set(request) != {"scenario_id"}
                or not isinstance(request.get("scenario_id"), str)
            ):
                self._reject(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_REQUEST",
                    "Invalid request",
                    "Choose one synthetic scenario.",
                )
                return
            scenario_id = request["scenario_id"]
            if scenario_id not in {"small-shop", "empty-ledger", "invalid-negative-amount"}:
                self._reject(
                    HTTPStatus.NOT_FOUND,
                    "SCENARIO_NOT_FOUND",
                    "Scenario not found",
                    "Choose a listed synthetic scenario.",
                )
                return
            if scenario_id == "invalid-negative-amount":
                payload = {
                    "schema_version": 1,
                    "verdict": "INPUT_REJECTED",
                    "cleanup_state": "not_needed",
                    "error": {
                        "code": "INPUT_REJECTED",
                        "title": "Input rejected",
                        "detail": "Amount must be a positive whole number of cents.",
                        "recovery": "Choose another synthetic scenario or reset.",
                        "field": "amount_cents",
                    },
                }
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, payload)
                return
            if not run_slot.acquire(blocking=False):
                self._reject(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "BUSY",
                    "Local rehearsal busy",
                    "Wait for the current local rehearsal to finish.",
                )
                return
            try:
                try:
                    result = rehearsal_runner(scenario_id)
                except EngineBusyError:
                    self._reject(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "BUSY",
                        "Local rehearsal busy",
                        "Wait for the current local rehearsal to finish.",
                    )
                    return
                except FixtureValidationError as error:
                    payload = {
                        "schema_version": 1,
                        "verdict": "INPUT_REJECTED",
                        "cleanup_state": "not_needed",
                        "error": {
                            "code": "INPUT_REJECTED",
                            "title": "Input rejected",
                            "detail": error.detail,
                            "recovery": "Choose another synthetic scenario or reset.",
                            "field": error.field,
                        },
                    }
                    self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, payload)
                    return
                except Exception:
                    self._reject(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "INTERNAL_ERROR",
                        "Local rehearsal unavailable",
                        "Reset the control room and try again.",
                    )
                    return
                try:
                    self._send_json(HTTPStatus.OK, result)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                    if isinstance(result, RehearsalResponse):
                        record_delivery_failure(root, result.cleanup_nonce, "client_disconnected")
                except OSError:
                    self.close_connection = True
                    if isinstance(result, RehearsalResponse):
                        record_delivery_failure(root, result.cleanup_nonce, "response_write_failed")
            finally:
                run_slot.release()

        def do_HEAD(self) -> None:
            if not self._request_safe():
                return
            self._reject(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "METHOD_NOT_ALLOWED",
                "Method not allowed",
                "Use the available local controls.",
            )

        def do_PUT(self) -> None:
            self.do_HEAD()

        def do_DELETE(self) -> None:
            self.do_HEAD()

        def do_PATCH(self) -> None:
            self.do_HEAD()

        def do_OPTIONS(self) -> None:
            if not self._request_safe():
                return
            self._reject(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "METHOD_NOT_ALLOWED",
                "Method not allowed",
                "Use the available local controls.",
            )

        def _unknown_method(self) -> None:
            if not self._request_safe():
                return
            self._reject(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "METHOD_NOT_ALLOWED",
                "Method not allowed",
                "Use the available local controls.",
            )

    return _Server(("127.0.0.1", port), Handler)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
