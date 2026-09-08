"""Pure, injectable PMR-CONTRACT-2 preflight and frozen gate runner."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from migration_rehearsal.final_once import FinalGateFailure

GATE_ORDER = (
    "ci_static",
    "ruff_format",
    "ruff_lint",
    "mypy",
    "gitleaks",
    "pip_audit",
    "trivy_fs",
    "trivy_image",
    "compose_start",
    "image_identity",
    "pytest_coverage",
    "compose_cleanup",
)
_FREEZE_KEYS = {
    "batch_size",
    "coverage_threshold",
    "dataset",
    "execution_tools",
    "gate_order",
    "image_id",
    "manifest",
    "metrics",
    "python_executable",
    "python_version",
    "repetitions",
    "scanner_db_digest",
    "scenario",
    "schema_version",
    "seed",
    "timeouts",
    "tool_versions",
    "volume",
}
_FREEZE_PATH = "reports/development/final-freeze.json"
_SCENARIO = "invoice-expand-contract"
_DATASET = "synthetic-invoices-v1"
_METRICS = ["integrity", "branch_coverage"]
_TOOL_KEYS = {
    "coverage",
    "docker",
    "docker_compose",
    "gitleaks",
    "mypy",
    "pip_audit",
    "postgresql",
    "psycopg",
    "pytest",
    "python",
    "ruff",
    "sqlite",
    "trivy",
    "uv",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    timeout_seconds: int
    cwd: Path = Path(".")
    env: Mapping[str, str] = field(default_factory=dict)
    tool_sha256: str = ""
    resolved_path: str = ""
    pass_fds: tuple[int, ...] = ()


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""


Executor = Callable[[CommandSpec], CommandResult]
_CACHE_NAME = ".pip-audit-cache.tmp"
_CACHE_MAX_DEPTH = 12
_CACHE_MAX_ENTRIES = 4096
_CACHE_MAX_BYTES = 64 * 1024 * 1024
TRIVY_CACHE_DIR = Path("/tmp/trivy")


def _remove_pip_audit_cache_at(parent_fd: int) -> None:
    """Remove the bounded cache relative to a borrowed, already-open parent FD."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_stat = os.stat(_CACHE_NAME, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        raise OSError("unsafe pip-audit cache root")
    cache_fd = os.open(_CACHE_NAME, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(cache_fd)
        if (opened.st_dev, opened.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise OSError("pip-audit cache inode changed")
        seen = 0
        total_bytes = 0

        def remove(directory_fd: int, depth: int) -> None:
            nonlocal seen, total_bytes
            if depth > _CACHE_MAX_DEPTH:
                raise OSError("pip-audit cache depth limit")
            for entry in os.scandir(directory_fd):
                seen += 1
                if seen > _CACHE_MAX_ENTRIES:
                    raise OSError("pip-audit cache entry limit")
                entry_stat = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise OSError("pip-audit cache symlink")
                if stat.S_ISREG(entry_stat.st_mode):
                    total_bytes += entry_stat.st_size
                    if total_bytes > _CACHE_MAX_BYTES:
                        raise OSError("pip-audit cache byte limit")
                    os.unlink(entry.name, dir_fd=directory_fd)
                elif stat.S_ISDIR(entry_stat.st_mode):
                    child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                    try:
                        opened_child = os.fstat(child_fd)
                        if (opened_child.st_dev, opened_child.st_ino) != (
                            entry_stat.st_dev,
                            entry_stat.st_ino,
                        ):
                            raise OSError("pip-audit cache child inode changed")
                        remove(child_fd, depth + 1)
                        os.fsync(child_fd)
                    finally:
                        os.close(child_fd)
                    os.rmdir(entry.name, dir_fd=directory_fd)
                else:
                    raise OSError("pip-audit cache special entry")

        remove(cache_fd, 1)
        current_root = os.stat(_CACHE_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (current_root.st_dev, current_root.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("pip-audit cache root inode changed")
    finally:
        os.close(cache_fd)
    os.rmdir(_CACHE_NAME, dir_fd=parent_fd)
    os.fsync(parent_fd)


def remove_pip_audit_cache(root: Path) -> None:
    """Remove only the bounded, no-follow pip-audit cache below this exact root."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(root, flags)
    try:
        _remove_pip_audit_cache_at(parent_fd)
    finally:
        os.close(parent_fd)


class TempStore:
    """Small injectable store for the only final-runner temporary input."""

    def write(self, name: str, payload: bytes) -> None:
        raise NotImplementedError

    def unlink(self, name: str) -> None:
        raise NotImplementedError

    def create_directory(self, name: str) -> None:
        raise NotImplementedError

    def remove_directory(self, name: str) -> None:
        raise NotImplementedError


class FileTempStore(TempStore):
    """No-follow, exclusive and durable files below one explicitly chosen directory."""

    def __init__(self, root: Path, *, directory_fd: int | None = None) -> None:
        self.root = root
        self.directory_fd = directory_fd

    def _name(self, name: str) -> str:
        if name not in {".pip-audit-requirements.tmp", _CACHE_NAME}:
            raise ValueError("unexpected temporary path")
        return name

    def _parent_fd(self) -> tuple[int, bool]:
        if self.directory_fd is not None:
            return self.directory_fd, False
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        return os.open(self.root, flags), True

    @staticmethod
    def _finish_parent_fd(parent_fd: int, owned: bool) -> None:
        try:
            os.fsync(parent_fd)
        finally:
            if owned:
                os.close(parent_fd)

    def write(self, name: str, payload: bytes) -> None:
        name = self._name(name)
        parent_fd, owned = self._parent_fd()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset:])
                    if written <= 0:
                        raise OSError("zero-byte temporary write")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            self._finish_parent_fd(parent_fd, owned)

    def unlink(self, name: str) -> None:
        name = self._name(name)
        parent_fd, owned = self._parent_fd()
        try:
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                return
        finally:
            self._finish_parent_fd(parent_fd, owned)

    def create_directory(self, name: str) -> None:
        if name != _CACHE_NAME:
            raise ValueError("unexpected temporary directory")
        parent_fd, owned = self._parent_fd()
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            child_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    raise OSError("unsafe temporary cache")
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(child_fd)
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    raise OSError("temporary cache inode changed")
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            os.fsync(parent_fd)
        finally:
            self._finish_parent_fd(parent_fd, owned)

    def remove_directory(self, name: str) -> None:
        if name != _CACHE_NAME:
            raise ValueError("unexpected temporary directory")
        parent_fd, owned = self._parent_fd()
        try:
            _remove_pip_audit_cache_at(parent_fd)
        finally:
            if owned:
                os.close(parent_fd)


class MemoryTempStore(TempStore):
    """Test double which exposes no filesystem behaviour."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def write(self, name: str, payload: bytes) -> None:
        if name in self.values:
            raise FileExistsError(name)
        self.values[name] = payload

    def unlink(self, name: str) -> None:
        self.values.pop(name, None)

    def create_directory(self, name: str) -> None:
        self.values[name] = b"directory"

    def remove_directory(self, name: str) -> None:
        self.values.pop(name, None)


def canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_sha256_reference(value: object) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and _is_hex_digest(value[7:])


_EXECUTION_TOOL_KEYS = {"git", "docker", "docker_compose", "trivy", "uv", "python"}


def attest_execution_tools(execution_tools: Mapping[str, object]) -> Mapping[str, object]:
    """Validate the frozen absolute executable identities without spawning them."""
    if set(execution_tools) != _EXECUTION_TOOL_KEYS:
        raise ValueError("execution tools are not exact")
    for name, raw in execution_tools.items():
        if not isinstance(raw, Mapping) or set(raw) != {"argv0", "resolved_path", "sha256"}:
            raise ValueError("execution tool schema is invalid")
        argv0, resolved, digest = raw["argv0"], raw["resolved_path"], raw["sha256"]
        if (
            not isinstance(argv0, str)
            or not isinstance(resolved, str)
            or not _is_hex_digest(digest)
        ):
            raise ValueError("execution tool value is invalid")
        path = Path(argv0)
        if (
            not path.is_absolute()
            or not Path(resolved).is_absolute()
            or path.resolve() != Path(resolved)
        ):
            raise ValueError("execution tool path is invalid")
        if name == "docker_compose" and path.name != "docker-compose":
            raise ValueError("docker compose must be the direct v2 plugin")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise ValueError("execution tool is not executable")
        if sha256_bytes(path.read_bytes()) != digest:
            raise ValueError("execution tool digest changed")
    return execution_tools


def bootstrap_preflight(execution_tools: Mapping[str, object]) -> Mapping[str, object]:
    try:
        attest_execution_tools(execution_tools)
    except (OSError, ValueError):
        return {"status": "BLOCKED"}
    return {"status": "PASS"}


def build_child_environments(
    root: Path, freeze: Mapping[str, object]
) -> Mapping[str, Mapping[str, str]]:
    tools = cast(Mapping[str, Mapping[str, str]], freeze["execution_tools"])
    del root  # Command cwd is frozen separately; children receive no inherited environment.
    tool_path = os.pathsep.join(
        sorted({str(Path(entry["argv0"]).parent) for entry in tools.values()})
    )
    return {
        "git": {"PATH": tool_path},
        "docker": {"PATH": tool_path},
        "docker_compose": {"PATH": tool_path},
        "trivy": {"PATH": tool_path},
        "uv": {"PATH": tool_path},
        "python": {"PATH": tool_path, "PYTHONPATH": "src"},
    }


def validate_freeze(
    raw: bytes, *, head_commit: str, tracked_hashes: Mapping[str, str]
) -> Mapping[str, object]:
    """Validate the tracked immutable experiment definition without creating it."""
    try:
        parsed: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("freeze is not canonical JSON") from error
    if not isinstance(parsed, dict) or raw != canonical_json(parsed) or set(parsed) != _FREEZE_KEYS:
        raise ValueError("freeze schema is not exact")
    freeze = parsed
    manifest = freeze["manifest"]
    expected = {
        path: digest
        for path, digest in tracked_hashes.items()
        if path not in {_FREEZE_PATH, "git_commit"}
    }
    controls = ("scenario", "dataset", "seed")
    if (
        type(freeze["schema_version"]) is not int
        or freeze["schema_version"] != 3
        or type(freeze["repetitions"]) is not int
        or freeze["repetitions"] != 1
        or freeze["gate_order"] != list(GATE_ORDER)
        or freeze["python_version"] != "3.12.14"
        or not isinstance(freeze["python_executable"], str)
        or not Path(freeze["python_executable"]).is_absolute()
        or not _is_sha256_reference(freeze["image_id"])
        or not _is_sha256_reference(freeze["scanner_db_digest"])
        or not isinstance(manifest, dict)
        or list(manifest) != sorted(manifest)
        or manifest != expected
        or any(
            not isinstance(path, str) or not _is_hex_digest(digest)
            for path, digest in manifest.items()
        )
        or any(not isinstance(freeze[key], str) or not freeze[key] for key in controls)
        or type(freeze["volume"]) is not int
        or freeze["volume"] <= 0
        or type(freeze["batch_size"]) is not int
        or freeze["batch_size"] <= 0
        or type(freeze["coverage_threshold"]) not in {int, float}
        or not 0 <= float(freeze["coverage_threshold"]) <= 100
        or not isinstance(freeze["metrics"], list)
        or not freeze["metrics"]
        or any(not isinstance(x, str) or not x for x in freeze["metrics"])
        or not isinstance(freeze["tool_versions"], dict)
        or set(freeze["tool_versions"]) != _TOOL_KEYS
        or any(
            not isinstance(k, str) or not isinstance(v, str) or not v
            for k, v in freeze["tool_versions"].items()
        )
        or not isinstance(freeze["timeouts"], dict)
        or set(freeze["timeouts"]) != set(GATE_ORDER)
        or any(type(value) is not int or value <= 0 for value in freeze["timeouts"].values())
        or _FREEZE_PATH in manifest
        or "git_commit" in manifest
        or not isinstance(head_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", head_commit) is None
    ):
        raise ValueError("freeze values are invalid")
    tools = freeze["execution_tools"]
    if not isinstance(tools, dict) or set(tools) != _EXECUTION_TOOL_KEYS:
        raise ValueError("freeze execution tools are invalid")
    if (
        any(
            not isinstance(entry, dict)
            or set(entry) != {"argv0", "resolved_path", "sha256"}
            or not all(isinstance(entry[key], str) for key in entry)
            or not Path(entry["argv0"]).is_absolute()
            or not Path(entry["resolved_path"]).is_absolute()
            or not _is_hex_digest(entry["sha256"])
            for entry in tools.values()
        )
        or tools["docker_compose"]["argv0"] != tools["docker_compose"]["resolved_path"]
        or Path(tools["docker_compose"]["argv0"]).name != "docker-compose"
    ):
        raise ValueError("freeze execution tools are invalid")
    if freeze["python_executable"] != tools["python"]["argv0"]:
        raise ValueError("freeze Python executable mismatch")
    if (
        freeze["volume"] != 4096
        or freeze["batch_size"] != 256
        or freeze["seed"] != "7919"
        or freeze["scenario"] != _SCENARIO
        or freeze["dataset"] != _DATASET
        or freeze["metrics"] != _METRICS
    ):
        raise ValueError("freeze protocol controls are invalid")
    return freeze


def preflight(
    snapshot: Mapping[str, object], *, execution_tools: Mapping[str, object] | None = None
) -> Mapping[str, object]:
    """Return PASS/BLOCKED without running a command or mutating a path."""
    if execution_tools is not None and bootstrap_preflight(execution_tools)["status"] != "PASS":
        return {"status": "BLOCKED", "reason_code": "PREFLIGHT_EXECUTION_TOOLS"}
    required = {
        "branch": "main",
        "compose_active": False,
        "docker_default_local": True,
        "index_clean": True,
        "remote_count": 0,
        "scanner_db_ready": True,
        "sqlite3_available": True,
        "python_executable_match": True,
        "worktree_clean": True,
        "python_version": "3.12.14",
        "tool_versions_match": True,
    }
    for key, expected in required.items():
        if snapshot.get(key) != expected:
            return {"status": "BLOCKED", "reason_code": f"PREFLIGHT_{key.upper()}"}
    if not _is_sha256_reference(snapshot.get("image_id")):
        return {"status": "BLOCKED", "reason_code": "PREFLIGHT_IMAGE_ID"}
    if snapshot.get("image_id") != snapshot.get("frozen_image_id"):
        return {"status": "BLOCKED", "reason_code": "PREFLIGHT_IMAGE_MISMATCH"}
    if snapshot.get("scanner_db_digest") != snapshot.get("frozen_scanner_db_digest"):
        return {"status": "BLOCKED", "reason_code": "PREFLIGHT_SCANNER_DB_DIGEST"}
    return {"status": "PASS"}


def _record(result: CommandResult) -> dict[str, object]:
    return {
        "exit_code": result.exit_code,
        "status": "PASS" if result.exit_code == 0 else "FAIL",
        "stderr_sha256": sha256_bytes(result.stderr),
        "stdout_sha256": sha256_bytes(result.stdout),
    }


def _safe_execute(executor: Executor, spec: CommandSpec) -> CommandResult:
    try:
        result = executor(spec)
        if result.argv != spec.argv:
            raise AssertionError("executor returned a different argv")
        return result
    except Exception as error:
        return CommandResult(spec.argv, 1, b"", type(error).__name__.encode("utf-8"))


def _scanner_summary(result: CommandResult) -> tuple[bool, dict[str, int]]:
    try:
        parsed: object = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, {"findings": 0, "targets": 0}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("Results"), list):
        return False, {"findings": 0, "targets": 0}
    findings = 0
    for item in parsed["Results"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("Target"), str)
            or not item["Target"]
        ):
            return False, {"findings": 0, "targets": 0}
        for key in ("Vulnerabilities", "Misconfigurations", "Secrets"):
            value = item.get(key)
            if value is not None:
                if not isinstance(value, list) or not all(
                    isinstance(entry, dict) for entry in value
                ):
                    return False, {"findings": 0, "targets": 0}
                findings += len(value)
    return bool(parsed["Results"]), {"findings": findings, "targets": len(parsed["Results"])}


def _scanner_surface(result: CommandResult, kind: str) -> tuple[bool, dict[str, int]]:
    try:
        parsed: object = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, {"findings": 0, "targets": 0}
    if kind == "gitleaks":
        if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
            return False, {"findings": 0, "targets": 0}
        return True, {"findings": len(parsed), "targets": 1}
    if kind == "pip_audit":
        valid = (
            isinstance(parsed, dict)
            and set(parsed) == {"dependencies", "fixes"}
            and isinstance(parsed.get("dependencies"), list)
            and bool(parsed["dependencies"])
            and isinstance(parsed.get("fixes"), list)
            and all(isinstance(item, dict) for item in parsed["fixes"])
            and all(
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and bool(item["name"])
                and isinstance(item.get("version"), str)
                and bool(item["version"])
                and isinstance(item.get("vulns"), list)
                and all(isinstance(vuln, dict) for vuln in item["vulns"])
                for item in parsed["dependencies"]
            )
        )
        return (
            valid,
            {
                "findings": sum(len(item["vulns"]) for item in parsed["dependencies"])
                if valid and isinstance(parsed, dict)
                else 0,
                "targets": len(parsed["dependencies"]) if valid and isinstance(parsed, dict) else 0,
            },
        )
    return _scanner_summary(result)


def _spec(
    argv: tuple[str, ...],
    timeouts: Mapping[str, object],
    gate: str,
    *,
    root: Path = Path("."),
    env: Mapping[str, str] | None = None,
    tool_sha256: str = "",
    pass_fds: tuple[int, ...] = (),
) -> CommandSpec:
    timeout = timeouts[gate]
    if type(timeout) is not int:
        raise ValueError("frozen timeout is invalid")
    return CommandSpec(
        argv,
        timeout,
        cwd=root,
        env={} if env is None else env,
        tool_sha256=tool_sha256,
        pass_fds=pass_fds,
    )


def run_gates(
    executor: Executor,
    *,
    freeze: Mapping[str, object],
    temp_store: TempStore,
    root: Path = Path("."),
    child_environments: Mapping[str, Mapping[str, str]] | None = None,
    final_dir: Path = Path("reports/final"),
    final_fd: int | None = None,
    trivy_cache_path: Path = TRIVY_CACHE_DIR,
    trivy_cache_fd: int | None = None,
    trivy_cache_digest: Callable[[], str] | None = None,
) -> bytes:
    """Run the exact frozen gate order once; no shell, retry, or implicit update."""
    image_id, python, timeouts = (
        str(freeze["image_id"]),
        str(freeze["python_executable"]),
        freeze["timeouts"],
    )
    if not isinstance(timeouts, Mapping):
        raise ValueError("frozen timeouts are invalid")
    tools = cast(Mapping[str, Mapping[str, str]], freeze.get("execution_tools", {}))

    def tool(name: str) -> str:
        return tools.get(name, {}).get("argv0", name)

    environments = child_environments or {"default": {}, "python": {}}
    supplied_executor = executor

    def attested_executor(spec: CommandSpec) -> CommandResult:
        name = next(
            (key for key, value in tools.items() if value.get("argv0") == spec.argv[0]), "python"
        )
        environment = environments.get(name, environments.get("python", {}))
        digest = tools.get(name, {}).get("sha256", "")
        resolved_path = tools.get(name, {}).get("resolved_path", "")
        return supplied_executor(
            CommandSpec(
                spec.argv,
                spec.timeout_seconds,
                root,
                environment,
                digest,
                resolved_path,
                spec.pass_fds,
            )
        )

    compose = (
        (
            tool("docker_compose"),
            "--context",
            "default",
            "--project-name",
            "pmr-contract2",
            "--file",
            "docker-compose.yml",
        )
        if tools
        else (
            "docker",
            "--context",
            "default",
            "compose",
            "--project-name",
            "pmr-contract2",
            "--file",
            "docker-compose.yml",
        )
    )
    docker = (tool("docker"), "--context", "default")
    store = temp_store
    requirements_name = ".pip-audit-requirements.tmp"
    cache_name = _CACHE_NAME
    trivy_pass_fds = () if trivy_cache_fd is None else (trivy_cache_fd,)
    commands = {
        "ci_static": (python, "-m", "pytest", "tests/test_ci_guards.py", "-q"),
        "ruff_format": (python, "-m", "ruff", "format", "--check", "."),
        "ruff_lint": (python, "-m", "ruff", "check", "."),
        "mypy": (python, "-m", "mypy", "src", "tests"),
        "gitleaks": (
            *docker,
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "-v",
            ".:/repo:ro",
            "ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f",
            "git",
            "--redact",
            "--no-banner",
            "--no-color",
            "--log-level",
            "fatal",
            "--report-format",
            "json",
            "--report-path",
            "-",
            "/repo",
        ),
        "trivy_fs": (
            tool("trivy"),
            "fs",
            "--cache-dir",
            str(trivy_cache_path),
            "--skip-db-update",
            "--skip-java-db-update",
            "--skip-check-update",
            "--skip-version-check",
            "--skip-vex-repo-update",
            "--offline-scan",
            "--format",
            "json",
            "--scanners",
            "vuln,misconfig,secret",
            "--severity",
            "HIGH,CRITICAL",
            "--exit-code",
            "1",
            ".",
        ),
        "trivy_image": (
            tool("trivy"),
            "image",
            "--cache-dir",
            str(trivy_cache_path),
            "--image-src",
            "docker",
            "--skip-db-update",
            "--skip-java-db-update",
            "--skip-check-update",
            "--skip-version-check",
            "--skip-vex-repo-update",
            "--offline-scan",
            "--format",
            "json",
            "--scanners",
            "vuln,secret",
            "--severity",
            "HIGH,CRITICAL",
            "--exit-code",
            "1",
            image_id,
        ),
        "compose_start": compose
        + ("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60"),
    }
    gates: dict[str, dict[str, object]] = {}
    summaries: dict[str, object] = {
        "pytest": {
            "branch_count": 0,
            "branch_coverage_percent": 0.0,
            "covered_branch_count": 0,
            "pytest_stderr_sha256": sha256_bytes(b""),
            "pytest_stdout_sha256": sha256_bytes(b""),
            "test_count": 0,
        },
        "scanners": {
            "gitleaks": {"findings": 0, "targets": 1},
            "pip_audit": {"findings": 0, "targets": 0},
            "trivy_fs": {"findings": 0, "targets": 0},
            "trivy_image": {"findings": 0, "targets": 0},
        },
    }
    failed = False
    compose_attempted = False
    try:
        for gate in GATE_ORDER[:-1]:
            if failed:
                gates[gate] = {"status": "NOT_RUN"}
                continue
            if gate == "image_identity":
                container = _safe_execute(
                    attested_executor, _spec(compose + ("ps", "-q", "postgres"), timeouts, gate)
                )
                container_ids = container.stdout.decode("utf-8", "replace").splitlines()
                if container.exit_code == 0 and len(container_ids) == 1 and container_ids[0]:
                    result = _safe_execute(
                        attested_executor,
                        _spec(
                            (
                                *docker,
                                "inspect",
                                "--format",
                                "{{.Image}}",
                                container_ids[0],
                            ),
                            timeouts,
                            gate,
                        ),
                    )
                    if result.exit_code == 0 and result.stdout.strip() != image_id.encode("utf-8"):
                        result = CommandResult(result.argv, 1, result.stdout, result.stderr)
                else:
                    result = CommandResult(container.argv, 1, container.stdout, container.stderr)
            elif gate == "pytest_coverage":
                result = _safe_execute(
                    attested_executor,
                    _spec(
                        (
                            python,
                            "scripts/final.py",
                            "_coverage-gate",
                            str(freeze["coverage_threshold"]),
                            str(final_dir / ".coverage-work.tmp"),
                        ),
                        timeouts,
                        gate,
                        pass_fds=() if final_fd is None else (final_fd,),
                    ),
                )
                try:
                    coverage = json.loads(result.stdout)
                    valid = (
                        isinstance(coverage, dict)
                        and set(coverage)
                        == {
                            "branch_count",
                            "branch_coverage_percent",
                            "covered_branch_count",
                            "pytest_stderr_sha256",
                            "pytest_stdout_sha256",
                            "schema_version",
                            "test_count",
                        }
                        and type(coverage["schema_version"]) is int
                        and coverage["schema_version"] == 1
                        and type(coverage["test_count"]) is int
                        and coverage["test_count"] > 0
                        and type(coverage["branch_count"]) is int
                        and coverage["branch_count"] > 0
                        and type(coverage["covered_branch_count"]) is int
                        and 0 <= coverage["covered_branch_count"] <= coverage["branch_count"]
                        and type(coverage["branch_coverage_percent"]) in {int, float}
                        and 0 <= float(coverage["branch_coverage_percent"]) <= 100
                        and math.isclose(
                            float(coverage["branch_coverage_percent"]),
                            100 * coverage["covered_branch_count"] / coverage["branch_count"],
                            rel_tol=0,
                            abs_tol=1e-12,
                        )
                        and float(coverage["branch_coverage_percent"])
                        >= float(str(freeze["coverage_threshold"]))
                        and all(
                            isinstance(coverage[name], str) and _SHA256.fullmatch(coverage[name])
                            for name in ("pytest_stdout_sha256", "pytest_stderr_sha256")
                        )
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    valid = False
                    coverage = None
                if result.exit_code != 0 or not valid:
                    result = CommandResult(result.argv, 1, result.stdout, result.stderr)
                else:
                    summaries["pytest"] = {
                        key: coverage[key]
                        for key in (
                            "branch_count",
                            "branch_coverage_percent",
                            "covered_branch_count",
                            "pytest_stderr_sha256",
                            "pytest_stdout_sha256",
                            "test_count",
                        )
                    }
            elif gate == "pip_audit":
                exported = _safe_execute(
                    attested_executor,
                    _spec(
                        (
                            tool("uv"),
                            "export",
                            "--frozen",
                            "--all-groups",
                            "--no-hashes",
                            "--no-emit-project",
                            "--no-config",
                        ),
                        timeouts,
                        gate,
                    ),
                )
                try:
                    if exported.exit_code != 0 or not exported.stdout:
                        result = CommandResult(
                            exported.argv, 1, exported.stdout, exported.stderr or b"empty-export"
                        )
                    else:
                        store.write(requirements_name, exported.stdout)
                        store.create_directory(cache_name)
                        audited = _safe_execute(
                            attested_executor,
                            _spec(
                                (
                                    python,
                                    "-m",
                                    "pip_audit",
                                    "-r",
                                    str(final_dir / requirements_name),
                                    "--no-deps",
                                    "--disable-pip",
                                    "--vulnerability-service",
                                    # PyPI has no frozen advisory snapshot or JSON freshness field;
                                    # an unavailable service therefore fails this bounded gate.
                                    "pypi",
                                    "--timeout",
                                    str(timeouts["pip_audit"]),
                                    "--cache-dir",
                                    str(final_dir / cache_name),
                                    "--format",
                                    "json",
                                    "--strict",
                                    "--progress-spinner",
                                    "off",
                                ),
                                timeouts,
                                gate,
                                pass_fds=() if final_fd is None else (final_fd,),
                            ),
                        )
                        result = audited
                except OSError as error:
                    result = CommandResult(exported.argv, 1, exported.stdout, str(error).encode())
                finally:
                    cleanup_error: OSError | None = None
                    try:
                        store.unlink(requirements_name)
                    except OSError as error:
                        cleanup_error = error
                    try:
                        store.remove_directory(cache_name)
                    except OSError as error:
                        cleanup_error = cleanup_error or error
                    if cleanup_error is not None:
                        result = CommandResult(
                            result.argv,
                            1,
                            result.stdout,
                            result.stderr or str(cleanup_error).encode("utf-8"),
                        )
                scanners = summaries["scanners"]
                assert isinstance(scanners, dict)
                valid_surface, summary = _scanner_surface(result, "pip_audit")
                scanners["pip_audit"] = summary
                if result.exit_code != 0 or not valid_surface or summary["findings"] > 0:
                    result = CommandResult(
                        result.argv,
                        1,
                        result.stdout,
                        result.stderr
                        or (
                            b"findings-present"
                            if summary["findings"] > 0
                            else b"invalid-scanner-json"
                        ),
                    )
            elif gate in {"trivy_fs", "trivy_image"}:
                digest_error = b""
                if trivy_cache_digest is not None:
                    try:
                        if trivy_cache_digest() != freeze["scanner_db_digest"]:
                            digest_error = b"scanner-cache-digest-mismatch"
                    except Exception as error:
                        digest_error = f"scanner-cache-integrity:{type(error).__name__}".encode(
                            "utf-8"
                        )
                if digest_error:
                    result = CommandResult(commands[gate], 1, b"", digest_error)
                else:
                    result = _safe_execute(
                        attested_executor,
                        _spec(
                            commands[gate],
                            timeouts,
                            gate,
                            pass_fds=trivy_pass_fds,
                        ),
                    )
                    if trivy_cache_digest is not None:
                        try:
                            if trivy_cache_digest() != freeze["scanner_db_digest"]:
                                result = CommandResult(
                                    result.argv,
                                    1,
                                    result.stdout,
                                    b"scanner-cache-digest-mismatch",
                                )
                        except Exception as error:
                            result = CommandResult(
                                result.argv,
                                1,
                                result.stdout,
                                f"scanner-cache-integrity:{type(error).__name__}".encode("utf-8"),
                            )
                valid_surface, summary = _scanner_surface(result, gate)
                scanners = summaries["scanners"]
                assert isinstance(scanners, dict)
                scanners[gate] = summary
                if result.exit_code != 0 or not valid_surface or summary["findings"] > 0:
                    result = CommandResult(
                        result.argv,
                        1,
                        result.stdout,
                        result.stderr
                        or (
                            b"findings-present"
                            if summary["findings"] > 0
                            else b"invalid-scanner-json"
                        ),
                    )
            else:
                if gate == "compose_start":
                    compose_attempted = True
                result = _safe_execute(attested_executor, _spec(commands[gate], timeouts, gate))
                if gate in {"gitleaks", "pip_audit"}:
                    valid_surface, summary = _scanner_surface(result, gate)
                    scanners = summaries["scanners"]
                    assert isinstance(scanners, dict)
                    scanners[gate] = summary
                    if result.exit_code != 0 or not valid_surface or summary["findings"] > 0:
                        result = CommandResult(
                            result.argv,
                            1,
                            result.stdout,
                            result.stderr
                            or (
                                b"findings-present"
                                if summary["findings"] > 0
                                else b"invalid-scanner-json"
                            ),
                        )
            gates[gate] = _record(result)
            failed = failed or result.exit_code != 0
    finally:
        if compose_attempted:
            compose_cleanup = _safe_execute(
                attested_executor,
                _spec(compose + ("down", "--remove-orphans"), timeouts, "compose_cleanup"),
            )
            gates["compose_cleanup"] = _record(compose_cleanup)
        else:
            gates["compose_cleanup"] = {"status": "NOT_NEEDED"}
    status = (
        "PHASE_B_GATES_PASSED_AWAITING_ADVERSARIAL_REVIEW"
        if not failed and gates["compose_cleanup"]["status"] == "PASS"
        else "HONEST_NEGATIVE"
    )
    report = canonical_json(
        {
            "gate_order": list(GATE_ORDER),
            "gates": gates,
            "frozen": {
                "coverage_threshold": freeze["coverage_threshold"],
                "image_id": image_id,
                "scanner_db_digest": freeze["scanner_db_digest"],
                "tool_versions": freeze["tool_versions"],
            },
            "schema_version": 2,
            "status": status,
            "summary": summaries,
        }
    )
    if status == "HONEST_NEGATIVE":
        raise FinalGateFailure(report)
    return report


class _FakeExecutor:
    def __init__(self, results: Sequence[CommandResult]) -> None:
        self._results = list(results)

    def __call__(self, spec: CommandSpec) -> CommandResult:
        if not self._results:
            raise AssertionError(f"unexpected argv: {spec.argv!r}")
        result = self._results.pop(0)
        if result.argv != spec.argv:
            raise AssertionError(f"unexpected argv: {spec.argv!r}")
        return result

    def assert_exhausted(self) -> None:
        if self._results:
            raise AssertionError(f"unconsumed argv: {self._results[0].argv!r}")


def fake_executor(results: Sequence[CommandResult]) -> _FakeExecutor:
    """A deterministic no-shell executor that rejects unexpected and leftover calls."""
    return _FakeExecutor(results)
