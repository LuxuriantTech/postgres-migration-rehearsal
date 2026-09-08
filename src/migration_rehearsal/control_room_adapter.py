"""Bounded adapter for the loopback-only migration control room.

This module intentionally reads only tracked fixture/manifest assets.  It has no
route to the one-shot protocol or ignored historical-result directory.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Final, cast

from migration_rehearsal import contract
from migration_rehearsal.control_room_runtime import wait_for_process
from migration_rehearsal.control_room_runtime.relay import (
    LoopbackPostgresRelay,
    RuntimeTarget,
)

SCENARIO_IDS: Final = ("small-shop", "empty-ledger", "invalid-negative-amount")
_NONCE_RE: Final = re.compile(r"[0-9a-f]{16}")
_RESOURCE_ID_RE: Final = re.compile(r"[0-9a-f]{64}")
_FIXTURES: Final = Path("control-room-data/fixtures-v1.json")
_HISTORICAL: Final = Path("control-room-data/historical-phase-b-v1.json")
_CONTROL_DIRECTORY: Final = ".control-room"
_RECEIPT_NAME: Final = "last-cleanup.json"
_RECEIPT_TEMP_NAME: Final = "last-cleanup.tmp"
_DOCKER: Final = Path("/usr/bin/docker")
_COMPOSE: Final = Path("/usr/libexec/docker/cli-plugins/docker-compose")
_DOCKER_SHA256: Final = "a429e235ef670ea83357a5c8c7451f0a69d485a6fee49f9032fd938a0ab4969d"
_COMPOSE_SHA256: Final = "c57ab918abd5b05ca7e7d0f275875dd1330a695074f309dc9eab1b49efafcd4b"
_IMAGE_ID: Final = "sha256:e4155efcf0c7e302168f98a9db891ff4ed14ff3209244308533332d30cd7f6ee"
_FIXTURE_SOURCE: Final = {
    "kind": "synthetic",
    "authoring_method": "hand-authored deterministic fixture",
    "license_status": "project-authored synthetic data",
    "limitation": "Six rows are a bounded local rehearsal, not production data.",
}
_FIXTURE_OUTCOMES: Final = {
    "small-shop": "ready",
    "empty-ledger": "empty",
    "invalid-negative-amount": "rejected",
}
_SMALL_SHOP_ROWS: Final = [
    {"invoice_id": 101, "amount_cents": 1250},
    {"invoice_id": 102, "amount_cents": 2400},
    {"invoice_id": 103, "amount_cents": 99},
    {"invoice_id": 104, "amount_cents": 5000},
    {"invoice_id": 105, "amount_cents": 725},
    {"invoice_id": 106, "amount_cents": 3100},
]
_INVALID_ROWS: Final = [{"invoice_id": 201, "amount_cents": -1}]
_HISTORICAL_SCALARS: Final = {
    "schema_version": 1,
    "kind": "historical_phase_b_evidence",
    "rerunnable": False,
    "label": "Historical evidence — no rerun available",
    "source_commit": "bd16659f130c9316036cf902575dc9fcbff81f06",
    "result_status": "PHASE_B_GATES_PASSED_AWAITING_ADVERSARIAL_REVIEW",
    "independent_review_status": "CONFIRMED",
    "report_outcome": "ALL_GATES_PASSED",
    "report_gate_count": 12,
    "report_sha256": "cb8b3ee60ad1f194d88e075690f2657d96bce1e5bfe21daf497878c930d7096b",
    "result_sha256": "12f7361a8111e90021930f8e5231bac554c25c8f9636e56ec5116365a5c95347",
    "freeze_sha256": "666e3e020c653bfdf9ac0272faeb071be25bb30c37ec712f0483d0b156116fb8",
    "protocol_sha256": "666e3e020c653bfdf9ac0272faeb071be25bb30c37ec712f0483d0b156116fb8",
    "attempt_id": "b462089bf5306607b53c5fe4acf99b1cd4cf2320f78c08de721bb8aaed72a26d",
}
_HISTORICAL_SUMMARY: Final = {
    "tests_passed": 277,
    "covered_branches": 472,
    "total_branches": 588,
    "branch_coverage_percent": 80.27210884353741,
}
_HISTORICAL_LIMITS: Final = [
    "This is local synthetic historical evidence.",
    "The one-shot Phase B operation is not available from this control room.",
    (
        "The raw rereview output is not retained here; the minimized historical review field "
        "is not durable independent-review proof."
    ),
]


class FixtureValidationError(ValueError):
    """A versioned fixture cannot safely enter the bounded engine."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(detail)
        self.field = field
        self.detail = detail


class RuntimeIdentityError(RuntimeError):
    """A local resource differs from the closed control-room ownership contract."""


class EngineBusyError(RuntimeError):
    """The exclusive bounded engine lock is already held."""


class RehearsalResponse(dict[str, object]):
    """JSON mapping carrying a same-process cleanup identity outside its keys."""

    def __init__(self, payload: Mapping[str, object], cleanup_nonce: str) -> None:
        if _NONCE_RE.fullmatch(cleanup_nonce) is None:
            raise RuntimeError("invalid cleanup response identity")
        super().__init__(payload)
        self.cleanup_nonce = cleanup_nonce


