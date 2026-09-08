"""Tests for fixture validation and the adapter's real-interface boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import migration_rehearsal.control_room_adapter as adapter
from migration_rehearsal.control_room_adapter import (
    FixtureValidationError,
    RuntimeIdentityError,
    historical_evidence,
    load_scenarios,
    verify_started_runtime,
)

_CONTAINER_ID = "c" * 64
_NETWORK_ID = "d" * 64
_ENDPOINT_ID = "e" * 64


class _SuccessfulRelay:
    def assert_healthy(self) -> None:
        pass

    def assert_destination_observed(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_versioned_fixtures_are_bounded_and_invalid_is_rejected() -> None:
    scenarios = load_scenarios(Path.cwd())
    assert tuple(scenarios) == ("small-shop", "empty-ledger", "invalid-negative-amount")
    happy_rows = scenarios["small-shop"]["rows"]
    empty_rows = scenarios["empty-ledger"]["rows"]
    assert isinstance(happy_rows, list) and len(happy_rows) == 6
    assert empty_rows == []
    with pytest.raises(FixtureValidationError) as captured:
        load_scenarios(Path.cwd(), scenario_id="invalid-negative-amount")
    assert captured.value.field == "amount_cents"


def test_selected_invalid_fixture_records_validation_rejected_without_compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixtures = json.loads(
        (Path.cwd() / "control-room-data/fixtures-v1.json").read_text(encoding="utf-8")
    )
    _write_versioned_asset(tmp_path, "fixtures-v1.json", fixtures)
    monkeypatch.setattr(secrets, "token_hex", lambda _length: "0" * 16)
    monkeypatch.setattr(
        adapter,
        "_owned_compose_runtime",
        lambda *_args: pytest.fail("validation rejection must not start Compose"),
    )

    with pytest.raises(FixtureValidationError):
        adapter.run_rehearsal(tmp_path, "invalid-negative-amount")

    receipt = json.loads((tmp_path / ".control-room/last-cleanup.json").read_text(encoding="utf-8"))
    assert receipt == {
        "schema_version": 1,
        "run_nonce": "0" * 16,
        "cleanup_state": "not_needed",
        "reason": "validation_rejected",
        "remaining_owned_resources": 0,
        "child_exit": "not_started",
    }


def test_historical_manifest_is_tracked_display_data_with_known_digest() -> None:
    evidence = historical_evidence(Path.cwd())
    assert evidence["rerunnable"] is False
    assert evidence["label"] == "Historical evidence — no rerun available"
    assert evidence["independent_review_status"] == "CONFIRMED"
    assert evidence["result_sha256"] == (
        "12f7361a8111e90021930f8e5231bac554c25c8f9636e56ec5116365a5c95347"
    )


def _write_versioned_asset(tmp_path: Path, name: str, value: object) -> None:
    directory = tmp_path / "control-room-data"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(json.dumps(value), encoding="utf-8")


def test_fixture_document_rejects_nested_schema_and_declared_outcome_drift(
    tmp_path: Path,
) -> None:
    source = json.loads(
        (Path.cwd() / "control-room-data/fixtures-v1.json").read_text(encoding="utf-8")
    )
    mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
        lambda value: value.update({"schema_version": True}),
        lambda value: value["source"].update({"authoring_method": 7}),
        lambda value: value["scenarios"][0].update({"title": ""}),
        lambda value: value["scenarios"][0].update({"expected_outcome": "empty"}),
        lambda value: value["scenarios"][0]["rows"][0].update({"amount_cents": True}),
        lambda value: value["scenarios"][2].update({"rows": []}),
    )
    for mutate in mutations:
        invalid = copy.deepcopy(source)
        mutate(invalid)
        _write_versioned_asset(tmp_path, "fixtures-v1.json", invalid)
        with pytest.raises(RuntimeError, match="fixture"):
            load_scenarios(tmp_path)


def test_historical_document_rejects_nested_types_and_reference_drift(tmp_path: Path) -> None:
    source = json.loads(
        (Path.cwd() / "control-room-data/historical-phase-b-v1.json").read_text(encoding="utf-8")
    )
    mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
        lambda value: value.update({"source_commit": "0" * 40}),
        lambda value: value.update({"report_sha256": "0" * 64}),
        lambda value: value["summary"].update({"tests_passed": True}),
        lambda value: value["summary"].update({"extra": 1}),
        lambda value: value.update({"limits": {"not": "a list"}}),
    )
    for mutate in mutations:
        invalid = copy.deepcopy(source)
        mutate(invalid)
        _write_versioned_asset(tmp_path, "historical-phase-b-v1.json", invalid)
        with pytest.raises(RuntimeError, match="historical"):
            historical_evidence(tmp_path)


def test_started_runtime_requires_exact_container_image_labels_and_internal_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {
        "container_id": _CONTAINER_ID,
        "network_id": _NETWORK_ID,
        "endpoint_id": _ENDPOINT_ID,
        "image_id": "sha256:e4155efcf0c7e302168f98a9db891ff4ed14ff3209244308533332d30cd7f6ee",
        "project": "pmr-control-room-0123456789abcdef",
        "service": "postgres",
        "network_internal": True,
        "container_ipv4": "172.28.0.2",
        "destination_port": 5432,
        "published_ports": [],
    }
    assert verify_started_runtime(observed, "pmr-control-room-0123456789abcdef") == _CONTAINER_ID
    observed["published_ports"] = ["127.0.0.1:55432->5432/tcp"]
    with pytest.raises(RuntimeIdentityError, match="publish"):
        verify_started_runtime(observed, "pmr-control-room-0123456789abcdef")


def test_partial_compose_up_runs_compensating_down_and_writes_closed_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshots = iter((frozenset(), frozenset({"container:owned"}), frozenset()))
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(adapter, "_verify_local_runtime", lambda _root: None)
    monkeypatch.setattr(adapter, "_resource_ids", lambda _root, _project: next(snapshots))
    monkeypatch.setattr(adapter, "_started_runtime", lambda *_args: "owned")

    def fake_checked(command: list[str], **_kwargs: object) -> str:
        commands.append(tuple(command))
        if command[-1] == "60":
            raise RuntimeError("partial up failure")
        return ""

    monkeypatch.setattr(adapter, "_checked", fake_checked)
    with pytest.raises(RuntimeError, match="partial up failure"):
        with adapter._owned_compose_runtime(tmp_path, "0123456789abcdef"):
            raise AssertionError("partial up must not yield")
    assert any(command[-3:] == ("down", "--remove-orphans", "--volumes") for command in commands)
    receipt = (tmp_path / ".control-room" / "last-cleanup.json").read_text(encoding="utf-8")
    assert '"cleanup_state":"complete"' in receipt
    assert '"reason":"spawn_failed"' in receipt
    assert '"child_exit":"not_started"' in receipt
    assert '"remaining_owned_resources":0' in receipt


def test_partial_identity_drift_never_runs_compose_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshots = iter((frozenset(), frozenset({"container:unknown"})))
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(adapter, "_verify_local_runtime", lambda _root: None)
    monkeypatch.setattr(adapter, "_resource_ids", lambda _root, _project: next(snapshots))
    monkeypatch.setattr(
        adapter,
        "_started_runtime",
        lambda *_args: (_ for _ in ()).throw(RuntimeIdentityError("drift")),
    )

    def fail_after_recording(command: list[str], **_kwargs: object) -> str:
        commands.append(tuple(command))
        raise RuntimeError("partial up failure")

    monkeypatch.setattr(adapter, "_checked", fail_after_recording)
    with pytest.raises(RuntimeError, match="identity drift"):
        with adapter._owned_compose_runtime(tmp_path, "0123456789abcdef"):
            raise AssertionError("partial up must not yield")
    assert not any("down" in command for command in commands)
    receipt = (tmp_path / ".control-room" / "last-cleanup.json").read_text(encoding="utf-8")
    assert '"cleanup_state":"blocked_identity_drift"' in receipt
    assert '"reason":"identity_drift"' in receipt


def test_relay_start_failure_cleans_the_structurally_owned_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owned = frozenset({f"container:{_CONTAINER_ID}", f"network:{_NETWORK_ID}"})
    snapshots = iter((frozenset(), owned, owned, frozenset()))
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(adapter, "_verify_local_runtime", lambda _root: None)
    monkeypatch.setattr(adapter, "_resource_ids", lambda *_args: next(snapshots))
    monkeypatch.setattr(adapter, "_started_runtime", lambda *_args: {"structural": "owned"})
    monkeypatch.setattr(
        adapter,
        "_start_runtime_relay",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("loopback relay port is occupied")),
    )

    def checked(command: list[str], **_kwargs: object) -> str:
        commands.append(tuple(command))
        return ""

    monkeypatch.setattr(adapter, "_checked", checked)

    with pytest.raises(RuntimeError, match="occupied"):
        with adapter._owned_compose_runtime(tmp_path, "0123456789abcdef"):
            raise AssertionError("a failed relay must not yield")

    assert any(command[-3:] == ("down", "--remove-orphans", "--volumes") for command in commands)
    receipt = json.loads((tmp_path / ".control-room/last-cleanup.json").read_text())
    assert receipt["cleanup_state"] == "complete"
    assert receipt["reason"] == "spawn_failed"


def test_runtime_relay_closes_before_compose_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owned = frozenset({f"container:{_CONTAINER_ID}", f"network:{_NETWORK_ID}"})
    snapshots = iter((frozenset(), owned, owned, frozenset()))
    events: list[str] = []

    class Relay:
        def assert_healthy(self) -> None:
            events.append("relay-healthy")

        def assert_destination_observed(self) -> None:
            events.append("relay-observed")

        def close(self) -> None:
            events.append("relay-close")

    monkeypatch.setattr(adapter, "_verify_local_runtime", lambda _root: None)
    monkeypatch.setattr(adapter, "_resource_ids", lambda *_args: next(snapshots))
    monkeypatch.setattr(adapter, "_started_runtime", lambda *_args: {"structural": "owned"})
    monkeypatch.setattr(adapter, "_start_runtime_relay", lambda *_args: Relay())

    def checked(command: list[str], **_kwargs: object) -> str:
        events.append("compose-down" if "down" in command else "compose-up")
        return ""

    monkeypatch.setattr(adapter, "_checked", checked)

    with adapter._owned_compose_runtime(tmp_path, "0123456789abcdef") as child:
        child["exit"] = "success"

    assert events == [
        "compose-up",
        "relay-healthy",
        "relay-observed",
        "relay-close",
        "compose-down",
    ]


def test_all_container_commands_pin_default_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []

    class Completed:
        returncode = 0
        stdout = ""

    def record_run(command: list[str], **_kwargs: object) -> Completed:
        commands.append(tuple(command))
        return Completed()

    monkeypatch.setattr(subprocess, "run", record_run)
    adapter._checked([str(adapter._DOCKER), "ps", "-aq"], root=tmp_path)
    adapter._checked([str(adapter._COMPOSE), "version"], root=tmp_path)
    assert commands == [
        (str(adapter._DOCKER), "--context", "default", "ps", "-aq"),
        (str(adapter._COMPOSE), "--context", "default", "version"),
    ]


def test_resource_inventory_requests_full_container_and_network_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []
    replies = iter((_CONTAINER_ID, _NETWORK_ID, ""))

    def checked(command: list[str], **_kwargs: object) -> str:
        commands.append(tuple(command))
        return next(replies)

    monkeypatch.setattr(adapter, "_checked", checked)

    assert adapter._resource_ids(tmp_path, "pmr-control-room-0123456789abcdef") == frozenset(
        {f"container:{_CONTAINER_ID}", f"network:{_NETWORK_ID}"}
    )
    assert commands[0][1:4] == ("ps", "-aq", "--no-trunc")
    assert commands[1][1:5] == ("network", "ls", "-q", "--no-trunc")
    assert "--no-trunc" not in commands[2]


def test_started_runtime_rejects_owned_volume_before_inspection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        adapter,
        "_checked",
        lambda *_args, **_kwargs: pytest.fail("inspect must not run for an owned volume"),
    )
    with pytest.raises(RuntimeIdentityError, match="incomplete"):
        adapter._started_runtime(
            tmp_path,
            "pmr-control-room-0123456789abcdef",
            frozenset({"container:c", "network:n", "volume:v"}),
        )


def _valid_runtime_documents(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    project = "pmr-control-room-0123456789abcdef"
    network_name = f"{project}_control_room_internal"
    container_name = f"{project}-postgres-1"
    container: dict[str, Any] = {
        "Id": _CONTAINER_ID,
        "Name": f"/{container_name}",
        "Image": adapter._IMAGE_ID,
        "Config": {
            "User": "70:70",
            "ExposedPorts": {"5432/tcp": {}},
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "postgres",
                "com.docker.compose.container-number": "1",
                "com.docker.compose.oneoff": "False",
            },
        },
        "HostConfig": {
            "Binds": [
                f"{(root / 'docker/init/001_roles.sql').resolve()}:"
                "/docker-entrypoint-initdb.d/001_roles.sql:ro"
            ],
            "Tmpfs": {"/var/lib/postgresql": "rw,noexec,nosuid,size=256m,uid=70,gid=70,mode=0700"},
            "NetworkMode": network_name,
            "PortBindings": {},
            "PublishAllPorts": False,
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str((root / "docker/init/001_roles.sql").resolve()),
                "Destination": "/docker-entrypoint-initdb.d/001_roles.sql",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            }
        ],
        "NetworkSettings": {
            "Ports": {"5432/tcp": None},
            "Networks": {
                network_name: {
                    "NetworkID": _NETWORK_ID,
                    "EndpointID": _ENDPOINT_ID,
                    "IPAddress": "172.28.0.2",
                    "IPPrefixLen": 16,
                },
            },
        },
    }
    network: dict[str, Any] = {
        "Id": _NETWORK_ID,
        "Name": network_name,
        "Internal": True,
        "Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.network": "control_room_internal",
        },
        "Containers": {
            _CONTAINER_ID: {
                "Name": container_name,
                "EndpointID": _ENDPOINT_ID,
                "IPv4Address": "172.28.0.2/16",
                "IPv6Address": "",
            }
        },
    }
    return container, network


def _mock_runtime_inspect(
    monkeypatch: pytest.MonkeyPatch,
    container: dict[str, Any],
    network: dict[str, Any],
) -> None:
    replies = iter((json.dumps([container]), json.dumps([network])))
    monkeypatch.setattr(adapter, "_checked", lambda *_args, **_kwargs: next(replies))


@pytest.mark.parametrize(
    "ports, networks, message",
    [
        (
            {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]},
            {
                "pmr-control-room-0123456789abcdef_control_room_internal": {
                    "NetworkID": _NETWORK_ID,
                    "EndpointID": _ENDPOINT_ID,
                    "IPAddress": "172.28.0.2",
                    "IPPrefixLen": 16,
                }
            },
            "publish",
        ),
        (
            {"5432/tcp": None},
            {"a": {"NetworkID": _NETWORK_ID}, "b": {"NetworkID": "f" * 64}},
            "unexpected network",
        ),
        (
            {"5432/tcp": None},
            {
                "pmr-control-room-0123456789abcdef_control_room_internal": {
                    "NetworkID": "f" * 64,
                    "EndpointID": _ENDPOINT_ID,
                    "IPAddress": "172.28.0.2",
                    "IPPrefixLen": 16,
                }
            },
            "identity drift",
        ),
    ],
)
def test_started_runtime_rejects_port_and_network_falsifiers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ports: dict[str, object],
    networks: dict[str, dict[str, str]],
    message: str,
) -> None:
    container, network = _valid_runtime_documents(tmp_path)
    container["NetworkSettings"]["Ports"] = ports
    container["NetworkSettings"]["Networks"] = networks
    _mock_runtime_inspect(monkeypatch, container, network)
    with pytest.raises(RuntimeIdentityError, match=message):
        adapter._started_runtime(
            tmp_path,
            "pmr-control-room-0123456789abcdef",
            frozenset({f"container:{_CONTAINER_ID}", f"network:{_NETWORK_ID}"}),
        )


def test_started_runtime_rejects_extra_published_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    container, network = _valid_runtime_documents(tmp_path)
    container["NetworkSettings"]["Ports"]["9999/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "9999"}]
    _mock_runtime_inspect(monkeypatch, container, network)
    with pytest.raises(RuntimeIdentityError, match="unexpected ports"):
        adapter._started_runtime(
            tmp_path,
            "pmr-control-room-0123456789abcdef",
            frozenset({f"container:{_CONTAINER_ID}", f"network:{_NETWORK_ID}"}),
        )


def test_started_runtime_closes_names_labels_and_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = "pmr-control-room-0123456789abcdef"
    owned = frozenset({f"container:{_CONTAINER_ID}", f"network:{_NETWORK_ID}"})
    container, network = _valid_runtime_documents(tmp_path)
    _mock_runtime_inspect(monkeypatch, container, network)
    observed = adapter._started_runtime(tmp_path, project, owned)
    assert observed["container_id"] == _CONTAINER_ID
    assert observed["container_ipv4"] == "172.28.0.2"
    assert observed["endpoint_id"] == _ENDPOINT_ID

    mutations: tuple[Callable[[dict[str, Any], dict[str, Any]], None], ...] = (
        lambda container, _network: container.update({"Name": "/foreign"}),
        lambda container, _network: container["Config"]["Labels"].update(
            {"com.docker.compose.oneoff": "True"}
        ),
        lambda _container, network: network.update({"Name": "foreign"}),
        lambda _container, network: network["Labels"].update(
            {"com.docker.compose.network": "default"}
        ),
        lambda container, _network: container["Mounts"].append(
            {
                "Type": "bind",
                "Source": "/tmp/foreign",
                "Destination": "/foreign",
                "Mode": "rw",
                "RW": True,
                "Propagation": "rprivate",
            }
        ),
        lambda container, _network: container["Mounts"][0].update({"RW": True}),
        lambda container, _network: container["HostConfig"].update({"Tmpfs": {}}),
        lambda container, _network: container["NetworkSettings"]["Networks"][
            f"{project}_control_room_internal"
        ].update({"EndpointID": "f" * 64}),
        lambda _container, network: network["Containers"][_CONTAINER_ID].update(
            {"IPv4Address": "172.28.0.3/16"}
        ),
    )
    for mutate in mutations:
        container, network = _valid_runtime_documents(tmp_path)
        mutate(container, network)
        _mock_runtime_inspect(monkeypatch, container, network)
        with pytest.raises(RuntimeIdentityError):
            adapter._started_runtime(tmp_path, project, owned)


@pytest.mark.parametrize("unsafe", ("directory", "target", "temporary"))
def test_cleanup_receipt_refuses_every_symlink_surface(unsafe: str, tmp_path: Path) -> None:
    directory = tmp_path / ".control-room"
    if unsafe == "directory":
        outside = tmp_path / "outside"
        outside.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        outside = tmp_path / "outside"
        outside.write_text("foreign", encoding="utf-8")
        name = "last-cleanup.json" if unsafe == "target" else "last-cleanup.tmp"
        (directory / name).symlink_to(outside)
    with pytest.raises(RuntimeError, match="symlink|unsafe"):
        adapter._cleanup_receipt(tmp_path, "0123456789abcdef", "complete", "success", 0, "success")


def test_cleanup_receipt_stays_on_anchored_directory_after_parent_swap(tmp_path: Path) -> None:
    directory = tmp_path / ".control-room"
    directory.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    directory_fd = adapter._open_control_directory(tmp_path, create=False)
    anchored = tmp_path / "anchored"
    directory.rename(anchored)
    directory.symlink_to(outside, target_is_directory=True)
    try:
        adapter._cleanup_receipt(
            tmp_path,
            "0123456789abcdef",
            "complete",
            "success",
            0,
            "success",
            directory_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)

    assert (
        json.loads((anchored / "last-cleanup.json").read_text(encoding="utf-8"))["run_nonce"]
        == "0123456789abcdef"
    )
    assert not (outside / "last-cleanup.json").exists()


@pytest.mark.parametrize(
    ("child_exit", "expected_reason"),
    (("error", "engine_error"), ("timeout", "engine_timeout")),
)
def test_cleanup_receipt_preserves_engine_failure_reason_after_successful_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    child_exit: str,
    expected_reason: str,
) -> None:
    owned = frozenset({"container:c", "network:n"})
    snapshots = iter((frozenset(), owned, owned, frozenset()))
    monkeypatch.setattr(adapter, "_verify_local_runtime", lambda _root: None)
    monkeypatch.setattr(adapter, "_resource_ids", lambda *_args: next(snapshots))
    monkeypatch.setattr(adapter, "_started_runtime", lambda *_args: {"container_id": "c"})
    monkeypatch.setattr(adapter, "_start_runtime_relay", lambda *_args: _SuccessfulRelay())
    monkeypatch.setattr(adapter, "_checked", lambda *_args, **_kwargs: "")

    with pytest.raises(RuntimeError, match="injected"):
        with adapter._owned_compose_runtime(tmp_path, "0123456789abcdef") as child:
            child["exit"] = child_exit
            raise RuntimeError("injected engine failure")

    receipt = json.loads((tmp_path / ".control-room/last-cleanup.json").read_text(encoding="utf-8"))
    assert receipt["cleanup_state"] == "complete"
    assert receipt["reason"] == expected_reason
    assert receipt["child_exit"] == child_exit


def test_delivery_failure_rewrites_only_the_matching_success_receipt(tmp_path: Path) -> None:
    nonce = "0123456789abcdef"
    response = adapter.RehearsalResponse({"schema_version": 1}, nonce)
    assert response.cleanup_nonce == nonce
    assert json.loads(json.dumps(response)) == {"schema_version": 1}
    directory = tmp_path / ".control-room"
    directory.mkdir(mode=0o700)
    (directory / "engine.lock").write_text("", encoding="utf-8")
    (directory / "engine.lock").chmod(0o600)
    adapter._cleanup_receipt(tmp_path, nonce, "complete", "success", 0, "success")

    adapter.record_delivery_failure(tmp_path, nonce, "client_disconnected")

    receipt_path = directory / "last-cleanup.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["reason"] == "client_disconnected"
    before_mismatch = receipt_path.read_bytes()
    with pytest.raises(RuntimeError, match="identity"):
        adapter.record_delivery_failure(tmp_path, "fedcba9876543210", "response_write_failed")
    assert receipt_path.read_bytes() == before_mismatch


def test_child_spawn_failure_is_recorded_after_compose_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owned = frozenset({"container:c", "network:n"})
    snapshots = iter((frozenset(), owned, owned, frozenset()))
    monkeypatch.setattr(adapter, "_verify_local_runtime", lambda _root: None)
    monkeypatch.setattr(adapter, "_resource_ids", lambda *_args: next(snapshots))
    monkeypatch.setattr(adapter, "_started_runtime", lambda *_args: {"container_id": "c"})
    monkeypatch.setattr(adapter, "_start_runtime_relay", lambda *_args: _SuccessfulRelay())
    monkeypatch.setattr(adapter, "_checked", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn refused")),
    )

    with pytest.raises(OSError, match="spawn refused"):
        with adapter._owned_compose_runtime(tmp_path, "0123456789abcdef") as child:
            adapter._run_child(
                tmp_path,
                "small-shop",
                "0123456789abcdef",
                child,
                expected_source_rows=[],
            )

    receipt = json.loads((tmp_path / ".control-room/last-cleanup.json").read_text(encoding="utf-8"))
    assert receipt["reason"] == "spawn_failed"
    assert receipt["child_exit"] == "not_started"


def test_child_uses_isolated_project_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def refuse_after_observation(command: list[str], **options: object) -> None:
        observed["command"] = command
        observed["options"] = options
        raise OSError("spawn refused")

    monkeypatch.setattr(subprocess, "Popen", refuse_after_observation)
    child = {"exit": "not_started"}

    with pytest.raises(OSError, match="spawn refused"):
        adapter._run_child(
            tmp_path,
            "small-shop",
            "0123456789abcdef",
            child,
            expected_source_rows=[],
        )

    assert observed["command"] == [
        sys.executable,
        "-P",
        str(tmp_path / "scripts/control_room_engine.py"),
        "--scenario",
        "small-shop",
        "--run-nonce",
        "0123456789abcdef",
    ]
    options = cast(dict[str, object], observed["options"])
    assert options["cwd"] == tmp_path
    assert options["env"] == {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(tmp_path / "src"),
    }


def test_engine_lock_symlink_is_refused_before_runtime_access(tmp_path: Path) -> None:
    directory = tmp_path / ".control-room"
    directory.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("foreign", encoding="utf-8")
    (directory / "engine.lock").symlink_to(outside)
    with pytest.raises(RuntimeError, match="lock symlink"):
        with adapter._owned_compose_runtime(tmp_path, "0123456789abcdef"):
            raise AssertionError("unsafe lock must not yield")


def test_run_rehearsal_requires_pidfd_before_compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(
        adapter,
        "_owned_compose_runtime",
        lambda *_args: pytest.fail("must not compose"),
    )
    with pytest.raises(RuntimeError, match="pidfd"):
        adapter.run_rehearsal(tmp_path, "small-shop")


def test_child_exception_terminates_and_waits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    class Process:
        pid = 123

        def communicate(self, **_kwargs: object) -> tuple[str, str]:
            raise ValueError("injected")

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

        def wait(self, **_kwargs: object) -> int:
            calls.append("wait")
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(os, "pidfd_open", lambda _pid: 7)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *_args: calls.append("signal"))
    monkeypatch.setattr(os, "close", lambda _pidfd: calls.append("close"))
    child = {"exit": "not_started"}
    with pytest.raises(ValueError, match="injected"):
        adapter._run_child(
            tmp_path,
            "small-shop",
            "0123456789abcdef",
            child,
            expected_source_rows=[],
        )
    assert calls == ["signal", "wait", "close"]


def test_sigterm_resistant_child_is_killed_through_the_owned_pidfd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    class Process:
        pid = 123

        def communicate(self, **_kwargs: object) -> tuple[str, str]:
            raise ValueError("injected")

        def poll(self) -> None:
            return None

        def wait(self, **_kwargs: object) -> int:
            calls.append("wait")
            if calls.count("wait") == 1:
                raise subprocess.TimeoutExpired("owned-child", 5)
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(os, "pidfd_open", lambda _pid: 7)
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda _pidfd, child_signal: calls.append(signal.Signals(child_signal).name),
    )
    monkeypatch.setattr(os, "close", lambda _pidfd: calls.append("close"))
    child = {"exit": "not_started"}

    with pytest.raises(ValueError, match="injected"):
        adapter._run_child(
            tmp_path,
            "small-shop",
            "0123456789abcdef",
            child,
            expected_source_rows=[],
        )

    assert calls == ["SIGTERM", "wait", "SIGKILL", "wait", "close"]
    assert child["exit"] == "signal"


def test_child_response_rejects_partial_and_nested_extra_fields() -> None:
    with pytest.raises(RuntimeError, match="engine response"):
        adapter._validate_engine_response({"schema_version": 1})
    with pytest.raises(RuntimeError, match="engine response"):
        adapter._validate_engine_response(
            {
                "schema_version": 1,
                "run_id": "a" * 64,
                "scenario_id": "small-shop",
                "mode": "bounded_disposable_database_operation",
                "verdict": "LOCAL_REHEARSAL_PASSED",
                "verdict_label": "Local rehearsal passed",
                "summary": "ok",
                "source_rows": [],
                "destination_rows": [],
                "stages": [],
                "checks": [],
                "warnings": [],
                "rollback_plan": [],
                "evidence": {"extra": True},
                "next_action": "ok",
                "cleanup_state": "complete",
            }
        )


def _valid_engine_response() -> dict[str, Any]:
    source_rows = [{"invoice_id": 101, "amount_cents": 1250}]
    destination_rows = [{"invoice_id": 101, "amount_minor": 1250, "currency_code": "EUR"}]
    migration_sha256 = dict(adapter._migration_digests())
    fixture_bytes = json.dumps(source_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "run_id": hashlib.sha256(
            fixture_bytes + "".join(migration_sha256.values()).encode("utf-8")
        ).hexdigest(),
        "scenario_id": "small-shop",
        "mode": "bounded_disposable_database_operation",
        "verdict": "LOCAL_REHEARSAL_PASSED",
        "verdict_label": "Local rehearsal passed",
        "summary": "The disposable local database completed the bounded migration path.",
        "source_rows": source_rows,
        "destination_rows": destination_rows,
        "stages": [
            {
                "id": identifier,
                "label": label,
                "status": "complete",
                "explanation": "Observed locally.",
                "evidence_ids": [identifier],
            }
            for identifier, label in (
                ("baseline", "Baseline"),
                ("expand", "Expand"),
                ("backfill", "Backfill"),
                ("switch", "Switch"),
                ("contract", "Contract"),
            )
        ],
        "checks": [
            {
                "id": "destination_matches_source",
                "label": "Destination matches source",
                "status": "pass",
                "detail": "Observed in the disposable database.",
                "evidence_id": "integrity",
            }
        ],
        "warnings": [
            {
                "id": "bounded_scope",
                "label": "Bounded scope",
                "detail": (
                    "This six-row rehearsal does not exercise the full-volume or "
                    "concurrent-client paths."
                ),
            }
        ],
        "rollback_plan": [
            {
                "order": 1,
                "label": "Return to the baseline shape",
                "observed": True,
                "evidence_id": "rollback",
            },
            {
                "order": 2,
                "label": "Roll forward through the contract",
                "observed": True,
                "evidence_id": "rollback",
            },
        ],
        "evidence": {
            "interfaces": [
                "contract._apply_migration",
                "contract._start_backfill",
                "contract._run_backfill_batch",
                "contract._finish_backfill_enable_v2",
                "contract.attempt_down",
            ],
            "postgresql_version": "18.6",
            "migration_sha256": migration_sha256,
            "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "backfill": {
                "initial_first_pass_rows": 1,
                "initial_idempotent_pass_rows": 0,
                "cycles": 2,
                "roll_forward_first_pass_rows": 1,
                "roll_forward_idempotent_pass_rows": 0,
            },
            "rollback": {
                "down_phase": "initial",
                "legacy_rows_restored": True,
                "roll_forward_complete": True,
            },
            "integrity": {
                "final_phase": "contract",
                "ledger_complete": True,
                "row_count": 1,
                "destination_matches_source": True,
                "no_target_nulls": True,
                "legacy_column_absent": True,
            },
            "operation_limit": "No full-volume, concurrent-client, or failpoint exercise.",
        },
        "next_action": "Review the local evidence and reset when ready.",
        "cleanup_state": "complete",
    }


def test_child_response_schema_is_closed_recursively() -> None:
    valid = _valid_engine_response()
    assert adapter._validate_engine_response(valid, expected_scenario_id="small-shop") == valid

    mutations: dict[str, Callable[[dict[str, Any]], None]] = {
        "bool source integer": lambda value: value["source_rows"][0].update({"amount_cents": True}),
        "extra destination key": lambda value: value["destination_rows"][0].update({"extra": 1}),
        "wrong stage enum": lambda value: value["stages"][0].update({"status": "unknown"}),
        "wrong check type": lambda value: value["checks"][0].update({"label": 7}),
        "extra warning key": lambda value: value["warnings"][0].update({"extra": True}),
        "non-contiguous rollback": lambda value: value["rollback_plan"][1].update({"order": 3}),
        "wrong interface order": lambda value: value["evidence"]["interfaces"].reverse(),
        "bad migration digest": lambda value: value["evidence"]["migration_sha256"].update(
            {"0001": "0" * 64}
        ),
        "bool backfill count": lambda value: value["evidence"]["backfill"].update(
            {"initial_first_pass_rows": True}
        ),
        "false rollback observation": lambda value: value["evidence"]["rollback"].update(
            {"legacy_rows_restored": False}
        ),
        "wrong integrity count": lambda value: value["evidence"]["integrity"].update(
            {"row_count": 2}
        ),
        "wrong fixture digest": lambda value: value["evidence"].update(
            {"fixture_sha256": "f" * 64}
        ),
        "wrong deterministic run id": lambda value: value.update({"run_id": "f" * 64}),
    }
    for _label, mutate in mutations.items():
        invalid = copy.deepcopy(valid)
        mutate(invalid)
        with pytest.raises(RuntimeError, match="engine response"):
            adapter._validate_engine_response(invalid, expected_scenario_id="small-shop")


def test_run_rehearsal_pins_prevalidated_source_rows_for_child_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = [{"invoice_id": 7, "amount_cents": 900}]
    captured: dict[str, object] = {}

    class Runtime:
        def __enter__(self) -> dict[str, str]:
            return {"exit": "not_started"}

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        adapter,
        "load_scenarios",
        lambda *_args, **_kwargs: {"small-shop": {"rows": rows}},
    )
    monkeypatch.setattr(adapter, "_owned_compose_runtime", lambda *_args: Runtime())
    monkeypatch.setattr(secrets, "token_hex", lambda _length: "0" * 16)

    def child_runner(
        _root: Path,
        _scenario_id: str,
        _nonce: str,
        _child: dict[str, str],
        *,
        expected_source_rows: list[dict[str, int]],
    ) -> dict[str, object]:
        captured["rows"] = expected_source_rows
        return {}

    monkeypatch.setattr(adapter, "_run_child", child_runner)
    adapter.run_rehearsal(tmp_path, "small-shop")

    assert captured["rows"] == rows
