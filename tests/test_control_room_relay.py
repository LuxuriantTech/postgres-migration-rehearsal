"""Behavioral tests for the fixed loopback-to-owned-container relay."""

from __future__ import annotations

import socket
import threading
from dataclasses import replace

import pytest

import migration_rehearsal.control_room_runtime.relay as relay_module
from migration_rehearsal.control_room_runtime.relay import (
    LoopbackPostgresRelay,
    RelayRuntimeError,
    RuntimeTarget,
)


def _target() -> RuntimeTarget:
    return RuntimeTarget(
        container_id="c" * 64,
        network_id="d" * 64,
        endpoint_id="e" * 64,
        ipv4_address="172.28.0.2",
    )


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_relay_contract_keeps_fixed_transport_bounds() -> None:
    assert relay_module._LISTEN_HOST == "127.0.0.1"
    assert relay_module._LISTEN_PORT == 55432
    assert relay_module._DESTINATION_PORT == 5432
    assert relay_module._MAX_CONNECTIONS == 20


def test_relay_refuses_an_occupied_fixed_loopback_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        port = int(blocker.getsockname()[1])
        blocker.listen(1)
        monkeypatch.setattr(relay_module, "_LISTEN_PORT", port)
        relay = LoopbackPostgresRelay(_target(), _target)

        with pytest.raises(RelayRuntimeError, match="occupied"):
            relay.start()

        relay.close()


def test_relay_start_proves_the_attested_destination_with_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    port = _unused_loopback_port()
    monkeypatch.setattr(relay_module, "_LISTEN_PORT", port)
    probe_side, destination_side = socket.socketpair()
    attempts: list[tuple[str, int]] = []
    revalidations: list[RuntimeTarget] = []

    def revalidate() -> RuntimeTarget:
        revalidations.append(target)
        return target

    def connect(address: tuple[str, int], timeout: float) -> socket.socket:
        attempts.append(address)
        if len(attempts) == 1:
            raise ConnectionRefusedError("route not ready")
        assert timeout == 3.0
        return probe_side

    monkeypatch.setattr(
        "migration_rehearsal.control_room_runtime.relay.socket.create_connection",
        connect,
    )
    monkeypatch.setattr(relay_module, "wait_for_retry", lambda **_kwargs: None, raising=False)
    relay = LoopbackPostgresRelay(target, revalidate)
    try:
        relay.start()
    finally:
        destination_side.close()
        relay.close()

    assert attempts == [("172.28.0.2", 5432), ("172.28.0.2", 5432)]
    assert revalidations == [target, target]
    assert relay.proof()["destination_probe"] == "connected"


def test_relay_forwards_both_directions_to_only_the_attested_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    port = _unused_loopback_port()
    monkeypatch.setattr(relay_module, "_LISTEN_PORT", port)
    revalidations: list[RuntimeTarget] = []
    destinations: list[tuple[tuple[str, int], float]] = []
    probe_side, probe_peer = socket.socketpair()
    relay_side, backend_side = socket.socketpair()
    upstreams = iter((probe_side, relay_side))
    real_create_connection = socket.create_connection

    def revalidate() -> RuntimeTarget:
        revalidations.append(target)
        return target

    def connect(address: tuple[str, int], timeout: float) -> socket.socket:
        destinations.append((address, timeout))
        return next(upstreams)

    monkeypatch.setattr(
        "migration_rehearsal.control_room_runtime.relay.socket.create_connection",
        connect,
    )
    relay = LoopbackPostgresRelay(target, revalidate)
    client: socket.socket | None = None
    try:
        relay.start()
        client = real_create_connection(("127.0.0.1", port), timeout=2)
        client.settimeout(2)
        backend_side.settimeout(2)
        client.sendall(b"client-to-postgres")
        assert backend_side.recv(64) == b"client-to-postgres"
        backend_side.sendall(b"postgres-to-client")
        assert client.recv(64) == b"postgres-to-client"
    finally:
        if client is not None:
            client.close()
        probe_peer.close()
        backend_side.close()
        relay.close()

    relay.assert_healthy()
    relay.assert_destination_observed()
    assert revalidations == [target, target]
    assert destinations == [
        (("172.28.0.2", 5432), 3.0),
        (("172.28.0.2", 5432), 3.0),
    ]
    assert relay.proof() == {
        "listen": f"127.0.0.1:{port}",
        "destination": "172.28.0.2:5432",
        "destination_probe": "connected",
        "destination_connections": 1,
        "max_connections": 20,
        "io_idle_timeout_seconds": 20.0,
    }
    assert not any(thread.name.startswith("pmr-relay-") for thread in threading.enumerate())


def test_relay_revalidates_identity_before_opening_the_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    port = _unused_loopback_port()
    monkeypatch.setattr(relay_module, "_LISTEN_PORT", port)
    revalidated = threading.Event()
    connector_called = False

    def changed_target() -> RuntimeTarget:
        revalidated.set()
        return replace(target, endpoint_id="f" * 64)

    def forbidden_connect(_address: tuple[str, int], _timeout: float) -> socket.socket:
        nonlocal connector_called
        connector_called = True
        raise AssertionError("identity drift must be rejected before destination connect")

    monkeypatch.setattr(
        "migration_rehearsal.control_room_runtime.relay.socket.create_connection",
        forbidden_connect,
    )
    relay = LoopbackPostgresRelay(target, changed_target)
    try:
        with pytest.raises(RelayRuntimeError, match="identity"):
            relay.start()
    finally:
        relay.close()

    assert revalidated.is_set()
    assert connector_called is False
    assert not any(thread.name.startswith("pmr-relay-") for thread in threading.enumerate())


def test_relay_close_owns_active_sockets_and_joins_all_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    port = _unused_loopback_port()
    monkeypatch.setattr(relay_module, "_LISTEN_PORT", port)
    probe_side, probe_peer = socket.socketpair()
    relay_side, backend_side = socket.socketpair()
    upstreams = iter((probe_side, relay_side))
    real_create_connection = socket.create_connection

    monkeypatch.setattr(
        "migration_rehearsal.control_room_runtime.relay.socket.create_connection",
        lambda _address, timeout: next(upstreams),
    )
    relay = LoopbackPostgresRelay(target, lambda: target)
    client: socket.socket | None = None
    try:
        relay.start()
        client = real_create_connection(("127.0.0.1", port), timeout=2)
        client.sendall(b"connection-is-active")
        backend_side.settimeout(2)
        assert backend_side.recv(64) == b"connection-is-active"

        relay.close()
    finally:
        if client is not None:
            client.close()
        probe_peer.close()
        backend_side.close()
        relay.close()

    assert not any(thread.name.startswith("pmr-relay-") for thread in threading.enumerate())