def verify_started_runtime(observed: Mapping[str, object], project: str) -> str:
    """Reject a started runtime unless its internal destination identity is exact."""
    container_id = observed.get("container_id")
    network_id = observed.get("network_id")
    endpoint_id = observed.get("endpoint_id")
    if not isinstance(container_id, str) or _RESOURCE_ID_RE.fullmatch(container_id) is None:
        raise RuntimeIdentityError("container identity is unavailable")
    if not isinstance(network_id, str) or _RESOURCE_ID_RE.fullmatch(network_id) is None:
        raise RuntimeIdentityError("network identity is unavailable")
    if not isinstance(endpoint_id, str) or _RESOURCE_ID_RE.fullmatch(endpoint_id) is None:
        raise RuntimeIdentityError("network endpoint identity is unavailable")
    if observed.get("image_id") != _IMAGE_ID:
        raise RuntimeIdentityError("frozen local image identity changed")
    if observed.get("project") != project or observed.get("service") != "postgres":
        raise RuntimeIdentityError("Compose labels do not identify the owned service")
    if observed.get("network_internal") is not True:
        raise RuntimeIdentityError("owned Compose network is not internal")
    if observed.get("published_ports") != []:
        raise RuntimeIdentityError("owned internal database must not publish a Docker port")
    if observed.get("destination_port") != 5432:
        raise RuntimeIdentityError("owned database destination port changed")
    address = observed.get("container_ipv4")
    try:
        parsed = ipaddress.ip_address(address) if isinstance(address, str) else None
    except ValueError as error:
        raise RuntimeIdentityError("owned database destination address is invalid") from error
    if (
        not isinstance(parsed, ipaddress.IPv4Address)
        or not parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_unspecified
    ):
        raise RuntimeIdentityError("owned database destination address is not private IPv4")
    return container_id


def _load_document(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, ValueError) as error:
        raise RuntimeError("tracked control-room asset is unavailable") from error
    if not isinstance(value, dict):
        raise RuntimeError("tracked control-room asset has an invalid shape")
    return cast(Mapping[str, object], value)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate tracked JSON key")
        result[key] = value
    return result


def _validate_rows(rows: object) -> list[Mapping[str, int]]:
    if not isinstance(rows, list) or len(rows) > 6:
        raise FixtureValidationError(
            "invoice_id", "The synthetic fixture has an invalid row count."
        )
    validated: list[Mapping[str, int]] = []
    ids: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"invoice_id", "amount_cents"}:
            raise FixtureValidationError(
                "invoice_id", "Each synthetic row must have two numeric fields."
            )
        invoice_id = row["invoice_id"]
        amount_cents = row["amount_cents"]
        if (
            isinstance(invoice_id, bool)
            or not isinstance(invoice_id, int)
            or not 1 <= invoice_id <= 2147483647
        ):
            raise FixtureValidationError(
                "invoice_id", "Invoice ID must be a positive whole number."
            )
        if invoice_id in ids:
            raise FixtureValidationError("invoice_id", "Invoice IDs must be unique.")
        if (
            isinstance(amount_cents, bool)
            or not isinstance(amount_cents, int)
            or not 1 <= amount_cents <= 100000000
        ):
            raise FixtureValidationError(
                "amount_cents", "Amount must be a positive whole number of cents."
            )
        ids.add(invoice_id)
        validated.append({"invoice_id": invoice_id, "amount_cents": amount_cents})
    return validated


