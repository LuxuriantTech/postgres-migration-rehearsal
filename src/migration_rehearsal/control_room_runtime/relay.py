"""Owned loopback relay to one attested container endpoint.

The browser cannot configure this transport.  Its listener, destination port,
connection limit, and deadlines are fixed constants; only the container IPv4
address comes from the adapter's exact Docker identity attestation.
"""

from __future__ import annotations

import ipaddress
import re
import select
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import BoundedSemaphore, Event, Lock, Thread, current_thread
from time import monotonic
from typing import Final

from migration_rehearsal.control_room_runtime.sync import wait_for_retry

_LISTEN_HOST: Final = "127.0.0.1"
_LISTEN_PORT: Final = 55432
_DESTINATION_PORT: Final = 5432
_CONNECT_TIMEOUT_SECONDS: Final = 3.0
_DESTINATION_READY_TIMEOUT_SECONDS: Final = 5.0
_DESTINATION_RETRY_SECONDS: Final = 0.1
_IO_IDLE_TIMEOUT_SECONDS: Final = 20.0
_SELECT_INTERVAL_SECONDS: Final = 0.25
_MAX_CONNECTIONS: Final = 20
_MAX_BUFFER_BYTES: Final = 1_048_576
_CHUNK_BYTES: Final = 65_536
_RESOURCE_ID_RE: Final = re.compile(r"[0-9a-f]{64}")


class RelayRuntimeError(RuntimeError):
    """The fixed relay could not preserve its closed runtime contract."""


