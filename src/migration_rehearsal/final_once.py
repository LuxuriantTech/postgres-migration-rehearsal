"""Durable, single-attempt ownership for the frozen PMR-CONTRACT-2 final gate."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, cast

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MARKER_KEYS = {"attempt_id", "git_commit", "protocol_sha256", "schema_version", "status"}
_TERMINAL_KEYS = {"attempt_id", "reason_code", "report_sha256", "schema_version", "status"}
_FORBIDDEN_REPORT_TERMS = {"dsn", "duration", "environment", "pid", "production", "raw_log"}
_NEGATIVE_REASONS = {"GATE_FAILED", "INTERRUPTED_AFTER_MARKER", "INTERNAL_ERROR"}
_SUCCESS_STATUS = "PHASE_B_GATES_PASSED_AWAITING_ADVERSARIAL_REVIEW"
_REPORT_LIMIT = 5 * 1024 * 1024
_GATE_ORDER = (
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


class FinalGateFailure(Exception):
    """A final gate failed after its expurgated report was assembled."""

    def __init__(self, report: bytes) -> None:
        super().__init__("final gate failed")
        self.report = report


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written == 0:
            raise OSError("zero-byte durable write")
        offset += written


def _write_exclusive(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(path.parent)


def _write_atomic(path: Path, payload: bytes) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp_path, flags, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp_path, path)
    _fsync_directory(path.parent)


def _read_canonical_mapping(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_bytes()
        parsed: object = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or raw != _canonical_json(cast(Mapping[str, object], parsed)):
        return None
    return cast(dict[str, object], parsed)


def _attempt_id(git_commit: str, protocol_sha256: str) -> str:
    if _COMMIT_RE.fullmatch(git_commit) is None or _SHA256_RE.fullmatch(protocol_sha256) is None:
        raise ValueError("invalid frozen commit or protocol hash")
    return hashlib.sha256(f"{git_commit}\n{protocol_sha256}\n".encode("utf-8")).hexdigest()


def _negative_terminal(
    attempt_id: str, reason_code: str, report_sha256: str | None = None
) -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "reason_code": reason_code,
        "report_sha256": report_sha256,
        "schema_version": 1,
        "status": "HONEST_NEGATIVE",
    }


def _valid_marker(marker: Mapping[str, object]) -> str | None:
    if set(marker) != _MARKER_KEYS or type(marker.get("schema_version")) is not int:
        return None
    attempt_id = marker.get("attempt_id")
    git_commit = marker.get("git_commit")
    protocol_sha256 = marker.get("protocol_sha256")
    if (
        marker.get("schema_version") != 1
        or marker.get("status") != "RUNNING"
        or not isinstance(attempt_id, str)
        or not isinstance(git_commit, str)
        or not isinstance(protocol_sha256, str)
    ):
        return None
    try:
        return attempt_id if attempt_id == _attempt_id(git_commit, protocol_sha256) else None
    except ValueError:
        return None


def _valid_terminal(terminal: Mapping[str, object], attempt_id: str) -> bool:
    if set(terminal) != _TERMINAL_KEYS or type(terminal.get("schema_version")) is not int:
        return False
    if terminal.get("schema_version") != 1 or terminal.get("attempt_id") != attempt_id:
        return False
    status = terminal.get("status")
    reason = terminal.get("reason_code")
    report_sha = terminal.get("report_sha256")
    if status == _SUCCESS_STATUS:
        return (
            reason == "ALL_GATES_PASSED"
            and isinstance(report_sha, str)
            and bool(_SHA256_RE.fullmatch(report_sha))
        )
    if (
        status != "HONEST_NEGATIVE"
        or not isinstance(reason, str)
        or reason not in _NEGATIVE_REASONS
    ):
        return False
    if reason == "GATE_FAILED":
        return isinstance(report_sha, str) and bool(_SHA256_RE.fullmatch(report_sha))
    return report_sha is None


def _valid_report(report: bytes) -> bool:
    if not report or len(report) > _REPORT_LIMIT:
        return False
    try:
        parsed: object = json.loads(report)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict) or report != _canonical_json(
        cast(Mapping[str, object], parsed)
    ):
        return False
    if set(parsed) != {"frozen", "gate_order", "gates", "schema_version", "status", "summary"}:
        return False
    if (
        type(parsed.get("schema_version")) is not int
        or parsed.get("schema_version") != 2
        or parsed.get("gate_order") != list(_GATE_ORDER)
        or parsed.get("status") not in {_SUCCESS_STATUS, "HONEST_NEGATIVE"}
        or not isinstance(parsed.get("gates"), dict)
        or set(parsed["gates"]) != set(_GATE_ORDER)
        or not isinstance(parsed.get("summary"), dict)
    ):
        return False
    for name, gate in parsed["gates"].items():
        if (
            not isinstance(name, str)
            or not isinstance(gate, dict)
            or gate.get("status") not in {"PASS", "FAIL", "NOT_RUN", "NOT_NEEDED"}
        ):
            return False
        if gate["status"] in {"PASS", "FAIL"}:
            if (
                set(gate) != {"exit_code", "status", "stdout_sha256", "stderr_sha256"}
                or type(gate["exit_code"]) is not int
                or (gate["status"] == "PASS" and gate["exit_code"] != 0)
                or (gate["status"] == "FAIL" and gate["exit_code"] == 0)
            ):
                return False
            if not all(
                isinstance(gate[key], str) and _SHA256_RE.fullmatch(gate[key])
                for key in ("stdout_sha256", "stderr_sha256")
            ):
                return False
        elif set(gate) != {"status"}:
            return False
    gate_statuses = [parsed["gates"][name]["status"] for name in _GATE_ORDER]
    if parsed["status"] == _SUCCESS_STATUS and any(status != "PASS" for status in gate_statuses):
        return False
    if parsed["status"] == "HONEST_NEGATIVE" and "FAIL" not in gate_statuses:
        return False
    first_failure = next(
        (index for index, value in enumerate(gate_statuses) if value == "FAIL"), None
    )
    if first_failure is not None:
        if any(value != "PASS" for value in gate_statuses[:first_failure]):
            return False
        for index, value in enumerate(gate_statuses):
            if (
                index > first_failure
                and _GATE_ORDER[index] != "compose_cleanup"
                and value != "NOT_RUN"
            ):
                return False
        cleanup = parsed["gates"]["compose_cleanup"]["status"]
        compose_index = _GATE_ORDER.index("compose_start")
        if first_failure < compose_index and cleanup != "NOT_NEEDED":
            return False
        if first_failure >= compose_index and cleanup not in {"PASS", "FAIL"}:
            return False
    summary = parsed["summary"]
    frozen = parsed["frozen"]
    if (
        not isinstance(frozen, dict)
        or set(frozen) != {"coverage_threshold", "image_id", "scanner_db_digest", "tool_versions"}
        or type(frozen["coverage_threshold"]) not in {int, float}
        or not 0 <= float(frozen["coverage_threshold"]) <= 100
        or not isinstance(frozen["image_id"], str)
        or not frozen["image_id"].startswith("sha256:")
        or _SHA256_RE.fullmatch(frozen["image_id"][7:]) is None
        or not isinstance(frozen["scanner_db_digest"], str)
        or not frozen["scanner_db_digest"].startswith("sha256:")
        or _SHA256_RE.fullmatch(frozen["scanner_db_digest"][7:]) is None
        or not isinstance(frozen["tool_versions"], dict)
        or set(frozen["tool_versions"]) != _TOOL_KEYS
        or any(
            not isinstance(value, str) or not value for value in frozen["tool_versions"].values()
        )
    ):
        return False
    if (
        set(summary) != {"pytest", "scanners"}
        or not isinstance(summary["pytest"], dict)
        or not isinstance(summary["scanners"], dict)
    ):
        return False
    pytest_summary = summary["pytest"]
    scanners = summary["scanners"]
    if (
        set(pytest_summary)
        != {
            "branch_count",
            "branch_coverage_percent",
            "covered_branch_count",
            "pytest_stderr_sha256",
            "pytest_stdout_sha256",
            "test_count",
        }
        or type(pytest_summary["test_count"]) is not int
        or type(pytest_summary["branch_coverage_percent"]) not in {int, float}
        or type(pytest_summary["branch_count"]) is not int
        or type(pytest_summary["covered_branch_count"]) is not int
        or pytest_summary["test_count"] < 0
        or pytest_summary["branch_count"] < 0
        or not 0 <= pytest_summary["covered_branch_count"] <= pytest_summary["branch_count"]
        or not 0 <= float(pytest_summary["branch_coverage_percent"]) <= 100
        or any(
            not isinstance(pytest_summary[key], str)
            or _SHA256_RE.fullmatch(pytest_summary[key]) is None
            for key in ("pytest_stderr_sha256", "pytest_stdout_sha256")
        )
    ):
        return False
    if set(scanners) != {"gitleaks", "pip_audit", "trivy_fs", "trivy_image"}:
        return False
    if any(
        not isinstance(item, dict)
        or set(item) != {"findings", "targets"}
        or any(type(value) is not int for value in item.values())
        for item in scanners.values()
    ):
        return False
    if any(item["findings"] < 0 or item["targets"] < 0 for item in scanners.values()):
        return False
    expected_percent = (
        100 * pytest_summary["covered_branch_count"] / pytest_summary["branch_count"]
        if pytest_summary["branch_count"]
        else 0.0
    )
    if not math.isclose(
        float(pytest_summary["branch_coverage_percent"]),
        expected_percent,
        rel_tol=0,
        abs_tol=1e-12,
    ):
        return False
    if parsed["status"] == _SUCCESS_STATUS and (
        pytest_summary["test_count"] <= 0
        or pytest_summary["branch_count"] <= 0
        or float(pytest_summary["branch_coverage_percent"]) < float(frozen["coverage_threshold"])
        or any(item["targets"] <= 0 or item["findings"] != 0 for item in scanners.values())
    ):
        return False
    rendered = json.dumps(parsed, sort_keys=True).lower()
    return not any(term in rendered for term in _FORBIDDEN_REPORT_TERMS)


def _report_status(report: bytes) -> str | None:
    if not _valid_report(report):
        return None
    parsed = cast(dict[str, object], json.loads(report))
    return cast(str, parsed["status"])


def _open_lock(path: Path) -> BinaryIO:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    return cast(BinaryIO, os.fdopen(os.open(path, flags, 0o600), "a+b"))


def _open_existing_lock(path: Path) -> BinaryIO:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    return cast(BinaryIO, os.fdopen(os.open(path, flags), "a+b"))


def _cleanup_stale_temps(final_dir: Path) -> None:
    for name in (".final-report.json.tmp", ".final-result.json.tmp", ".pip-audit-requirements.tmp"):
        path = final_dir / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise OSError("unexpected final temporary entry")
            path.unlink()
    cache = final_dir / ".pip-audit-cache.tmp"
    if cache.exists() or cache.is_symlink():
        from migration_rehearsal.final_runner import remove_pip_audit_cache

        remove_pip_audit_cache(final_dir)
    coverage_dir = final_dir / ".coverage-work.tmp"
    if coverage_dir.exists() or coverage_dir.is_symlink():
        if coverage_dir.is_symlink() or not coverage_dir.is_dir():
            raise OSError("unexpected coverage temporary entry")
        allowed = {
            ".coverage",
            ".coverage-journal",
            ".coverage-shm",
            ".coverage-wal",
            "coverage.json",
        }
        entries = list(coverage_dir.iterdir())
        if any(
            entry.name not in allowed or entry.is_symlink() or not entry.is_file()
            for entry in entries
        ):
            raise OSError("unexpected coverage temporary content")
        for entry in entries:
            entry.unlink()
        coverage_dir.rmdir()
    _fsync_directory(final_dir)


def _is_canonical_final_dir(final_dir: Path) -> bool:
    return final_dir.name == "final" and final_dir.parent.name == "reports"


def _validate_canonical_final_path(final_dir: Path) -> None:
    """Reject symlink and non-directory parents without creating or changing anything."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(final_dir.parent.parent, flags)
    reports_fd = -1
    final_fd = -1
    try:
        try:
            reports_fd = os.open("reports", flags, dir_fd=root_fd)
        except FileNotFoundError:
            return
        try:
            final_fd = os.open("final", flags, dir_fd=reports_fd)
        except FileNotFoundError:
            return
    finally:
        if final_fd >= 0:
            os.close(final_fd)
        if reports_fd >= 0:
            os.close(reports_fd)
        os.close(root_fd)