def load_scenarios(
    root: Path, *, scenario_id: str | None = None
) -> Mapping[str, Mapping[str, object]]:
    """Load versioned scenarios; validate a selected fixture only when requested."""
    document = _load_document(root / _FIXTURES)
    if (
        type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("default_scenario_id") != "small-shop"
    ):
        raise RuntimeError("fixture schema is not the approved version")
    if set(document) != {"schema_version", "default_scenario_id", "source", "scenarios"}:
        raise RuntimeError("fixture schema is not closed")
    source = document.get("source")
    if not isinstance(source, dict) or source != _FIXTURE_SOURCE:
        raise RuntimeError("fixture source is not approved")
    items = document.get("scenarios")
    if not isinstance(items, list):
        raise RuntimeError("fixture scenario list is unavailable")
    scenarios: dict[str, Mapping[str, object]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise RuntimeError("fixture scenario has an invalid shape")
        if set(item) != {"id", "title", "purpose", "expected_outcome", "rows"}:
            raise RuntimeError("fixture scenario schema is not closed")
        identifier = cast(str, item["id"])
        if identifier not in SCENARIO_IDS or identifier in scenarios:
            raise RuntimeError("fixture scenario identifier is invalid")
        if (
            not _bounded_text(item.get("title"), maximum=80)
            or not _bounded_text(item.get("purpose"), maximum=240)
            or item.get("expected_outcome") != _FIXTURE_OUTCOMES[identifier]
        ):
            raise RuntimeError("fixture scenario metadata is invalid")
        rows = item.get("rows")
        if identifier == "invalid-negative-amount":
            if rows != _INVALID_ROWS:
                raise RuntimeError("fixture rejected scenario changed")
        else:
            try:
                validated_rows = _validate_rows(rows)
            except FixtureValidationError as error:
                raise RuntimeError("fixture scenario rows are invalid") from error
            expected_rows = _SMALL_SHOP_ROWS if identifier == "small-shop" else []
            if validated_rows != expected_rows:
                raise RuntimeError("fixture scenario rows changed without a schema version")
        scenarios[identifier] = cast(Mapping[str, object], item)
    if tuple(scenarios) != SCENARIO_IDS:
        raise RuntimeError("fixture scenario set is not the approved allowlist")
    if scenario_id is not None:
        selected = scenarios.get(scenario_id)
        if selected is None:
            raise KeyError(scenario_id)
        _validate_rows(selected.get("rows"))
    return scenarios


def scenario_summaries(root: Path) -> Mapping[str, object]:
    scenarios = load_scenarios(root)
    source_schema = ["invoice_id", "amount_cents"]
    destination_schema = ["invoice_id", "amount_minor", "currency_code"]
    summaries = []
    for identifier in SCENARIO_IDS:
        scenario = scenarios[identifier]
        rows = scenario.get("rows")
        summaries.append(
            {
                "id": identifier,
                "title": scenario["title"],
                "purpose": scenario["purpose"],
                "row_count": len(rows) if isinstance(rows, list) else 0,
                "expected_outcome": scenario["expected_outcome"],
                "source_schema": source_schema,
                "destination_schema": destination_schema,
                "provenance": {
                    "kind": "synthetic",
                    "fixture_version": "1",
                    "source": "control-room-data/fixtures-v1.json",
                    "license_status": "project-authored synthetic data",
                },
            }
        )
    return {
        "schema_version": 1,
        "default_scenario_id": "small-shop",
        "scenarios": summaries,
        "scope": "synthetic_local_only_no_external_database",
    }


def historical_evidence(root: Path) -> Mapping[str, object]:
    """Return the tracked historical display manifest, never a runtime report."""
    document = _load_document(root / _HISTORICAL)
    if any(
        type(document.get(key)) is not type(value) or document.get(key) != value
        for key, value in _HISTORICAL_SCALARS.items()
    ):
        raise RuntimeError("historical display manifest failed validation")
    allowed = {*_HISTORICAL_SCALARS, "summary", "limits"}
    if set(document) != allowed:
        raise RuntimeError("historical display manifest schema is not closed")
    summary = document.get("summary")
    if (
        not isinstance(summary, dict)
        or any(
            type(summary.get(key)) is not type(value) or summary.get(key) != value
            for key, value in _HISTORICAL_SUMMARY.items()
        )
        or set(summary) != set(_HISTORICAL_SUMMARY)
    ):
        raise RuntimeError("historical display manifest summary is invalid")
    limits = document.get("limits")
    if not isinstance(limits, list) or limits != _HISTORICAL_LIMITS:
        raise RuntimeError("historical display manifest limits are invalid")
    return document


def _migration_digests() -> Mapping[str, str]:
    return {
        version: hashlib.sha256(contract._migration_bytes(version)).hexdigest()
        for version in ("0001", "0002", "0003", "0004", "0005")
    }


def _checked(command: list[str], *, root: Path, timeout: int = 15) -> str:
    if command and command[0] in {str(_DOCKER), str(_COMPOSE)}:
        command = [command[0], "--context", "default", *command[1:]]
    completed = subprocess.run(
        command, cwd=root, check=False, capture_output=True, text=True, timeout=timeout
    )
    if completed.returncode != 0:
        raise RuntimeError("local container runtime command was rejected")
    return completed.stdout.strip()


def _resource_ids(root: Path, project: str) -> frozenset[str]:
    label = f"com.docker.compose.project={project}"
    identifiers: set[str] = set()
    for kind, arguments in (
        ("container", ["ps", "-aq", "--no-trunc"]),
        ("network", ["network", "ls", "-q", "--no-trunc"]),
        ("volume", ["volume", "ls", "-q"]),
    ):
        output = _checked([str(_DOCKER), *arguments, "--filter", f"label={label}"], root=root)
        identifiers.update(f"{kind}:{line}" for line in output.splitlines() if line)
    return frozenset(identifiers)


def _started_runtime(root: Path, project: str, owned: frozenset[str]) -> Mapping[str, object]:
    containers = [
        item.removeprefix("container:") for item in owned if item.startswith("container:")
    ]
    networks = [item.removeprefix("network:") for item in owned if item.startswith("network:")]
    volumes = [item.removeprefix("volume:") for item in owned if item.startswith("volume:")]
    if len(containers) != 1 or len(networks) != 1 or volumes:
        raise RuntimeIdentityError("owned local resource set is incomplete")
    try:
        container_doc = json.loads(_checked([str(_DOCKER), "inspect", containers[0]], root=root))
        network_doc = json.loads(
            _checked([str(_DOCKER), "network", "inspect", networks[0]], root=root)
        )
        if (
            not isinstance(container_doc, list)
            or len(container_doc) != 1
            or not isinstance(network_doc, list)
            or len(network_doc) != 1
        ):
            raise RuntimeIdentityError("owned local runtime inspection is not unique")
        container = container_doc[0]
        network = network_doc[0]
        if container["Id"] != containers[0] or network["Id"] != networks[0]:
            raise RuntimeIdentityError(
                "owned inspect identity does not match the requested resource"
            )
        network_name = f"{project}_control_room_internal"
        container_name = f"{project}-postgres-1"
        if container["Name"] != f"/{container_name}" or network["Name"] != network_name:
            raise RuntimeIdentityError("owned local runtime name changed")
        labels = container["Config"]["Labels"]
        if not isinstance(labels, dict) or any(
            labels.get(key) != value
            for key, value in {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "postgres",
                "com.docker.compose.container-number": "1",
                "com.docker.compose.oneoff": "False",
            }.items()
        ):
            raise RuntimeIdentityError("owned container labels changed")
        network_labels = network["Labels"]
        if not isinstance(network_labels, dict) or any(
            network_labels.get(key) != value
            for key, value in {
                "com.docker.compose.project": project,
                "com.docker.compose.network": "control_room_internal",
            }.items()
        ):
            raise RuntimeIdentityError("owned network labels changed")
        if container["Config"]["User"] != "70:70":
            raise RuntimeIdentityError("owned container user changed")
        if container["Config"]["ExposedPorts"] != {"5432/tcp": {}}:
            raise RuntimeIdentityError("owned container exposes unexpected ports")
        expected_bind_source = str((root / "docker/init/001_roles.sql").resolve())
        expected_bind = f"{expected_bind_source}:/docker-entrypoint-initdb.d/001_roles.sql:ro"
        host_config = container["HostConfig"]
        if (
            host_config["Binds"] != [expected_bind]
            or host_config["Tmpfs"]
            != {"/var/lib/postgresql": ("rw,noexec,nosuid,size=256m,uid=70,gid=70,mode=0700")}
            or host_config["NetworkMode"] != network_name
            or host_config["PortBindings"] != {}
            or host_config["PublishAllPorts"] is not False
        ):
            raise RuntimeIdentityError("owned container mounts, network mode, or bindings changed")
        if container["Mounts"] != [
            {
                "Type": "bind",
                "Source": expected_bind_source,
                "Destination": "/docker-entrypoint-initdb.d/001_roles.sql",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            }
        ]:
            raise RuntimeIdentityError("owned container mount identity changed")
        ports = container["NetworkSettings"]["Ports"]
        if set(ports) != {"5432/tcp"}:
            raise RuntimeIdentityError("owned container publishes unexpected ports")
        if ports["5432/tcp"] is not None:
            raise RuntimeIdentityError("owned internal database must not publish a Docker port")
        memberships = container["NetworkSettings"]["Networks"]
        if not isinstance(memberships, dict) or set(memberships) != {network_name}:
            raise RuntimeIdentityError("owned container has an unexpected network membership")
        membership = memberships[network_name]
        if not isinstance(membership, dict) or membership.get("NetworkID") != network["Id"]:
            raise RuntimeIdentityError("owned container network identity drift")
        endpoint_id = membership.get("EndpointID")
        container_ipv4 = membership.get("IPAddress")
        prefix_length = membership.get("IPPrefixLen")
        if (
            not isinstance(endpoint_id, str)
            or not isinstance(container_ipv4, str)
            or type(prefix_length) is not int
        ):
            raise RuntimeIdentityError("owned container endpoint identity is unavailable")
        network_containers = network["Containers"]
        if (
            not isinstance(network_containers, dict)
            or set(network_containers) != {containers[0]}
            or not isinstance(network_containers[containers[0]], dict)
            or network_containers[containers[0]].get("Name") != container_name
        ):
            raise RuntimeIdentityError("owned network container identity changed")
        network_membership = network_containers[containers[0]]
        if (
            network_membership.get("EndpointID") != endpoint_id
            or network_membership.get("IPv4Address") != f"{container_ipv4}/{prefix_length}"
            or network_membership.get("IPv6Address") != ""
        ):
            raise RuntimeIdentityError("owned network endpoint or address identity drift")
        observed = {
            "container_id": container["Id"],
            "network_id": network["Id"],
            "endpoint_id": endpoint_id,
            "image_id": container["Image"],
            "project": labels["com.docker.compose.project"],
            "service": labels["com.docker.compose.service"],
            "network_internal": network["Internal"],
            "container_ipv4": container_ipv4,
            "destination_port": 5432,
            "published_ports": [],
        }
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeIdentityError("owned local runtime inspection is malformed") from error
    verify_started_runtime(observed, project)
    return observed


def _runtime_target(observed: Mapping[str, object]) -> RuntimeTarget:
    return RuntimeTarget(
        container_id=cast(str, observed["container_id"]),
        network_id=cast(str, observed["network_id"]),
        endpoint_id=cast(str, observed["endpoint_id"]),
        ipv4_address=cast(str, observed["container_ipv4"]),
    )


def _revalidate_runtime_target(
    root: Path,
    project: str,
    owned: frozenset[str],
) -> RuntimeTarget:
    current = _resource_ids(root, project)
    if current != owned:
        raise RuntimeIdentityError("owned local resource set changed before destination connect")
    return _runtime_target(_started_runtime(root, project, current))


def _start_runtime_relay(
    root: Path,
    project: str,
    owned: frozenset[str],
    observed: Mapping[str, object],
) -> LoopbackPostgresRelay:
    target = _runtime_target(observed)
    relay = LoopbackPostgresRelay(
        target,
        lambda: _revalidate_runtime_target(root, project, owned),
    )
    try:
        relay.start()
    except BaseException:
        relay.close()
        raise
    return relay


def _verify_local_runtime(root: Path) -> None:
    for path, expected in ((_DOCKER, _DOCKER_SHA256), (_COMPOSE, _COMPOSE_SHA256)):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise RuntimeError("frozen local container executable is unavailable") from error
        if digest != expected:
            raise RuntimeError("frozen local container executable digest changed")
    if any(
        os.environ.get(name)
        for name in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "COMPOSE_FILE",
            "COMPOSE_PROJECT_NAME",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        )
    ):
        raise RuntimeError("container runtime overrides are not allowed")
    endpoint = _checked(
        [str(_DOCKER), "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"],
        root=root,
    )
    if endpoint != "unix:///var/run/docker.sock":
        raise RuntimeError("default local container endpoint is required")
    image = _checked(
        [
            str(_DOCKER),
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            "postgres-migration-rehearsal:contract2",
        ],
        root=root,
    )
    if image != _IMAGE_ID:
        raise RuntimeError("frozen local container image is unavailable")