@dataclass(frozen=True)
class RuntimeTarget:
    """Container endpoint identity obtained only from Docker inspection."""

    container_id: str
    network_id: str
    endpoint_id: str
    ipv4_address: str

    def __post_init__(self) -> None:
        if any(
            _RESOURCE_ID_RE.fullmatch(value) is None
            for value in (self.container_id, self.network_id, self.endpoint_id)
        ):
            raise ValueError("relay target resource identity is invalid")
        try:
            address = ipaddress.ip_address(self.ipv4_address)
        except ValueError as error:
            raise ValueError("relay target address is invalid") from error
        if (
            not isinstance(address, ipaddress.IPv4Address)
            or not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValueError("relay target must be a private container IPv4 address")


class LoopbackPostgresRelay:
    """Bounded concurrent relay owned by one adapter invocation."""

    def __init__(
        self,
        target: RuntimeTarget,
        revalidate: Callable[[], RuntimeTarget],
    ) -> None:
        self._target = target
        self._revalidate = revalidate
        self._stop = Event()
        self._capacity = BoundedSemaphore(_MAX_CONNECTIONS)
        self._lock = Lock()
        self._listener: socket.socket | None = None
        self._accept_thread: Thread | None = None
        self._workers: set[Thread] = set()
        self._sockets: set[socket.socket] = set()
        self._failures: list[RelayRuntimeError] = []
        self._destination_probe = False
        self._destination_connections = 0
        self._started = False
        self._closed = False

    def start(self) -> None:
        """Bind the exclusive fixed loopback listener before any child starts."""
        if self._started or self._closed:
            raise RelayRuntimeError("loopback relay lifecycle is invalid")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((_LISTEN_HOST, _LISTEN_PORT))
            listener.listen(_MAX_CONNECTIONS)
            listener.settimeout(_SELECT_INTERVAL_SECONDS)
            if listener.getsockname() != (_LISTEN_HOST, _LISTEN_PORT):
                raise RelayRuntimeError("loopback relay bound an unexpected socket")
            self._prove_destination_available()
        except OSError as error:
            listener.close()
            raise RelayRuntimeError(
                "fixed loopback relay port is occupied or unavailable"
            ) from error
        except BaseException:
            listener.close()
            raise
        self._listener = listener
        self._started = True
        thread = Thread(
            target=self._accept_connections,
            name=f"pmr-relay-accept-{self._target.container_id[:12]}",
            daemon=False,
        )
        self._accept_thread = thread
        thread.start()

    def close(self) -> None:
        """Close every owned socket and join every owned thread."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        listener = self._listener
        if listener is not None:
            self._close_socket(listener)
        accept_thread = self._accept_thread
        if accept_thread is not None:
            accept_thread.join()
        with self._lock:
            sockets = tuple(self._sockets)
        for connection in sockets:
            self._close_socket(connection)
        while True:
            with self._lock:
                workers = tuple(self._workers)
            if not workers:
                break
            for worker in workers:
                worker.join()
        self._listener = None
        self._accept_thread = None

    def assert_healthy(self) -> None:
        """Surface a worker identity or transport failure to the adapter."""
        with self._lock:
            failure = self._failures[0] if self._failures else None
        if failure is not None:
            raise RelayRuntimeError(str(failure)) from failure

    def assert_destination_observed(self) -> None:
        """Require at least one successfully opened attested destination."""
        with self._lock:
            probe = self._destination_probe
            connections = self._destination_connections
        if not probe or connections < 1:
            raise RelayRuntimeError("owned relay did not observe its attested destination")

    def proof(self) -> Mapping[str, object]:
        """Return non-sensitive fixed transport facts for local evidence."""
        with self._lock:
            probe = self._destination_probe
            connections = self._destination_connections
        return {
            "listen": f"{_LISTEN_HOST}:{_LISTEN_PORT}",
            "destination": f"{self._target.ipv4_address}:{_DESTINATION_PORT}",
            "destination_probe": "connected" if probe else "not_connected",
            "destination_connections": connections,
            "max_connections": _MAX_CONNECTIONS,
            "io_idle_timeout_seconds": _IO_IDLE_TIMEOUT_SECONDS,
        }

    def _prove_destination_available(self) -> None:
        deadline = monotonic() + _DESTINATION_READY_TIMEOUT_SECONDS
        while True:
            try:
                current_target = self._revalidate()
            except Exception as error:
                raise RelayRuntimeError(
                    "owned runtime identity could not be revalidated before destination probe"
                ) from error
            if current_target != self._target:
                raise RelayRuntimeError(
                    "owned runtime identity changed before relay destination probe"
                )
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RelayRuntimeError("owned relay destination was not reachable before deadline")
            try:
                destination = socket.create_connection(
                    (self._target.ipv4_address, _DESTINATION_PORT),
                    timeout=min(_CONNECT_TIMEOUT_SECONDS, remaining),
                )
            except (OSError, TimeoutError) as error:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise RelayRuntimeError(
                        "owned relay destination was not reachable before deadline"
                    ) from error
                wait_for_retry(timeout=min(_DESTINATION_RETRY_SECONDS, remaining))
                continue
            self._close_socket(destination)
            with self._lock:
                self._destination_probe = True
            return

    def _accept_connections(self) -> None:
        listener = self._listener
        if listener is None:
            self._record_failure(RelayRuntimeError("loopback relay listener is unavailable"))
            return
        while not self._stop.is_set():
            try:
                client, peer = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._stop.is_set():
                    self._record_failure(RelayRuntimeError("loopback relay accept failed"))
                return
            if peer[0] != _LISTEN_HOST or not self._capacity.acquire(blocking=False):
                self._close_socket(client)
                continue
            worker = Thread(
                target=self._serve_connection,
                args=(client,),
                name=f"pmr-relay-client-{self._target.container_id[:12]}",
                daemon=False,
            )
            with self._lock:
                self._workers.add(worker)
            try:
                worker.start()
            except RuntimeError:
                with self._lock:
                    self._workers.discard(worker)
                self._capacity.release()
                self._close_socket(client)
                self._record_failure(RelayRuntimeError("loopback relay worker could not start"))
                if not self._stop.is_set():
                    self._stop.set()
                return

    def _serve_connection(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        self._register_socket(client)
        try:
            if self._stop.is_set():
                return
            try:
                current_target = self._revalidate()
            except Exception as error:
                raise RelayRuntimeError(
                    "owned runtime identity could not be revalidated before destination connect"
                ) from error
            if current_target != self._target:
                raise RelayRuntimeError(
                    "owned runtime identity changed before relay destination connect"
                )
            if self._stop.is_set():
                return
            upstream = socket.create_connection(
                (self._target.ipv4_address, _DESTINATION_PORT),
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
            self._register_socket(upstream)
            with self._lock:
                self._destination_connections += 1
            self._forward(client, upstream)
        except RelayRuntimeError as error:
            if not self._stop.is_set():
                self._record_failure(error)
        except (OSError, TimeoutError):
            if not self._stop.is_set():
                self._record_failure(RelayRuntimeError("owned relay transport failed"))
        except Exception:
            if not self._stop.is_set():
                self._record_failure(RelayRuntimeError("owned relay worker failed"))
        finally:
            if upstream is not None:
                self._unregister_and_close(upstream)
            self._unregister_and_close(client)
            self._capacity.release()
            with self._lock:
                self._workers.discard(current_thread())

    def _forward(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = (client, upstream)
        peers = {client: upstream, upstream: client}
        buffers = {client: bytearray(), upstream: bytearray()}
        readers = {client, upstream}
        write_shutdown: set[socket.socket] = set()
        for connection in sockets:
            connection.setblocking(False)
        last_activity = monotonic()
        while not self._stop.is_set():
            for destination in sockets:
                source = peers[destination]
                if (
                    source not in readers
                    and not buffers[destination]
                    and destination not in write_shutdown
                ):
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    write_shutdown.add(destination)
            if not readers and not any(buffers.values()):
                return
            remaining = _IO_IDLE_TIMEOUT_SECONDS - (monotonic() - last_activity)
            if remaining <= 0:
                raise RelayRuntimeError("owned relay I/O idle deadline expired")
            read_list = [
                source for source in readers if len(buffers[peers[source]]) < _MAX_BUFFER_BYTES
            ]
            write_list = [destination for destination in sockets if buffers[destination]]
            try:
                readable, writable, _ = select.select(
                    read_list,
                    write_list,
                    [],
                    min(_SELECT_INTERVAL_SECONDS, remaining),
                )
            except (OSError, ValueError):
                if self._stop.is_set():
                    return
                raise
            for source in readable:
                try:
                    chunk = source.recv(_CHUNK_BYTES)
                except (BlockingIOError, InterruptedError):
                    continue
                except (ConnectionError, OSError):
                    return
                if not chunk:
                    readers.discard(source)
                    continue
                buffers[peers[source]].extend(chunk)
                last_activity = monotonic()
            for destination in writable:
                try:
                    sent = destination.send(buffers[destination])
                except (BlockingIOError, InterruptedError):
                    continue
                except (ConnectionError, OSError):
                    return
                if sent <= 0:
                    return
                del buffers[destination][:sent]
                last_activity = monotonic()

    def _register_socket(self, connection: socket.socket) -> None:
        with self._lock:
            self._sockets.add(connection)

    def _unregister_and_close(self, connection: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(connection)
        self._close_socket(connection)

    def _record_failure(self, error: RelayRuntimeError) -> None:
        with self._lock:
            if not self._failures:
                self._failures.append(error)

    @staticmethod
    def _close_socket(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()
