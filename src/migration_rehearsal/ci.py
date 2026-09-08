"""Static AC15 CI expectations for this local-only rehearsal."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

_CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
_SETUP_UV_SHA = "c771a70e6277c0a99b617c7a806ffedaca235ff9"
_TRIVY_SHA = "ed142fd0673e97e23eac54620cfb913e5ce36c25"
_POSTGRES_IMAGE = (
    "postgres:18.6-alpine3.24@sha256:"
    "63bdc97d67b5133bf0e5ebd500bec6d046fa851dc81340d838f0347e616107e8"
)
_GITLEAKS_IMAGE = (
    "ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:"
    "c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
)
_DERIVED_IMAGE = "postgres-migration-rehearsal:contract2"
_ACTION_PIN = re.compile(r"[0-9a-f]{40}")
_WORKFLOW_SHA256 = "ba01e885d415549553d0eedcc357e03593f461d8af84554f9c854451702ca768"
_COMPOSE_SHA256 = "741a205f905f031f32d0fc6e3605f8fb4fe279fbc6f669ed4ce8de482204a160"
_DOCKERFILE_SHA256 = "a724a29208bc07394fe08370c47271156288f4538a0485a73f38747e6c13a9d4"
_ROLES_SQL_SHA256 = "1826ef5abe58e29b6166cbf2b1ecc124690f53822c5151c0cc5f62f099b71316"
_LIBCRYPTO_ADD = (
    "ADD --checksum=sha256:161223a16f042b8e469e9441291e071464fd91d4f4bbe6f496ee8d0abd4e0701 "
    "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libcrypto3-3.5.8-r0.apk "
    "/tmp/libcrypto3.apk"
)
_LIBSSL_ADD = (
    "ADD --checksum=sha256:aca521e5ae4a321322a9d47ed64a1775f5ab1ffd215d1e9fc0433c58f7bfd037 "
    "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libssl3-3.5.8-r0.apk "
    "/tmp/libssl3.apk"
)
_FILESYSTEM_TRIVY_BLOCK = f"""      - uses: aquasecurity/trivy-action@{_TRIVY_SHA}
        with:
          scan-type: fs
          scan-ref: .
          scanners: vuln,misconfig,secret
          severity: HIGH,CRITICAL
          exit-code: '1'
          ignore-unfixed: 'false'
"""
_IMAGE_TRIVY_BLOCK = f"""      - uses: aquasecurity/trivy-action@{_TRIVY_SHA}
        with:
          scan-type: image
          image-ref: {_DERIVED_IMAGE}
          scanners: vuln,secret
          severity: HIGH,CRITICAL
          exit-code: '1'
          ignore-unfixed: 'false'
          skip-setup-trivy: 'true'
