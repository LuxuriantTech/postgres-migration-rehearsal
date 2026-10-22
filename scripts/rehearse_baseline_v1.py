#!/usr/bin/env python3
"""Separate, opt-in demo baseline. Never changes the frozen browser/Phase B baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from migration_rehearsal.control_room_adapter import _run_child, _validate_rows, load_scenarios
from migration_rehearsal.control_room_runtime.relay import LoopbackPostgresRelay, RuntimeTarget

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "runtime-baselines/demo-20260908/baseline.json"
DOCKER = "/usr/bin/docker"
BASELINE_SHA256 = "feb4ce482d64d4331d080a5c518ecdc8bc716e810f2e57e486a942928be50dd4"
BASELINE_VERSION = "PMR-DEMO-20260908-v1"
RESOURCE_PREFIX = "pmr-demo-v1-"


def docker(*args: str) -> str:
    return subprocess.check_output(
        [DOCKER, "--context", "default", *args],
        text=True,
        timeout=30,
        stderr=subprocess.PIPE,
    ).strip()


def verify_image(image: dict, baseline: dict) -> None:
    if (
        image.get("Id") != baseline["image_manifest"]
        or image.get("Architecture") != "amd64"
        or image.get("Os") != "linux"
        or image.get("Config", {}).get("User") != "70:70"
    ):
        raise RuntimeError("demo baseline image identity mismatch")


def verify_docker_binary(path: Path, expected_sha256: str) -> None:
    try:
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise RuntimeError(f"required Docker executable is unavailable: {path}") from error
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "Docker executable integrity check failed at "
            f"{path}: expected sha256:{expected_sha256}, got sha256:{actual_sha256}. "
            "Use the reviewed Linux amd64 Docker executable; do not update the pin "
            "to bypass this check."
        )


def verify_package_version(name: str, expected_version: str) -> None:
    try:
        actual_version = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", name],
            text=True,
            timeout=10,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"required reviewed package is unavailable: {name}") from error
    if actual_version != expected_version:
        raise RuntimeError(
            f"package version mismatch for {name}: expected {expected_version}, "
            f"got {actual_version}"
        )


def preflight(
    *,
    baseline_path: Path | None = None,
    baseline_sha256: str | None = None,
    expected_version: str | None = None,
) -> dict:
    baseline_path = baseline_path or BASELINE
    baseline_sha256 = baseline_sha256 or BASELINE_SHA256
    expected_version = expected_version or BASELINE_VERSION
    raw = baseline_path.read_bytes()
    actual_baseline_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_baseline_sha256 != baseline_sha256:
        raise RuntimeError(
            "reviewed demo baseline manifest changed: "
            f"expected sha256:{baseline_sha256}, got sha256:{actual_baseline_sha256}. "
            "Check that Git preserved LF line endings in pinned files."
        )
    baseline = json.loads(raw)
    if baseline["version"] != expected_version:
        raise RuntimeError("unknown demo baseline")
    if any(
        os.environ.get(key)
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "COMPOSE_FILE",
            "COMPOSE_PROJECT_NAME",
        )
    ):
        raise RuntimeError("container runtime overrides are not allowed")
    if baseline.get("platform") == "linux/amd64":
        import platform

        if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
            raise RuntimeError("Linux amd64 is required for this demo baseline")
    verify_docker_binary(Path(DOCKER), baseline["docker_sha256"])
    if baseline.get("docker_package"):
        verify_package_version(
            baseline["docker_package"]["name"], baseline["docker_package"]["version"]
        )
    if baseline.get("engine_sha256"):
        verify_docker_binary(Path("/usr/bin/dockerd"), baseline["engine_sha256"])
    if baseline.get("engine_package"):
        verify_package_version(
            baseline["engine_package"]["name"], baseline["engine_package"]["version"]
        )
    if baseline.get("buildx_sha256"):
        verify_docker_binary(
            Path("/usr/libexec/docker/cli-plugins/docker-buildx"),
            baseline["buildx_sha256"],
        )
    if baseline.get("buildx_package"):
        verify_package_version(
            baseline["buildx_package"]["name"], baseline["buildx_package"]["version"]
        )
    if baseline.get("docker_version") and docker(
        "version", "--format", "{{.Client.Version}}"
    ) != baseline["docker_version"]:
        raise RuntimeError("Docker CLI version does not match the reviewed demo baseline")
    if baseline.get("engine_version") and docker(
        "version", "--format", "{{.Server.Version}}"
    ) != baseline["engine_version"]:
        raise RuntimeError("Docker Engine version does not match the reviewed demo baseline")
    if baseline.get("buildx_version") and docker("buildx", "version") != baseline[
        "buildx_version"
    ]:
        raise RuntimeError("Docker Buildx version does not match the reviewed demo baseline")
    if baseline.get("python_version"):
        import platform

        if platform.python_version() != baseline["python_version"]:
            raise RuntimeError("Python version does not match the reviewed demo baseline")
    if (
        docker("context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}")
        != "unix:///var/run/docker.sock"
    ):
        raise RuntimeError("default local Docker endpoint is required")
    for name, digest in baseline["source_sha256"].items():
        path = ROOT / name
        if not path.is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("demo baseline source identity changed")
    verify_image(json.loads(docker("image", "inspect", baseline["image_manifest"]))[0], baseline)
    return baseline


def recover_owned(kind: str, name: str, nonce: str) -> str | None:
    """Recover a created resource if the CLI lost its response; never adopt another label."""
    try:
        obj = json.loads(docker(kind, "inspect", name))[0]
    except subprocess.CalledProcessError:
        return None
    labels = obj["Config"]["Labels"] if kind == "container" else obj["Labels"]
    if labels.get("pmr.demo") != nonce:
        raise RuntimeError("cleanup refused: recovered resource ownership differs")
    return str(obj["Id"])


def rehearse(
    scenario: str,
    *,
    baseline_path: Path | None = None,
    baseline_sha256: str | None = None,
    expected_version: str | None = None,
    resource_prefix: str | None = None,
) -> dict:
    # Fixture rejection occurs before any container or database is created.
    rows = _validate_rows(load_scenarios(ROOT, scenario_id=scenario)[scenario]["rows"])
    if baseline_path is None and baseline_sha256 is None and expected_version is None:
        baseline = preflight()
    else:
        baseline = preflight(
            baseline_path=baseline_path,
            baseline_sha256=baseline_sha256,
            expected_version=expected_version,
        )
    nonce = secrets.token_hex(8)
    name = (resource_prefix or RESOURCE_PREFIX) + nonce
    network_id = None
    container_id = None
    relay = None
    result = None
    try:
        network_id = docker("network", "create", "--internal", "--label", "pmr.demo=" + nonce, name)
        container_id = docker(
            "run",
            "--detach",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            "pmr.demo=" + nonce,
            "--network",
            network_id,
            "--user",
            "70:70",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--pids-limit",
            "128",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/var/lib/postgresql:rw,noexec,nosuid,size=256m,uid=70,gid=70,mode=0700",
            "--mount",
            "type=bind,src="
            + str(ROOT / "docker/init/001_roles.sql")
            + ",dst=/docker-entrypoint-initdb.d/001_roles.sql,readonly",
            "--env",
            "POSTGRES_DB=migration_rehearsal_test",
            "--env",
            "POSTGRES_USER=rehearsal_app",
            "--env",
            "POSTGRES_PASSWORD=pmr-test-password-placeholder-not-a-secret",
            baseline["image_manifest"],
            "postgres",
            "-c",
            "max_connections=20",
        )

        def target() -> RuntimeTarget:
            current = json.loads(docker("inspect", container_id))[0]
            network = json.loads(docker("network", "inspect", network_id))[0]
            endpoints = current["NetworkSettings"]["Networks"]
            if (
                current["Id"] != container_id
                or current["Image"] != baseline["image_manifest"]
                or current["Config"]["Labels"].get("pmr.demo") != nonce
                or current["Config"]["User"] != "70:70"
                or current["HostConfig"]["PortBindings"]
                or set(endpoints) != {name}
                or network["Id"] != network_id
                or network["Internal"] is not True
                or network["Labels"].get("pmr.demo") != nonce
                or set(network["Containers"]) != {container_id}
            ):
                raise RuntimeError("owned demo runtime identity changed")
            endpoint = endpoints[name]
            if endpoint["NetworkID"] != network_id:
                raise RuntimeError("owned demo network changed")
            return RuntimeTarget(
                container_id, network_id, endpoint["EndpointID"], endpoint["IPAddress"]
            )

        deadline = time.monotonic() + 30
        while True:
            target()
            ready = subprocess.run(
                [
                    DOCKER,
                    "--context",
                    "default",
                    "exec",
                    container_id,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "rehearsal_app",
                    "-t",
                    "1",
                ],
                capture_output=True,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("owned demo database startup timed out")
        relay = LoopbackPostgresRelay(target(), target)
        relay.start()
        child = {"exit": "not_started"}
        result = dict(_run_child(ROOT, scenario, nonce, child, expected_source_rows=rows))
        result["baseline"] = baseline["version"]
        result["baseline_image"] = baseline["image_manifest"]
        result["historical_phase_b_replayed"] = False
    finally:
        relay_error = None
        if relay is not None:
            try:
                relay.close()
            except Exception as error:
                relay_error = error
        # Delete only immutable IDs created here, after checking their ownership labels.
        if container_id is None:
            container_id = recover_owned("container", name, nonce)
        if container_id is not None:
            obj = json.loads(docker("inspect", container_id))[0]
            if obj["Id"] != container_id or obj["Config"]["Labels"].get("pmr.demo") != nonce:
                raise RuntimeError("cleanup refused: container ownership changed")
            docker("rm", "--force", container_id)
        if network_id is None:
            network_id = recover_owned("network", name, nonce)
        if network_id is not None:
            obj = json.loads(docker("network", "inspect", network_id))[0]
            if (
                obj["Id"] != network_id
                or obj["Labels"].get("pmr.demo") != nonce
                or obj["Containers"]
            ):
                raise RuntimeError("cleanup refused: network ownership changed")
            docker("network", "rm", network_id)
        if relay_error is not None:
            raise RuntimeError("demo relay cleanup failed") from relay_error
    if result is None:
        raise RuntimeError("no demo result")
    result["owned_resources_remaining"] = 0
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=["small-shop", "empty-ledger", "invalid-negative-amount"],
        default="small-shop",
    )
    args = parser.parse_args()
    print(json.dumps(rehearse(args.scenario), sort_keys=True, indent=2))
