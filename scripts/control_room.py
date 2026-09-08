#!/usr/bin/env python3
"""Local lifecycle command for the loopback control-room server."""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path
from time import monotonic
from types import FrameType
from typing import NoReturn, Protocol

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".control-room"
STATE_PATH = STATE_DIR / "server.json"
STATIC_ROOT = ROOT / "dist" / "control-room"
_STATE_NAME = "server.json"
_STATE_TEMP_NAME = "server.tmp"
_START_LOCK_NAME = "start.lock"
_STATE_MAX_BYTES = 16_384
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _wait_for_process(process: subprocess.Popen[bytes], *, timeout: float) -> int:
    from migration_rehearsal.control_room_runtime import wait_for_process

    return wait_for_process(process, timeout=timeout)


def _wait_for_retry(*, timeout: float) -> None:
    from migration_rehearsal.control_room_runtime import wait_for_retry

    wait_for_retry(timeout=timeout)


class _OwnedServer(Protocol):
    def serve_forever(self) -> None: ...

    def shutdown(self) -> None: ...

    def server_close(self) -> None: ...


class _StateMissing(RuntimeError):
    """No owned lifecycle state exists in the anchored state directory."""


def _start_ticks(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    _, separator, tail = raw.rpartition(") ")
    fields = tail.split()
    if not separator or len(fields) <= 19:
        raise RuntimeError("process start time is unavailable")
    return fields[19]


def _command_line(pid: int) -> list[str]:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return [part.decode("utf-8") for part in raw.split(b"\0") if part]


def _executable(pid: int) -> str:
    return str(Path(f"/proc/{pid}/exe").resolve(strict=True))


def _open_state_directory(*, create: bool) -> int:
    parent = STATE_DIR.parent
    if STATE_PATH.parent != STATE_DIR:
        raise RuntimeError("control-room state path is outside its owned directory")
    try:
        parent_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise RuntimeError("control-room state parent is unsafe") from error
    try:
        if create:
            try:
                os.mkdir(STATE_DIR.name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        try:
            directory_fd = os.open(
                STATE_DIR.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except FileNotFoundError as error:
            raise _StateMissing("control-room state is absent") from error
        except OSError as error:
            raise RuntimeError("control-room state directory is unsafe") from error
    finally:
        os.close(parent_fd)
    metadata = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_nlink < 1
    ):
        os.close(directory_fd)
        raise RuntimeError("control-room state directory is unsafe")
    return directory_fd


def _state_from_fd(directory_fd: int) -> dict[str, object]:
    try:
        descriptor = os.open(
            _STATE_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except FileNotFoundError as error:
        raise _StateMissing("control-room state is absent") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RuntimeError("control-room state symlink is refused") from error
        raise RuntimeError("control-room state is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= _STATE_MAX_BYTES
        ):
            raise RuntimeError("control-room state file is unsafe")
        chunks: list[bytes] = []
        remaining = _STATE_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _STATE_MAX_BYTES:
            raise RuntimeError("control-room state is too large")
        value = json.loads(raw, object_pairs_hook=_unique_state_object)
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("control-room state is unavailable") from error
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise RuntimeError("control-room state is malformed")
    required = {
        "schema_version",
        "pid",
        "linux_start_ticks",
        "argv",
        "project_root",
        "nonce",
        "executable",
        "port",
    }
    if (
        set(value) != required
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise RuntimeError("control-room state schema is malformed")
    pid = value.get("pid")
    ticks = value.get("linux_start_ticks")
    argv = value.get("argv")
    nonce = value.get("nonce")
    executable = value.get("executable")
    port = value.get("port")
    expected_argv = [
        sys.executable,
        str(ROOT / "scripts/control_room.py"),
        "serve",
        "--port",
        str(port),
        "--instance-nonce",
        nonce,
    ]
    if (
        type(pid) is not int
        or pid <= 0
        or type(ticks) is not int
        or ticks <= 0
        or value.get("project_root") != str(ROOT)
        or argv != expected_argv
        or executable != str(Path(sys.executable).resolve(strict=True))
        or type(port) is not int
        or not 1024 <= port <= 65535
        or not isinstance(nonce, str)
        or len(nonce) != 64
        or any(character not in "0123456789abcdef" for character in nonce)
    ):
        raise RuntimeError("control-room state fields are malformed")
    return value


def _state() -> dict[str, object]:
    directory_fd = _open_state_directory(create=False)
    try:
        return _state_from_fd(directory_fd)
    finally:
        os.close(directory_fd)


def _write_state(state: dict[str, object], *, directory_fd: int | None = None) -> None:
    owned_fd = directory_fd is None
    if directory_fd is None:
        directory_fd = _open_state_directory(create=True)
    try:
        try:
            target = os.stat(_STATE_NAME, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            target = None
        if target is not None:
            if stat.S_ISLNK(target.st_mode):
                raise RuntimeError("control-room state path symlink is refused")
            raise RuntimeError("control-room state already exists")
        try:
            descriptor = os.open(
                _STATE_TEMP_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise RuntimeError("control-room state temporary path is unsafe") from error
        replaced = False
        try:
            raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(raw) > _STATE_MAX_BYTES:
                raise RuntimeError("control-room state is too large")
            offset = 0
            while offset < len(raw):
                offset += os.write(descriptor, raw[offset:])
            os.fsync(descriptor)
            os.replace(
                _STATE_TEMP_NAME,
                _STATE_NAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            replaced = True
            os.fsync(directory_fd)
        finally:
            os.close(descriptor)
            if not replaced:
                try:
                    os.unlink(_STATE_TEMP_NAME, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
    finally:
        if owned_fd:
            os.close(directory_fd)


def _remove_state(directory_fd: int, expected: dict[str, object], *, missing_ok: bool) -> None:
    try:
        current = _state_from_fd(directory_fd)
    except _StateMissing:
        if missing_ok:
            return
        raise
    if current != expected:
        raise RuntimeError("owned control-room state identity drift")
    os.unlink(_STATE_NAME, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _safe_state_directory() -> None:
    directory_fd = _open_state_directory(create=True)
    os.close(directory_fd)


def _unique_state_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate state key")
        result[key] = value
    return result


def _loopback_port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def start(port: int) -> int:
    if not 1024 <= port <= 65535:
        raise RuntimeError("control-room port is outside the local unprivileged range")
    if not STATIC_ROOT.is_dir() or not (STATIC_ROOT / "index.html").is_file():
        raise RuntimeError("control-room production static root is unavailable")
    if not _loopback_port_available(port):
        raise RuntimeError("control-room loopback port is already occupied")
    directory_fd = _open_state_directory(create=True)
    try:
        try:
            _state_from_fd(directory_fd)
        except _StateMissing:
            pass
        else:
            raise RuntimeError(
                "control-room state already exists; use stop only for its owned process"
            )
        try:
            lock_fd = os.open(
                _START_LOCK_NAME,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError as error:
            raise RuntimeError("control-room start is already in progress") from error
        try:
            return _start_owned(port, directory_fd=directory_fd)
        finally:
            os.close(lock_fd)
            os.unlink(_START_LOCK_NAME, dir_fd=directory_fd)
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _start_owned(port: int, *, directory_fd: int) -> int:
    if not hasattr(os, "pidfd_open"):
        raise RuntimeError("Linux pidfd support is required for control-room start")
    nonce = secrets.token_hex(32)
    command = [
        sys.executable,
        str(ROOT / "scripts/control_room.py"),
        "serve",
        "--port",
        str(port),
        "--instance-nonce",
        nonce,
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    try:
        preflight_pidfd = os.pidfd_open(process.pid)
    except OSError as error:
        process.terminate()
        _wait_for_process(process, timeout=2)
        raise RuntimeError("control-room pidfd preflight failed") from error
    else:
        os.close(preflight_pidfd)
    state: dict[str, object] | None = None
    try:
        state = {
            "schema_version": 1,
            "pid": process.pid,
            "linux_start_ticks": int(_start_ticks(process.pid)),
            "argv": command,
            "nonce": nonce,
            "executable": str(Path(sys.executable).resolve(strict=True)),
            "project_root": str(ROOT),
            "port": port,
        }
        _write_state(state, directory_fd=directory_fd)
    except Exception:
        process.terminate()
        _wait_for_process(process, timeout=2)
        if state is not None:
            _remove_state(directory_fd, state, missing_ok=True)
        raise
    deadline = monotonic() + 5
    while monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
            connection.request("GET", "/healthz", headers={"Host": f"127.0.0.1:{port}"})
            response = connection.getresponse()
            payload = json.loads(response.read())
            if response.status == 200 and payload == {
                "schema_version": 1,
                "status": "ok",
                "scope": "loopback_only",
            }:
                break
        except (OSError, json.JSONDecodeError, http.client.HTTPException):
            pass
        _wait_for_retry(timeout=0.05)
    else:
        if process.poll() is None:
            process.terminate()
            _wait_for_process(process, timeout=2)
        if state is not None:
            _remove_state(directory_fd, state, missing_ok=True)
        raise RuntimeError("control-room health check did not become ready")
    print(json.dumps({"status": "started", "port": port}, sort_keys=True))
    return 0


def stop() -> int:
    try:
        directory_fd = _open_state_directory(create=False)
    except _StateMissing:
        print(json.dumps({"status": "already_stopped"}, sort_keys=True))
        return 0
    try:
        try:
            state = _state_from_fd(directory_fd)
        except _StateMissing:
            print(json.dumps({"status": "already_stopped"}, sort_keys=True))
            return 0
        initial_pid = state.get("pid")
        if not isinstance(initial_pid, int):
            raise RuntimeError("control-room state is malformed")
        try:
            pidfd = os.pidfd_open(initial_pid)
        except ProcessLookupError:
            port = state.get("port")
            if not isinstance(port, int) or not _loopback_port_available(port):
                raise RuntimeError(
                    "owned control-room PID is unavailable while its port remains occupied"
                )
            _remove_state(directory_fd, state, missing_ok=False)
            print(json.dumps({"status": "stale_state_removed"}, sort_keys=True))
            return 0
        except (AttributeError, OSError) as error:
            raise RuntimeError("owned control-room pidfd is unavailable") from error
        try:
            state = _state_from_fd(directory_fd)
            pid = state.get("pid")
            ticks = state.get("linux_start_ticks")
            argv = state.get("argv")
            nonce = state.get("nonce")
            executable = state.get("executable")
            if (
                not isinstance(pid, int)
                or not isinstance(ticks, int)
                or not isinstance(argv, list)
                or not isinstance(nonce, str)
                or not isinstance(executable, str)
            ):
                raise RuntimeError("control-room state is malformed")
            if pid != initial_pid:
                raise RuntimeError("owned control-room process identity drift")
            observed = _command_line(pid)
            expected = [str(value) for value in argv]
            try:
                observed_ticks = int(_start_ticks(pid))
            except (OSError, ValueError) as error:
                raise RuntimeError("owned control-room process identity drift") from error
            if (
                observed_ticks != ticks
                or _executable(pid) != executable
                or observed != expected
                or nonce not in observed
            ):
                raise RuntimeError("owned control-room process identity drift")
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            poller = select.poll()
            poller.register(pidfd, select.POLLIN)
            if not poller.poll(140_000):
                raise RuntimeError("owned control-room process did not stop")
            _remove_state(directory_fd, state, missing_ok=False)
            print(json.dumps({"status": "stopped"}, sort_keys=True))
            return 0
        finally:
            os.close(pidfd)
    finally:
        os.close(directory_fd)


def _serve_owned_server(server: _OwnedServer) -> None:
    """Stop accepting on SIGTERM, then wait for non-daemon request cleanup."""
    previous_handler = signal.getsignal(signal.SIGTERM)
    shutdown_started = threading.Event()
    shutdown_thread: threading.Thread | None = None

    def request_shutdown(_signum: int, _frame: FrameType | None) -> None:
        nonlocal shutdown_thread
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        shutdown_thread = threading.Thread(
            target=server.shutdown,
            name="pmr-control-room-shutdown",
            daemon=False,
        )
        shutdown_thread.start()

    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        server.serve_forever()
    finally:
        if shutdown_thread is not None:
            shutdown_thread.join()
        server.server_close()
        signal.signal(signal.SIGTERM, previous_handler)


def serve(port: int, instance_nonce: str) -> NoReturn:
    if len(instance_nonce) != 64 or any(char not in "0123456789abcdef" for char in instance_nonce):
        raise RuntimeError("invalid local instance nonce")
    if not STATIC_ROOT.is_dir() or not (STATIC_ROOT / "index.html").is_file():
        raise RuntimeError("control-room production static root is unavailable")
    from migration_rehearsal.control_room_adapter import run_rehearsal
    from migration_rehearsal.control_room_http import create_server

    server = create_server(
        root=ROOT,
        port=port,
        static_root=STATIC_ROOT,
        rehearsal_runner=lambda scenario_id: run_rehearsal(ROOT, scenario_id),
    )
    _serve_owned_server(server)
    raise SystemExit(0)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--port", type=int, default=8787)
    commands.add_parser("stop")
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--port", type=int, required=True)
    serve_parser.add_argument("--instance-nonce", required=True)
    arguments = parser.parse_args()
    if arguments.command == "start":
        return start(arguments.port)
    if arguments.command == "stop":
        return stop()
    serve(arguments.port, arguments.instance_nonce)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"control-room: {error}", file=sys.stderr)
        raise SystemExit(1) from error