def _open_control_directory(root: Path, *, create: bool) -> int:
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise RuntimeError("control-room project root is unsafe") from error
    try:
        if create:
            try:
                os.mkdir(_CONTROL_DIRECTORY, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        try:
            directory_fd = os.open(
                _CONTROL_DIRECTORY,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            raise
        except OSError as error:
            raise RuntimeError("control-room receipt directory is unsafe") from error
    finally:
        os.close(root_fd)
    metadata = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_nlink < 1
    ):
        os.close(directory_fd)
        raise RuntimeError("control-room receipt directory is unsafe")
    return directory_fd


def _cleanup_receipt(
    root: Path,
    nonce: str,
    state: str,
    reason: str,
    remaining: int,
    child_exit: str,
    *,
    directory_fd: int | None = None,
) -> None:
    """Retain a closed, private cleanup receipt without browser-derived data."""
    if (
        _NONCE_RE.fullmatch(nonce) is None
        or state not in {"complete", "not_needed", "blocked_identity_drift"}
        or reason
        not in {
            "success",
            "validation_rejected",
            "preflight_rejected",
            "spawn_failed",
            "engine_error",
            "engine_timeout",
            "client_disconnected",
            "response_write_failed",
            "identity_drift",
        }
        or not _whole_number(remaining)
        or child_exit not in {"not_started", "success", "error", "timeout", "signal"}
    ):
        raise RuntimeError("control-room cleanup receipt value is invalid")
    owned_fd = directory_fd is None
    if directory_fd is None:
        directory_fd = _open_control_directory(root, create=True)
    payload = {
        "schema_version": 1,
        "run_nonce": nonce,
        "cleanup_state": state,
        "reason": reason,
        "remaining_owned_resources": remaining,
        "child_exit": child_exit,
    }
    try:
        try:
            target = os.stat(_RECEIPT_NAME, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            target = None
        if target is not None and (
            not stat.S_ISREG(target.st_mode)
            or target.st_uid != os.getuid()
            or stat.S_IMODE(target.st_mode) != 0o600
            or target.st_nlink != 1
        ):
            raise RuntimeError("control-room receipt path is unsafe")
        try:
            descriptor = os.open(
                _RECEIPT_TEMP_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise RuntimeError("control-room receipt path is unsafe") from error
        replaced = False
        try:
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            offset = 0
            while offset < len(raw):
                offset += os.write(descriptor, raw[offset:])
            os.fsync(descriptor)
            os.replace(
                _RECEIPT_TEMP_NAME,
                _RECEIPT_NAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            replaced = True
            os.fsync(directory_fd)
        finally:
            os.close(descriptor)
            if not replaced:
                try:
                    os.unlink(_RECEIPT_TEMP_NAME, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
    finally:
        if owned_fd:
            os.close(directory_fd)


def record_delivery_failure(root: Path, nonce: str, reason: str) -> None:
    """Replace only the matching successful receipt after HTTP delivery fails."""
    import fcntl

    if _NONCE_RE.fullmatch(nonce) is None or reason not in {
        "client_disconnected",
        "response_write_failed",
    }:
        raise RuntimeError("invalid delivery failure identity")
    directory_fd = _open_control_directory(root, create=False)
    try:
        try:
            lock_descriptor = os.open(
                "engine.lock",
                os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise RuntimeError("delivery failure identity is unavailable") from error
        with os.fdopen(lock_descriptor, "a+", encoding="utf-8") as lock_file:
            lock_metadata = os.fstat(lock_file.fileno())
            if (
                not stat.S_ISREG(lock_metadata.st_mode)
                or lock_metadata.st_uid != os.getuid()
                or stat.S_IMODE(lock_metadata.st_mode) != 0o600
                or lock_metadata.st_nlink != 1
            ):
                raise RuntimeError("delivery failure identity is unsafe")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                receipt_descriptor = os.open(
                    _RECEIPT_NAME,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise RuntimeError("delivery failure receipt identity is unavailable") from error
            try:
                metadata = os.fstat(receipt_descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_nlink != 1
                    or not 1 <= metadata.st_size <= 4096
                ):
                    raise RuntimeError("delivery failure receipt identity is unsafe")
                raw = bytearray()
                while len(raw) <= 4096:
                    chunk = os.read(receipt_descriptor, 4097 - len(raw))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if len(raw) > 4096:
                    raise RuntimeError("delivery failure receipt identity is unsafe")
            finally:
                os.close(receipt_descriptor)
            try:
                receipt = json.loads(bytes(raw), object_pairs_hook=_unique_json_object)
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("delivery failure receipt identity is invalid") from error
            if receipt != {
                "schema_version": 1,
                "run_nonce": nonce,
                "cleanup_state": "complete",
                "reason": "success",
                "remaining_owned_resources": 0,
                "child_exit": "success",
            }:
                raise RuntimeError("delivery failure receipt identity changed")
            _cleanup_receipt(
                root,
                nonce,
                "complete",
                reason,
                0,
                "success",
                directory_fd=directory_fd,
            )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(directory_fd)


@contextmanager
def _owned_compose_runtime(root: Path, nonce: str) -> Iterator[dict[str, str]]:
    """Create and remove only an exact, freshly-recorded local Compose set."""
    import fcntl

    directory_fd = _open_control_directory(root, create=True)
    try:
        try:
            lock_descriptor = os.open(
                "engine.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise RuntimeError("control-room engine lock symlink is refused") from error
        with os.fdopen(lock_descriptor, "a+", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise EngineBusyError("bounded local engine is busy") from error
            project = f"pmr-control-room-{nonce}"
            owned: frozenset[str] = frozenset()
            started = False
            relay: LoopbackPostgresRelay | None = None
            cleanup_state = "not_needed"
            cleanup_reason = "preflight_rejected"
            child = {"exit": "not_started"}
            try:
                _verify_local_runtime(root)
                if _resource_ids(root, project):
                    raise RuntimeError("fresh local Compose project already owns resources")
                compose = [
                    str(_COMPOSE),
                    "--project-name",
                    project,
                    "--file",
                    str(root / "docker-compose.yml"),
                    "--file",
                    str(root / "control-room-compose.yml"),
                ]
                cleanup_reason = "spawn_failed"
                _checked(
                    [
                        *compose,
                        "up",
                        "-d",
                        "--no-build",
                        "--pull",
                        "never",
                        "--wait",
                        "--wait-timeout",
                        "60",
                    ],
                    root=root,
                    timeout=70,
                )
                owned = _resource_ids(root, project)
                if not owned:
                    raise RuntimeError("local Compose start created no owned resources")
                observed = _started_runtime(root, project, owned)
                started = True
                relay = _start_runtime_relay(root, project, owned, observed)
                cleanup_reason = "engine_error"
                try:
                    yield child
                except BaseException:
                    if child["exit"] == "not_started":
                        cleanup_reason = "spawn_failed"
                    elif child["exit"] == "timeout":
                        cleanup_reason = "engine_timeout"
                    else:
                        cleanup_reason = "engine_error"
                    raise
                else:
                    relay.assert_healthy()
                    relay.assert_destination_observed()
                    cleanup_reason = "success"
            finally:
                if relay is not None:
                    relay.close()
                if started:
                    current = _resource_ids(root, project)
                    if current != owned:
                        _cleanup_receipt(
                            root,
                            nonce,
                            "blocked_identity_drift",
                            "identity_drift",
                            len(current),
                            child["exit"],
                            directory_fd=directory_fd,
                        )
                        raise RuntimeError("local cleanup blocked by resource identity drift")
                    _checked(
                        [*compose, "down", "--remove-orphans", "--volumes"], root=root, timeout=30
                    )
                    remaining = _resource_ids(root, project)
                    if remaining:
                        _cleanup_receipt(
                            root,
                            nonce,
                            "blocked_identity_drift",
                            "identity_drift",
                            len(remaining),
                            child["exit"],
                            directory_fd=directory_fd,
                        )
                        raise RuntimeError("local cleanup did not remove owned resources")
                    cleanup_state = "complete"
                elif "compose" in locals():
                    partial = _resource_ids(root, project)
                    if partial:
                        try:
                            _started_runtime(root, project, partial)
                        except RuntimeIdentityError as error:
                            _cleanup_receipt(
                                root,
                                nonce,
                                "blocked_identity_drift",
                                "identity_drift",
                                len(partial),
                                child["exit"],
                                directory_fd=directory_fd,
                            )
                            raise RuntimeError("partial local Compose identity drift") from error
                        _checked(
                            [*compose, "down", "--remove-orphans", "--volumes"],
                            root=root,
                            timeout=30,
                        )
                        remaining = _resource_ids(root, project)
                        if remaining:
                            _cleanup_receipt(
                                root,
                                nonce,
                                "blocked_identity_drift",
                                "identity_drift",
                                len(remaining),
                                child["exit"],
                                directory_fd=directory_fd,
                            )
                            raise RuntimeError(
                                "partial local Compose cleanup did not remove owned resources"
                            )
                        cleanup_state = "complete"
                _cleanup_receipt(
                    root,
                    nonce,
                    cleanup_state,
                    cleanup_reason,
                    0,
                    child["exit"],
                    directory_fd=directory_fd,
                )
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(directory_fd)


def _run_child(
    root: Path,
    scenario_id: str,
    nonce: str,
    child: dict[str, str],
    *,
    expected_source_rows: list[Mapping[str, int]],
) -> Mapping[str, object]:
    command = [
        sys.executable,
        "-P",
        str(root / "scripts/control_room_engine.py"),
        "--scenario",
        scenario_id,
        "--run-nonce",
        nonce,
    ]
    environment = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(root / "src"),
    }
    process = subprocess.Popen(
        command,
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    pidfd: int | None = None
    try:
        try:
            pidfd = os.pidfd_open(process.pid)
        except (AttributeError, OSError) as error:
            child["exit"] = "error"
            raise RuntimeError("Linux pidfd support is required") from error
        try:
            output, _ = process.communicate(timeout=75)
        except subprocess.TimeoutExpired as error:
            child["exit"] = "timeout"
            raise RuntimeError("bounded local engine timed out") from error
        if process.returncode != 0:
            child["exit"] = "error"
            raise RuntimeError("bounded local engine did not complete")
        try:
            response = json.loads(output)
        except json.JSONDecodeError as error:
            child["exit"] = "error"
            raise RuntimeError("bounded local engine returned invalid evidence") from error
        if not isinstance(response, dict):
            child["exit"] = "error"
            raise RuntimeError("bounded local engine returned invalid evidence")
        validated = _validate_engine_response(
            response,
            expected_scenario_id=scenario_id,
            expected_source_rows=expected_source_rows,
        )
        child["exit"] = "success"
        return validated
    finally:
        if process.poll() is None:
            try:
                if pidfd is not None:
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                else:
                    process.terminate()
                wait_for_process(process, timeout=5)
            except subprocess.TimeoutExpired:
                child["exit"] = "signal"
                if pidfd is not None:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                else:
                    process.kill()
                wait_for_process(process, timeout=5)
        if pidfd is not None:
            os.close(pidfd)


def _engine_contract_mismatch() -> RuntimeError:
    return RuntimeError("engine response contract mismatch")


def _bounded_text(value: object, *, maximum: int = 1024) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum


def _whole_number(value: object, *, minimum: int = 0, maximum: int = 2147483647) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _validate_engine_response(
    response: object,
    *,
    expected_scenario_id: str | None = None,
    expected_source_rows: list[Mapping[str, int]] | None = None,
) -> Mapping[str, object]:
    """Reject a child payload that is not the closed control-room result shape."""
    required = {
        "schema_version",
        "run_id",
        "scenario_id",
        "mode",
        "verdict",
        "verdict_label",
        "summary",
        "source_rows",
        "destination_rows",
        "stages",
        "checks",
        "warnings",
        "rollback_plan",
        "evidence",
        "next_action",
        "cleanup_state",
    }
    if not isinstance(response, dict) or set(response) != required:
        raise _engine_contract_mismatch()
    scenario_id = response.get("scenario_id")
    if (
        type(response.get("schema_version")) is not int
        or response.get("schema_version") != 1
        or not isinstance(scenario_id, str)
        or scenario_id not in {"small-shop", "empty-ledger"}
        or (expected_scenario_id is not None and scenario_id != expected_scenario_id)
        or response.get("mode") != "bounded_disposable_database_operation"
        or response.get("cleanup_state") != "complete"
        or re.fullmatch(r"[0-9a-f]{64}", cast(str, response.get("run_id", ""))) is None
        or not _bounded_text(response.get("verdict_label"), maximum=120)
        or not _bounded_text(response.get("summary"))
        or not _bounded_text(response.get("next_action"), maximum=240)
    ):
        raise _engine_contract_mismatch()

    try:
        source_rows = _validate_rows(response.get("source_rows"))
    except FixtureValidationError as error:
        raise _engine_contract_mismatch() from error
    if expected_source_rows is not None and source_rows != expected_source_rows:
        raise _engine_contract_mismatch()
    expected_destination = [
        {
            "invoice_id": row["invoice_id"],
            "amount_minor": row["amount_cents"],
            "currency_code": "EUR",
        }
        for row in source_rows
    ]
    destination_rows = response.get("destination_rows")
    if not isinstance(destination_rows, list) or len(destination_rows) > 6:
        raise _engine_contract_mismatch()
    for row in destination_rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"invoice_id", "amount_minor", "currency_code"}
            or not _whole_number(row.get("invoice_id"), minimum=1)
            or not _whole_number(row.get("amount_minor"), minimum=1, maximum=100000000)
            or row.get("currency_code") != "EUR"
        ):
            raise _engine_contract_mismatch()
    if destination_rows != expected_destination:
        raise _engine_contract_mismatch()

    empty = not source_rows
    expected_verdict = "NO_ROWS_TO_MIGRATE" if empty else "LOCAL_REHEARSAL_PASSED"
    if response.get("verdict") != expected_verdict:
        raise _engine_contract_mismatch()

    stage_identifiers = ("baseline", "expand", "backfill", "switch", "contract")
    stages = response.get("stages")
    if not isinstance(stages, list) or len(stages) != len(stage_identifiers):
        raise _engine_contract_mismatch()
    for stage, identifier in zip(stages, stage_identifiers, strict=True):
        expected_status = "not_needed" if empty and identifier == "backfill" else "complete"
        if (
            not isinstance(stage, dict)
            or set(stage) != {"id", "label", "status", "explanation", "evidence_ids"}
            or stage.get("id") != identifier
            or not _bounded_text(stage.get("label"), maximum=80)
            or stage.get("status") != expected_status
            or not _bounded_text(stage.get("explanation"), maximum=240)
            or stage.get("evidence_ids") != [identifier]
        ):
            raise _engine_contract_mismatch()

    checks = response.get("checks")
    if not isinstance(checks, list) or len(checks) != 1:
        raise _engine_contract_mismatch()
    check = checks[0]
    if (
        not isinstance(check, dict)
        or set(check) != {"id", "label", "status", "detail", "evidence_id"}
        or check.get("id") != "destination_matches_source"
        or not _bounded_text(check.get("label"), maximum=120)
        or check.get("status") != "pass"
        or not _bounded_text(check.get("detail"), maximum=240)
        or check.get("evidence_id") != "integrity"
    ):
        raise _engine_contract_mismatch()

    warnings = response.get("warnings")
    if not isinstance(warnings, list) or len(warnings) != 1:
        raise _engine_contract_mismatch()
    warning = warnings[0]
    if (
        not isinstance(warning, dict)
        or set(warning) != {"id", "label", "detail"}
        or warning.get("id") != "bounded_scope"
        or not _bounded_text(warning.get("label"), maximum=120)
        or not _bounded_text(warning.get("detail"), maximum=320)
    ):
        raise _engine_contract_mismatch()

    rollback_plan = response.get("rollback_plan")
    if not isinstance(rollback_plan, list) or len(rollback_plan) != 2:
        raise _engine_contract_mismatch()
    for order, step in enumerate(rollback_plan, start=1):
        if (
            not isinstance(step, dict)
            or set(step) != {"order", "label", "observed", "evidence_id"}
            or type(step.get("order")) is not int
            or step.get("order") != order
            or not _bounded_text(step.get("label"), maximum=160)
            or step.get("observed") is not True
            or step.get("evidence_id") != "rollback"
        ):
            raise _engine_contract_mismatch()

    evidence = response.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {
        "interfaces",
        "postgresql_version",
        "migration_sha256",
        "fixture_sha256",
        "backfill",
        "rollback",
        "integrity",
        "operation_limit",
    }:
        raise _engine_contract_mismatch()

    interfaces = [
        "contract._apply_migration",
        "contract._start_backfill",
        "contract._run_backfill_batch",
        "contract._finish_backfill_enable_v2",
        "contract.attempt_down",
    ]
    migration_sha256 = evidence.get("migration_sha256")
    expected_migration_sha256 = dict(_migration_digests())
    fixture_bytes = json.dumps(source_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_fixture_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
    expected_run_id = hashlib.sha256(
        fixture_bytes + "".join(expected_migration_sha256.values()).encode("utf-8")
    ).hexdigest()
    if (
        evidence.get("interfaces") != interfaces
        or evidence.get("postgresql_version") != "18.6"
        or not isinstance(migration_sha256, dict)
        or migration_sha256 != expected_migration_sha256
        or evidence.get("fixture_sha256") != expected_fixture_sha256
        or response.get("run_id") != expected_run_id
        or not _bounded_text(evidence.get("operation_limit"), maximum=320)
    ):
        raise _engine_contract_mismatch()

    backfill = evidence.get("backfill")
    expected_count = len(source_rows)
    if (
        not isinstance(backfill, dict)
        or set(backfill)
        != {
            "initial_first_pass_rows",
            "initial_idempotent_pass_rows",
            "cycles",
            "roll_forward_first_pass_rows",
            "roll_forward_idempotent_pass_rows",
        }
        or not _whole_number(backfill.get("initial_first_pass_rows"), maximum=6)
        or backfill.get("initial_first_pass_rows") != expected_count
        or type(backfill.get("initial_idempotent_pass_rows")) is not int
        or backfill.get("initial_idempotent_pass_rows") != 0
        or type(backfill.get("cycles")) is not int
        or backfill.get("cycles") != 2
        or not _whole_number(backfill.get("roll_forward_first_pass_rows"), maximum=6)
        or backfill.get("roll_forward_first_pass_rows") != expected_count
        or type(backfill.get("roll_forward_idempotent_pass_rows")) is not int
        or backfill.get("roll_forward_idempotent_pass_rows") != 0
    ):
        raise _engine_contract_mismatch()

    rollback = evidence.get("rollback")
    if not isinstance(rollback, dict) or rollback != {
        "down_phase": "initial",
        "legacy_rows_restored": True,
        "roll_forward_complete": True,
    }:
        raise _engine_contract_mismatch()
    integrity = evidence.get("integrity")
    if not isinstance(integrity, dict) or integrity != {
        "final_phase": "contract",
        "ledger_complete": True,
        "row_count": expected_count,
        "destination_matches_source": True,
        "no_target_nulls": True,
        "legacy_column_absent": True,
    }:
        raise _engine_contract_mismatch()
    return cast(Mapping[str, object], response)


def run_rehearsal(root: Path, scenario_id: str) -> Mapping[str, object]:
    """Invoke the bounded child by a closed argv template after fixture validation.

    The child remains opt-in for an actual local Docker engine: normal unit and
    browser suites inject a deterministic runner into the HTTP service.
    """
    if scenario_id not in SCENARIO_IDS:
        raise KeyError(scenario_id)
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Linux pidfd support is required")
    nonce = secrets.token_hex(8)
    if _NONCE_RE.fullmatch(nonce) is None:
        raise RuntimeError("invalid internal engine nonce")
    try:
        selected = load_scenarios(root, scenario_id=scenario_id)[scenario_id]
        expected_source_rows = _validate_rows(selected.get("rows"))
    except FixtureValidationError:
        _cleanup_receipt(
            root,
            nonce,
            "not_needed",
            "validation_rejected",
            0,
            "not_started",
        )
        raise
    with _owned_compose_runtime(root, nonce) as child:
        return RehearsalResponse(
            _run_child(
                root,
                scenario_id,
                nonce,
                child,
                expected_source_rows=expected_source_rows,
            ),
            nonce,
        )