def _open_or_create_canonical_final_dir(final_dir: Path) -> tuple[int, int, int]:
    """Return stable directory FDs for the exact root/reports/final walk."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(final_dir.parent.parent, flags)
    reports_fd = -1
    final_fd = -1
    try:
        for name, parent_fd in (("reports", root_fd), ("final", None)):
            if name == "final":
                if reports_fd < 0:
                    raise OSError("reports directory was not opened")
                parent_fd = reports_fd
            assert parent_fd is not None
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            opened = os.open(name, flags, dir_fd=parent_fd)
            os.fchmod(opened, 0o700)
            if name == "reports":
                reports_fd = opened
            else:
                final_fd = opened
        return root_fd, reports_fd, final_fd
    except Exception:
        if final_fd >= 0:
            os.close(final_fd)
        if reports_fd >= 0:
            os.close(reports_fd)
        os.close(root_fd)
        raise


def _run_final_once_marker_protocol(
    final_dir: Path,
    *,
    identity: Callable[[], tuple[str, str]] | None = None,
    git_commit: str | None = None,
    protocol_sha256: str | None = None,
    bootstrap_preflight: Callable[[], Mapping[str, object]] | None = None,
    preflight: Callable[[], Mapping[str, object]] | None = None,
    effect: Callable[[], bytes],
    effect_with_final_dir: Callable[[Path, int], bytes] | None = None,
    path_prepared: bool = False,
    final_fd: int | None = None,
) -> Mapping[str, object]:
    """Recover first, then preflight, marker, report and one terminal without replay."""
    marker_path = final_dir / "final-attempt.json"
    report_path = final_dir / "final-report.json"
    terminal_path = final_dir / "final-result.json"
    if (not path_prepared and final_dir.is_symlink()) or terminal_path.is_symlink():
        return {"status": "BLOCKED", "reason_code": "TERMINAL_PATH_SYMLINK"}
    terminal_already_exists = terminal_path.exists()
    if terminal_already_exists:
        lock_path = final_dir / ".final-once.lock"
        if not lock_path.is_file() or lock_path.is_symlink():
            return {"status": "BLOCKED", "reason_code": "TERMINAL_LOCK_UNSAFE"}
        lock_opener = _open_existing_lock
    else:
        if path_prepared:
            lock_path = final_dir / ".final-once.lock"
            lock_opener = _open_lock
        else:
            final_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(final_dir, 0o700)
        lock_path = final_dir / ".final-once.lock"
        lock_opener = _open_lock
    if identity is None:
        if git_commit is None or protocol_sha256 is None:
            raise ValueError("identity is required")
        legacy_commit, legacy_protocol = git_commit, protocol_sha256

        def legacy_identity() -> tuple[str, str]:
            return legacy_commit, legacy_protocol

        identity = legacy_identity

    with lock_opener(lock_path) as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "BLOCKED", "reason_code": "LOCK_HELD"}
        try:
            if (not path_prepared and final_dir.is_symlink()) or terminal_path.is_symlink():
                return {"status": "BLOCKED", "reason_code": "TERMINAL_PATH_SYMLINK"}
            if terminal_path.exists():
                if any(
                    (final_dir / name).exists() or (final_dir / name).is_symlink()
                    for name in (
                        ".final-report.json.tmp",
                        ".final-result.json.tmp",
                        ".pip-audit-requirements.tmp",
                        ".pip-audit-cache.tmp",
                        ".coverage-work.tmp",
                    )
                ):
                    return {"status": "BLOCKED", "reason_code": "TERMINAL_STALE_TEMP"}
                marker = _read_canonical_mapping(marker_path) if marker_path.exists() else None
                attempt_id = _valid_marker(marker) if marker is not None else None
                if attempt_id is None:
                    terminal = _read_canonical_mapping(terminal_path)
                    if terminal is not None and _valid_terminal(terminal, "0" * 64):
                        return terminal
                    return {"status": "BLOCKED", "reason_code": "TERMINAL_MARKER_MISMATCH"}
                terminal = _read_canonical_mapping(terminal_path)
                if terminal is None:
                    return {"status": "BLOCKED", "reason_code": "TERMINAL_INVALID"}
                if terminal.get("attempt_id") != attempt_id:
                    return {"status": "BLOCKED", "reason_code": "TERMINAL_ATTEMPT_MISMATCH"}
                if not _valid_terminal(terminal, attempt_id):
                    return {"status": "BLOCKED", "reason_code": "TERMINAL_INVALID"}
                report_sha = terminal.get("report_sha256")
                if isinstance(report_sha, str):
                    report = report_path.read_bytes() if report_path.exists() else b""
                    if (
                        _report_status(report)
                        != (
                            _SUCCESS_STATUS
                            if terminal.get("status") == _SUCCESS_STATUS
                            else "HONEST_NEGATIVE"
                        )
                        or hashlib.sha256(report).hexdigest() != report_sha
                    ):
                        return {"status": "BLOCKED", "reason_code": "TERMINAL_REPORT_MISMATCH"}
                return terminal
            try:
                _cleanup_stale_temps(final_dir)
            except OSError:
                return {"status": "BLOCKED", "reason_code": "STALE_TEMP_INVALID"}
            if marker_path.exists():
                marker = _read_canonical_mapping(marker_path)
                orphan_id = _valid_marker(marker) if marker is not None else None
                terminal = _negative_terminal(
                    orphan_id or "0" * 64,
                    "INTERRUPTED_AFTER_MARKER" if orphan_id else "INTERNAL_ERROR",
                )
                _write_atomic(terminal_path, _canonical_json(terminal))
                return terminal
            try:
                if (
                    bootstrap_preflight is not None
                    and bootstrap_preflight().get("status") != "PASS"
                ):
                    return {"status": "BLOCKED", "reason_code": "PREFLIGHT_FAILED"}
                git_commit, protocol_sha256 = identity()
                attempt_id = _attempt_id(git_commit, protocol_sha256)
                if preflight is not None and preflight().get("status") != "PASS":
                    return {"status": "BLOCKED", "reason_code": "PREFLIGHT_FAILED"}
            except Exception:
                return {"status": "BLOCKED", "reason_code": "PREFLIGHT_INTERNAL"}
            marker = {
                "attempt_id": attempt_id,
                "git_commit": git_commit,
                "protocol_sha256": protocol_sha256,
                "schema_version": 1,
                "status": "RUNNING",
            }
            _write_exclusive(marker_path, _canonical_json(marker))
            try:
                if effect_with_final_dir is not None:
                    if final_fd is None:
                        raise ValueError("pinned final directory descriptor is required")
                    report = effect_with_final_dir(final_dir, final_fd)
                else:
                    report = effect()
                if not isinstance(report, bytes) or _report_status(report) != _SUCCESS_STATUS:
                    raise ValueError("invalid final report")
                _write_atomic(report_path, report)
                persisted = report_path.read_bytes()
                if not _valid_report(persisted):
                    raise OSError("persisted final report invalid")
                terminal = {
                    "attempt_id": attempt_id,
                    "reason_code": "ALL_GATES_PASSED",
                    "report_sha256": hashlib.sha256(persisted).hexdigest(),
                    "schema_version": 1,
                    "status": _SUCCESS_STATUS,
                }
            except FinalGateFailure as error:
                if _report_status(error.report) != "HONEST_NEGATIVE":
                    terminal = _negative_terminal(attempt_id, "INTERNAL_ERROR")
                else:
                    _write_atomic(report_path, error.report)
                    persisted = report_path.read_bytes()
                    if not _valid_report(persisted):
                        terminal = _negative_terminal(attempt_id, "INTERNAL_ERROR")
                    else:
                        terminal = _negative_terminal(
                            attempt_id, "GATE_FAILED", hashlib.sha256(persisted).hexdigest()
                        )
            except Exception:
                terminal = _negative_terminal(attempt_id, "INTERNAL_ERROR")
            _write_atomic(terminal_path, _canonical_json(terminal))
            return terminal
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run_final_once_marker_protocol(
    final_dir: Path,
    *,
    identity: Callable[[], tuple[str, str]] | None = None,
    git_commit: str | None = None,
    protocol_sha256: str | None = None,
    bootstrap_preflight: Callable[[], Mapping[str, object]] | None = None,
    preflight: Callable[[], Mapping[str, object]] | None = None,
    effect: Callable[[], bytes],
    effect_with_final_dir: Callable[[Path, int], bytes] | None = None,
) -> Mapping[str, object]:
    """Run once, retaining canonical directory FDs through recovery and writes."""
    canonical = _is_canonical_final_dir(final_dir)
    try:
        if canonical:
            _validate_canonical_final_path(final_dir)
        if bootstrap_preflight is not None and bootstrap_preflight().get("status") != "PASS":
            return {"status": "BLOCKED", "reason_code": "PREFLIGHT_FAILED"}
    except OSError:
        return {"status": "BLOCKED", "reason_code": "REPORTS_PARENT_UNSAFE"}

    if not canonical:
        return _run_final_once_marker_protocol(
            final_dir,
            identity=identity,
            git_commit=git_commit,
            protocol_sha256=protocol_sha256,
            bootstrap_preflight=bootstrap_preflight,
            preflight=preflight,
            effect=effect,
            effect_with_final_dir=effect_with_final_dir,
        )

    root_fd = reports_fd = final_fd = -1
    try:
        root_fd, reports_fd, final_fd = _open_or_create_canonical_final_dir(final_dir)
        work_dir = Path(f"/proc/self/fd/{final_fd}")
        return _run_final_once_marker_protocol(
            work_dir,
            identity=identity,
            git_commit=git_commit,
            protocol_sha256=protocol_sha256,
            bootstrap_preflight=bootstrap_preflight,
            preflight=preflight,
            effect=effect,
            effect_with_final_dir=effect_with_final_dir,
            path_prepared=True,
            final_fd=final_fd,
        )
    except OSError:
        return {"status": "BLOCKED", "reason_code": "REPORTS_PARENT_UNSAFE"}
    finally:
        for descriptor in (final_fd, reports_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)