"""


def _require(content: str, terms: tuple[str, ...], artifact: str) -> None:
    missing = [term for term in terms if term not in content]
    if missing:
        raise RuntimeError(f"{artifact} missing required CI terms: {', '.join(missing)}")


def _exact_line_count(content: str, line: str) -> int:
    return sum(candidate.strip() == line for candidate in content.splitlines())


def _canonical_text(path: Path, expected_sha256: str) -> str:
    try:
        artifact = path.read_bytes()
        text = artifact.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError("AC15 workflow or Compose file is unavailable") from error
    if not text.endswith("\n") or "\r" in text:
        raise RuntimeError("AC15 artifact is not UTF-8 LF canonical")
    if hashlib.sha256(artifact).hexdigest() != expected_sha256:
        raise RuntimeError("AC15 artifact digest does not match the canonical contract")
    return text


def _validate_actions(workflow: str) -> None:
    expected_actions = {
        f"actions/checkout@{_CHECKOUT_SHA}": 2,
        f"astral-sh/setup-uv@{_SETUP_UV_SHA}": 2,
        f"aquasecurity/trivy-action@{_TRIVY_SHA}": 2,
    }
    observed_actions: list[str] = []
    for line in workflow.splitlines():
        if "uses:" not in line:
            continue
        action = line.split("uses:", maxsplit=1)[1].strip().split(maxsplit=1)[0]
        if "@" not in action:
            raise RuntimeError("workflow action is not pinned")
        pin = action.rsplit("@", maxsplit=1)[1]
        if _ACTION_PIN.fullmatch(pin) is None:
            raise RuntimeError("workflow action uses a mutable pin")
        observed_actions.append(action)
    if any(action not in expected_actions for action in observed_actions):
        raise RuntimeError("workflow action is outside the AC15 allowlist")
    if {action: observed_actions.count(action) for action in expected_actions} != expected_actions:
        raise RuntimeError("workflow action occurrences do not match AC15")


def verify_ci_expectations(root: Path) -> Mapping[str, object]:
    """Refuse any workflow that cannot prove the narrow AC15 static contract."""
    workflow_path = root / ".github/workflows/ci.yml"
    compose_path = root / "docker-compose.yml"
    dockerfile_path = root / "docker/postgres/Dockerfile"
    roles_path = root / "docker/init/001_roles.sql"
    workflow = _canonical_text(workflow_path, _WORKFLOW_SHA256)
    compose = _canonical_text(compose_path, _COMPOSE_SHA256)
    dockerfile = _canonical_text(dockerfile_path, _DOCKERFILE_SHA256)
    _canonical_text(roles_path, _ROLES_SQL_SHA256)
    security_job = workflow.partition("  security:\n")[2]
    if not security_job:
        raise RuntimeError("workflow security job is unavailable")
    test_job = workflow.partition("  test:\n")[2].partition("  security:\n")[0]
    test_checkout = (
        f"      - uses: actions/checkout@{_CHECKOUT_SHA}\n"
        "        with:\n"
        "          persist-credentials: false\n"
    )
    security_checkout = (
        f"      - uses: actions/checkout@{_CHECKOUT_SHA}\n"
        "        with:\n"
        "          fetch-depth: 0\n"
        "          persist-credentials: false\n"
    )
    if test_checkout not in test_job or security_checkout not in security_job:
        raise RuntimeError("checkout hardening is incomplete")
    _require(
        compose,
        (
            f"image: {_DERIVED_IMAGE}",
            "build:\n      context: docker/postgres\n      dockerfile: Dockerfile",
            "platform: linux/amd64",
            "cpus: 1",
            "mem_limit: 512m",
            "pids_limit: 128",
            '"127.0.0.1:55432:5432"',
            "/var/lib/postgresql:rw,noexec,nosuid,size=256m",
            'test: ["CMD-SHELL", "pg_isready -U rehearsal_app -d migration_rehearsal_test"]',
        ),
        "Compose",
    )
    _require(
        dockerfile,
        (
            f"FROM {_POSTGRES_IMAGE}",
            _LIBCRYPTO_ADD,
            _LIBSSL_ADD,
            "apk add --no-network --no-cache /tmp/libcrypto3.apk /tmp/libssl3.apk",
            "rm -f /tmp/libcrypto3.apk /tmp/libssl3.apk /usr/local/bin/gosu",
            "! command -v gosu",
            "USER 70:70",
        ),
        "Dockerfile",
    )
    if "apk upgrade" in dockerfile or "apk add" not in dockerfile:
        raise RuntimeError("Dockerfile package policy is outside CONTRACT-2")
    _require(
        workflow,
        (
            "permissions:\n  contents: read\n\njobs:",
            "timeout-minutes:",
            f"actions/checkout@{_CHECKOUT_SHA}",
            f"astral-sh/setup-uv@{_SETUP_UV_SHA}",
            "version: 0.12.7",
            "persist-credentials: false",
            "uv sync --frozen --all-groups",
            "ruff format --check",
            "ruff check",
            "mypy",
            "docker compose --project-name pmr-ci --file docker-compose.yml build",
            "docker compose --project-name pmr-ci --file docker-compose.yml "
            "up -d --no-build --pull never --wait --wait-timeout 60",
            "coverage run --branch -m pytest",
            "coverage report",
            "if: always()",
            "docker compose --project-name pmr-ci --file docker-compose.yml down --remove-orphans",
            "fetch-depth: 0",
            f'docker run --rm --network none -v "$PWD:/repo:ro" {_GITLEAKS_IMAGE} '
            "git --redact --verbose --no-banner /repo",
            "uv export --frozen --all-groups --no-hashes --no-emit-project "
            "| uv run --frozen pip-audit -r /dev/stdin --progress-spinner off --strict",
            _FILESYSTEM_TRIVY_BLOCK,
            _IMAGE_TRIVY_BLOCK,
        ),
        "workflow",
    )
    if workflow.count("permissions:") != 1:
        raise RuntimeError("workflow permissions exceed the root AC15 policy")
    exact_twice = (
        "timeout-minutes: 20",
        "version: 0.12.7",
        "- run: uv python install 3.12.14",
        "- run: uv sync --frozen --all-groups",
    )
    if any(_exact_line_count(workflow, term) != 2 for term in exact_twice):
        raise RuntimeError("workflow job invariants do not occur exactly twice")
    build_step = "- run: docker compose --project-name pmr-ci --file docker-compose.yml build"
    if (
        _exact_line_count(test_job, build_step) != 1
        or _exact_line_count(security_job, build_step) != 1
    ):
        raise RuntimeError("each workflow job must build the derived image exactly once")
    if any("uv run" in line and "uv run --frozen" not in line for line in workflow.splitlines()):
        raise RuntimeError("workflow has a non-frozen uv run command")
    python_step = "- run: uv python install 3.12.14"
    if (
        _exact_line_count(test_job, python_step) != 1
        or _exact_line_count(security_job, python_step) != 1
    ):
        raise RuntimeError("test or security job does not install the required Python exactly once")
    _validate_actions(workflow)
    for forbidden in ("secrets.", "GITHUB_TOKEN", "pull_request_target", "write-all", "@v"):
        if forbidden in workflow:
            raise RuntimeError(f"workflow contains forbidden CI term: {forbidden}")
    return {"real_postgresql": True, "security_scans": ["gitleaks", "pip-audit", "trivy"]}
