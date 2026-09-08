"""Small, real PostgreSQL primitives used by the AC01--AC07 rehearsal tests."""

from __future__ import annotations

import concurrent.futures
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

import psycopg

from migration_rehearsal.ci import verify_ci_expectations  # noqa: F401
from migration_rehearsal.clients import write_v1, write_v2
from migration_rehearsal.demo import render_demo_and_report  # noqa: F401
from migration_rehearsal.final_once import (  # noqa: F401
    FinalGateFailure,
    run_final_once_marker_protocol,
)
from migration_rehearsal.sync import (
    BACKFILL_TIMEOUT_SECONDS,
    BackfillContentionWindow,
    ClientWindow,
)

ADVISORY_KEY: Final = 7_241_830_001
MIGRATIONS_DIR: Final = Path(__file__).resolve().parents[2] / "migrations"
Observer = Callable[[str, Mapping[str, int]], None]
BatchRejectedObserver = Callable[[int], None]
PidObserver = Callable[[int], None]
RoleAssumer = Callable[[psycopg.Cursor[tuple[object, ...]]], None]
V2Insert = Callable[[psycopg.Cursor[tuple[object, ...]]], None]
EXPECTED_PHASE: Final = {
    "0002": "initial",
    "0003": "expand",
    "0004": "backfill",
    "0005": "switch",
}


def _migration_path(version: str) -> Path:
    matches = sorted(MIGRATIONS_DIR.glob(f"{version}_*.sql"))
    if len(matches) != 1:
        raise RuntimeError(f"expected one migration for {version}")
    return matches[0]


def _migration_bytes(version: str) -> bytes:
    return _migration_path(version).read_bytes()


def _assume_migrator(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute("SET LOCAL ROLE pmr_migrator")
    cursor.execute("SELECT session_user, current_user")
    if cursor.fetchone() != ("rehearsal_app", "pmr_migrator"):
        raise RuntimeError("migration role assertion failed")


def _assume_owner(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute("SET LOCAL ROLE pmr_owner")
    cursor.execute("SELECT session_user, current_user")
    if cursor.fetchone() != ("rehearsal_app", "pmr_owner"):
        raise RuntimeError("owner role assertion failed")


def _assume_v1(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute("SET LOCAL ROLE pmr_app_v1")
    cursor.execute("SELECT session_user, current_user")
    if cursor.fetchone() != ("rehearsal_app", "pmr_app_v1"):
        raise RuntimeError("v1 role assertion failed")


def _assume_v2(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute("SET LOCAL ROLE pmr_app_v2")
    cursor.execute("SELECT session_user, current_user")
    if cursor.fetchone() != ("rehearsal_app", "pmr_app_v2"):
        raise RuntimeError("v2 role assertion failed")


def _apply_migration(
    dsn: str,
    version: str,
    *,
    observe_pid: PidObserver | None = None,
    fail_before_commit: bool = False,
) -> bool:
    content = _migration_bytes(version)
    checksum = hashlib.sha256(content).hexdigest()
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            _assume_migrator(cursor)
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SELECT pg_backend_pid()")
            pid_row = cursor.fetchone()
            if pid_row is None:
                raise RuntimeError("cannot read migration PID")
            if observe_pid is not None:
                observe_pid(int(pid_row[0]))
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
            cursor.execute("SELECT to_regclass('schema_migrations')")
            ledger_exists = cursor.fetchone() == ("schema_migrations",)
        if ledger_exists:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT sha256 FROM schema_migrations WHERE version = %s", (version,)
                )
                row = cursor.fetchone()
            if row is not None:
                if row[0] != checksum:
                    raise RuntimeError(f"migration checksum mismatch for {version}")
                connection.commit()
                return False
        if version in EXPECTED_PHASE:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT phase::text FROM migration_control WHERE singleton FOR UPDATE"
                )
                phase_row = cursor.fetchone()
            if phase_row != (EXPECTED_PHASE[version],):
                raise RuntimeError(f"unexpected phase before {version}")
        if version == "0004":
            with connection.cursor() as cursor:
                cursor.execute("SELECT v2_enabled FROM migration_control WHERE singleton")
                if cursor.fetchone() != (True,):
                    raise RuntimeError("switch requires v2")
                cursor.execute(
                    "SELECT count(*) FROM invoices WHERE amount_minor IS NULL "
                    "OR amount_cents <> amount_minor OR currency_code IS DISTINCT FROM 'EUR'"
                )
                if cursor.fetchone() != (0,):
                    raise RuntimeError("switch requires complete backfill")
        with connection.cursor() as cursor:
            cursor.execute(content.decode("utf-8"))
        if version == "0001":
            _seed_invoices(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO schema_migrations (version, sha256) VALUES (%s, %s)",
                (version, checksum),
            )
        if fail_before_commit:
            raise RuntimeError(f"injected migration failure for {version}")
        connection.commit()
        return True


def _seed_invoices(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO invoices (id, amount_cents) "
            "SELECT id, ((id * 7919) % 1000000) + 1 FROM generate_series(1, 4096) AS id "
            "ON CONFLICT (id) DO NOTHING"
        )


def _bootstrap(dsn: str, through: str) -> None:
    versions = ("0001", "0002", "0003", "0004", "0005")
    if through not in versions:
        raise RuntimeError(f"unsupported migration target {through}")
    for version in versions:
        _apply_migration(dsn, version)
        if version == through:
            return


def verify_environment(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            row = cursor.fetchone()
    if row != ("180006",):
        raise RuntimeError("PostgreSQL 18.6 is required")


def rebase_fresh_database(*, database_dsns: Sequence[str]) -> None:
    if len(database_dsns) != 3:
        raise RuntimeError("expected lane R and two lane F databases")
    for dsn in database_dsns:
        _bootstrap(dsn, "0003")


def exercise_trigger_truth_table(dsn: str) -> None:
    _bootstrap(dsn, "0003")


def read_control_ledger(dsn: str, *, observe: Observer) -> None:
    _bootstrap(dsn, "0001")
    observe("initial_committed", {})
    try:
        _apply_migration(dsn, "0002", fail_before_commit=True)
    except RuntimeError as error:
        if str(error) != "injected migration failure for 0002":
            raise
    observe("failed_expand_rolled_back", {})
    _bootstrap(dsn, "0003")


def observe_advisory_phase_gate(dsn: str, *, observe: Observer) -> Mapping[str, object]:
    _bootstrap(dsn, "0001")
    with psycopg.connect(dsn) as holder:
        with holder.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("cannot read holder PID")
            holder_pid = int(row[0])
            cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (ADVISORY_KEY,))
        observe("shared_before_phase", {"holder_pid": holder_pid})

        actors: dict[str, int] = {"holder_pid": holder_pid}

        def record_waiter_pid(pid: int) -> None:
            actors["waiter_pid"] = pid

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_apply_migration, dsn, "0002", observe_pid=record_waiter_pid)
            deadline = time.monotonic() + 1.5
            observed = False
            with psycopg.connect(dsn, autocommit=True) as observer:
                while time.monotonic() < deadline and not future.done():
                    waiter_pid = actors.get("waiter_pid")
                    if waiter_pid is None:
                        continue
                    with observer.cursor() as cursor:
                        cursor.execute(
                            "SELECT granted FROM pg_locks WHERE pid = %s AND locktype = 'advisory' "
                            "AND mode = 'ExclusiveLock'",
                            (waiter_pid,),
                        )
                        locks = cursor.fetchall()
                    if any(granted is False for (granted,) in locks):
                        observe("exclusive_waiting", actors)
                        observed = True
                        break
            if not observed:
                raise RuntimeError("exclusive advisory waiter was not observed")
            holder.commit()
            future.result(timeout=3)
    return {"advisory_key": ADVISORY_KEY, "shared_before_phase": True}


def prepare_expand_ddl_contention(dsn: str) -> None:
    _bootstrap(dsn, "0001")


def _start_backfill(dsn: str) -> None:
    _bootstrap(dsn, "0003")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            _assume_migrator(cursor)
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
            cursor.execute("SELECT singleton FROM migration_control WHERE singleton FOR UPDATE")
            cursor.execute("UPDATE migration_control SET phase = 'backfill' WHERE singleton")
        connection.commit()


def _run_backfill_batch(
    dsn: str,
    *,
    rollback_after_update: bool = False,
    rejected_observer: BatchRejectedObserver | None = None,
    causal_events: list[str] | None = None,
) -> tuple[int, int]:
    """Run one bounded, resumable batch; the optional failpoint always rolls back."""
    with psycopg.connect(dsn) as connection:
        try:
            with connection.cursor() as cursor:
                _assume_migrator(cursor)
                cursor.execute("SET LOCAL lock_timeout = '250ms'")
                cursor.execute("SET LOCAL statement_timeout = '2s'")
                cursor.execute("SELECT pg_backend_pid()")
                pid_row = cursor.fetchone()
                if pid_row is None:
                    raise RuntimeError("cannot read backfill PID")
                cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (ADVISORY_KEY,))
                if causal_events is not None:
                    causal_events.append("shared")
                cursor.execute("SELECT phase::text FROM migration_control WHERE singleton")
                phase_row = cursor.fetchone()
                if phase_row is None:
                    raise RuntimeError("missing migration control")
                if phase_row != ("backfill",):
                    if rejected_observer is not None:
                        rejected_observer(int(pid_row[0]))
                    raise RuntimeError("BACKFILL_PHASE_REJECTED")
                cursor.execute(
                    "SELECT phase::text, backfill_last_id FROM migration_control "
                    "WHERE singleton FOR UPDATE"
                )
                if causal_events is not None:
                    causal_events.append("singleton")
                control = cursor.fetchone()
                if control is None:
                    raise RuntimeError("missing migration control")
                if control[0] != "backfill":
                    if rejected_observer is not None:
                        rejected_observer(int(pid_row[0]))
                    raise RuntimeError("BACKFILL_PHASE_REJECTED")
                cursor.execute(
                    "SELECT id FROM invoices WHERE id > %s AND amount_minor IS NULL ORDER BY id "
                    "LIMIT 256 FOR UPDATE",
                    (control[1],),
                )
                if causal_events is not None:
                    causal_events.append("invoice")
                rows = cursor.fetchall()
                if not rows:
                    connection.commit()
                    return 0, int(pid_row[0])
                ids = [int(row[0]) for row in rows]
                cursor.execute(
                    "UPDATE invoices SET amount_minor = amount_cents, currency_code = 'EUR' "
                    "WHERE id = ANY(%s)",
                    (ids,),
                )
                cursor.execute(
                    "UPDATE migration_control SET backfill_last_id = %s, "
                    "backfill_scanned_rows = backfill_scanned_rows + %s WHERE singleton",
                    (ids[-1], len(ids)),
                )
                if causal_events is not None:
                    causal_events.append("checkpoint")
            if rollback_after_update:
                connection.rollback()
                return len(ids), int(pid_row[0])
            connection.commit()
            return len(ids), int(pid_row[0])
        except Exception:
            connection.rollback()
            raise


def _finish_backfill_enable_v2(dsn: str) -> None:

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            _assume_migrator(cursor)
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
            cursor.execute("SELECT phase::text FROM migration_control WHERE singleton FOR UPDATE")
            phase = cursor.fetchone()
            if phase != ("backfill",):
                raise RuntimeError("cannot enable v2 outside backfill")
            cursor.execute(
                "SELECT count(*) FROM invoices WHERE amount_minor IS NULL "
                "OR amount_cents <> amount_minor OR currency_code IS DISTINCT FROM 'EUR'"
            )
            mismatch = cursor.fetchone()
            if mismatch != (0,):
                raise RuntimeError("cannot enable v2 before complete backfill")
            cursor.execute(
                "GRANT SELECT (id, amount_minor, currency_code), "
                "UPDATE (amount_minor, currency_code), "
                "INSERT (id, amount_minor, currency_code) "
                "ON TABLE invoices TO pmr_app_v2"
            )
            cursor.execute("UPDATE migration_control SET v2_enabled = true WHERE singleton")
        connection.commit()


def _enter_backfill(dsn: str) -> None:
    """Compatibility helper retained for AC07; AC08 drives individual batches itself."""
    _start_backfill(dsn)
    while _run_backfill_batch(dsn)[0] > 0:
        pass
    _finish_backfill_enable_v2(dsn)


def _backfill_v1_actor(
    dsn: str, *, window: BackfillContentionWindow, actors: dict[str, int]
) -> None:
    with psycopg.connect(dsn) as connection:
        try:
            with connection.cursor() as cursor:
                _assume_v1(cursor)
                cursor.execute("SET LOCAL lock_timeout = '250ms'")
                cursor.execute("SET LOCAL statement_timeout = '2s'")
                cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (ADVISORY_KEY,))
                cursor.execute("SELECT phase::text FROM migration_control WHERE singleton")
                phase = cursor.fetchone()
                if phase != ("backfill",):
                    raise RuntimeError("CLIENT_PHASE_REJECTED")
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()
                if pid is None:
                    raise RuntimeError("cannot read V1 PID")
                actors["v1_pid"] = int(pid[0])
            window.worker_pid_ready()
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE invoices SET amount_cents = amount_cents + 17 WHERE id = 513"
                )
            window.worker_dml_done()
            window.worker_wait_for_commit_release()
            connection.commit()
        except Exception:
            window.abort()
            connection.rollback()
            raise


def _backfill_digest(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, amount_minor, currency_code FROM invoices ORDER BY id")
            rows = cursor.fetchall()
    stream = "".join(
        "|".join("NULL" if value is None else str(value) for value in row) + "\n" for row in rows
    ).encode("utf-8")
    return hashlib.sha256(stream).hexdigest()


def _migration_checksum(version: str) -> str:
    return hashlib.sha256(_migration_bytes(version)).hexdigest()


def attempt_down(dsn: str, *, observe_pid: PidObserver | None = None) -> None:
    """Atomically return an expand/compatibility/backfill/switch database to initial."""
    with psycopg.connect(dsn) as connection:
        try:
            with connection.cursor() as cursor:
                _assume_migrator(cursor)
                cursor.execute("SET LOCAL lock_timeout = '1s'")
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                cursor.execute("SELECT pg_backend_pid()")
                pid_row = cursor.fetchone()
                if pid_row is None:
                    raise RuntimeError("cannot read down PID")
                if observe_pid is not None:
                    observe_pid(int(pid_row[0]))
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
                cursor.execute(
                    "SELECT phase::text FROM migration_control WHERE singleton FOR UPDATE"
                )
                phase_row = cursor.fetchone()
                if phase_row == ("contract",):
                    cursor.execute("SELECT count(*) FROM invoices WHERE currency_code = 'USD'")
                    if cursor.fetchone() != (0,):
                        raise RuntimeError("DOWN_FORBIDDEN_USD")
                    raise RuntimeError("DOWN_PHASE_REJECTED")
                if phase_row not in {("expand",), ("compatibility",), ("backfill",), ("switch",)}:
                    raise RuntimeError("DOWN_PHASE_REJECTED")
                phase = str(phase_row[0])
                versions = ["0001"]
                if phase in {"expand", "compatibility", "backfill", "switch"}:
                    versions.append("0002")
                if phase in {"compatibility", "backfill", "switch"}:
                    versions.append("0003")
                if phase == "switch":
                    versions.append("0004")
                cursor.execute("SELECT version, sha256 FROM schema_migrations ORDER BY version")
                expected = [(version, _migration_checksum(version)) for version in versions]
                if cursor.fetchall() != expected:
                    raise RuntimeError("DOWN_LEDGER_REJECTED")
                if phase in {"expand", "compatibility", "backfill", "switch"}:
                    cursor.execute(
                        "SELECT count(*) FROM invoices WHERE "
                        "(amount_minor IS NULL) <> (currency_code IS NULL) OR "
                        "(amount_minor IS NOT NULL AND (amount_minor IS DISTINCT FROM amount_cents "
                        "OR currency_code IS DISTINCT FROM 'EUR'))"
                    )
                    if cursor.fetchone() != (0,):
                        raise RuntimeError("DOWN_REPRESENTABILITY_REJECTED")
                cursor.execute("REVOKE ALL ON TABLE invoices FROM pmr_app_v2")
                if phase == "switch":
                    cursor.execute("ALTER TABLE invoices ALTER COLUMN amount_minor DROP NOT NULL")
                    cursor.execute("ALTER TABLE invoices ALTER COLUMN currency_code DROP NOT NULL")
                if phase in {"compatibility", "backfill", "switch"}:
                    cursor.execute("DROP TRIGGER invoices_compatibility_write ON invoices")
                    cursor.execute("DROP FUNCTION invoices_compatibility_write()")
                if phase in {"expand", "compatibility", "backfill", "switch"}:
                    cursor.execute(
                        "ALTER TABLE invoices DROP CONSTRAINT invoices_amount_minor_nonnegative"
                    )
                    cursor.execute(
                        "ALTER TABLE invoices DROP CONSTRAINT invoices_currency_code_eur"
                    )
                    cursor.execute("ALTER TABLE invoices DROP COLUMN currency_code")
                    cursor.execute("ALTER TABLE invoices DROP COLUMN amount_minor")
                cursor.execute(
                    "UPDATE migration_control SET phase = 'initial', backfill_last_id = 0, "
                    "backfill_scanned_rows = 0, v2_enabled = false WHERE singleton"
                )
                cursor.execute("DELETE FROM schema_migrations WHERE version <> '0001'")
                cursor.execute(
                    "GRANT SELECT (id, amount_cents), UPDATE (amount_cents) "
                    "ON TABLE invoices TO pmr_app_v1"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _down_to_initial(dsn: str, *, observe_pid: PidObserver | None = None) -> None:
    """Compatibility wrapper: all down paths use attempt_down."""
    attempt_down(dsn, observe_pid=observe_pid)


def _to_switch(dsn: str) -> None:
    _enter_backfill(dsn)
    _apply_migration(dsn, "0004")


def _write_fixed_v1_513(dsn: str) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            _assume_v1(cursor)
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (ADVISORY_KEY,))
            cursor.execute("SELECT phase::text FROM migration_control WHERE singleton")
            if cursor.fetchone() != ("backfill",):
                raise RuntimeError("CLIENT_PHASE_REJECTED")
            cursor.execute("UPDATE invoices SET amount_cents = amount_cents + 17 WHERE id = 513")
        connection.commit()


def _workload_to_backfill(dsn: str) -> None:
    _enter_backfill(dsn)
    _write_fixed_v1_513(dsn)
    run_concurrent_clients(dsn, observe=lambda _stage, _actors: None)


def _workload_to_switch(dsn: str) -> None:
    _workload_to_backfill(dsn)
    _apply_migration(dsn, "0004")


def _legacy_column_sqlstate(dsn: str, assume_role: RoleAssumer) -> str:
    with psycopg.connect(dsn) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '1s'")
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                assume_role(cursor)
                cursor.execute("SELECT amount_cents FROM invoices LIMIT 1")
            connection.commit()
        except psycopg.Error as error:
            connection.rollback()
            if error.sqlstate is None:
                raise RuntimeError("legacy probe omitted SQLSTATE") from error
            return error.sqlstate
    raise RuntimeError("legacy column unexpectedly exists")


def _v2_insert_null_amount(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute(
        "INSERT INTO invoices (id, amount_minor, currency_code) VALUES (910001, NULL, 'EUR')"
    )


def _v2_insert_null_currency(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute(
        "INSERT INTO invoices (id, amount_minor, currency_code) VALUES (910002, 1, NULL)"
    )


def _v2_insert_negative(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute(
        "INSERT INTO invoices (id, amount_minor, currency_code) VALUES (910003, -1, 'EUR')"
    )


def _v2_insert_wrong_currency(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute(
        "INSERT INTO invoices (id, amount_minor, currency_code) VALUES (910004, 1, 'GBP')"
    )


def _v2_insert_sqlstate(dsn: str, insert: V2Insert) -> str:
    with psycopg.connect(dsn) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '1s'")
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                _assume_v2(cursor)
                insert(cursor)
            connection.commit()
        except psycopg.Error as error:
            connection.rollback()
            if error.sqlstate is None:
                raise RuntimeError("v2 insert probe omitted SQLSTATE") from error
            return error.sqlstate
    raise RuntimeError("invalid V2 insert unexpectedly succeeded")


def compute_contract_digests(dsn: str, *, lane_dsns: Sequence[str], observe: Observer) -> None:
    if len(lane_dsns) != 3:
        raise RuntimeError("expected three digest lanes")
    _workload_to_backfill(dsn)
    observe("compatibility", {})
    _apply_migration(dsn, "0004")
    observe("logical", {})
    _apply_migration(dsn, "0005")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            _assume_v2(cursor)
            cursor.execute(
                "INSERT INTO invoices (id, amount_minor, currency_code) VALUES (5001, 12345, 'USD')"
            )
        connection.commit()
    observe("usd", {})
    lane_r, lane_f1, lane_f2 = lane_dsns
    _workload_to_switch(lane_r)
    observe("lane_r_pre_down", {})
    attempt_down(lane_r)
    observe("lane_r", {})
    _workload_to_switch(lane_f1)
    observe("lane_f_1", {})
    _workload_to_switch(lane_f2)
    observe("lane_f_2", {})


def exercise_rollback_reforward_and_0005(dsn: str, *, observe: Observer) -> None:
    _workload_to_switch(dsn)
    observe("before_down", {})
    attempt_down(dsn)
    observe("down_committed", {})
    _bootstrap(dsn, "0003")
    _enter_backfill(dsn)
    _apply_migration(dsn, "0004")
    observe("reforward_committed", {})
    failed: dict[str, int] = {}
    try:
        _apply_migration(
            dsn,
            "0005",
            observe_pid=lambda pid: failed.__setitem__("migration_pid", pid),
            fail_before_commit=True,
        )
    except RuntimeError as error:
        if str(error) != "injected migration failure for 0005":
            raise
    observe("migration_0005_rolled_back", failed)
    committed: dict[str, int] = {}
    _apply_migration(
        dsn, "0005", observe_pid=lambda pid: committed.__setitem__("migration_pid", pid)
    )
    observe("migration_0005_committed", committed)


def exercise_post_contract_rejections(dsn: str) -> Mapping[str, str]:
    _to_switch(dsn)
    _apply_migration(dsn, "0005")
    try:
        attempt_v1_write(dsn)
    except RuntimeError as error:
        if str(error) != "CLIENT_PHASE_REJECTED":
            raise
    else:
        raise RuntimeError("V1 write unexpectedly passed after contract")
    evidence = {
        "legacy_owner": _legacy_column_sqlstate(dsn, _assume_owner),
        "legacy_migrator": _legacy_column_sqlstate(dsn, _assume_migrator),
        "v2_invalid_null_amount": _v2_insert_sqlstate(dsn, _v2_insert_null_amount),
        "v2_invalid_null_currency": _v2_insert_sqlstate(dsn, _v2_insert_null_currency),
        "v2_invalid_negative": _v2_insert_sqlstate(dsn, _v2_insert_negative),
        "v2_invalid_currency": _v2_insert_sqlstate(dsn, _v2_insert_wrong_currency),
    }
    expected = {
        "legacy_owner": "42703",
        "legacy_migrator": "42703",
        "v2_invalid_null_amount": "23502",
        "v2_invalid_null_currency": "23502",
        "v2_invalid_negative": "23514",
        "v2_invalid_currency": "23514",
    }
    if evidence != expected:
        raise RuntimeError("post-contract SQLSTATE mismatch")
    return evidence


def attempt_v1_write(dsn: str, *, rejected_observer: BatchRejectedObserver | None = None) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            _assume_v1(cursor)
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (ADVISORY_KEY,))
            cursor.execute("SELECT phase::text FROM migration_control WHERE singleton")
            allowed_phases = {("initial",), ("expand",), ("compatibility",), ("backfill",)}
            if cursor.fetchone() not in allowed_phases:
                cursor.execute("SELECT pg_backend_pid()")
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("cannot read V1 PID")
                if rejected_observer is not None:
                    rejected_observer(int(row[0]))
                raise RuntimeError("CLIENT_PHASE_REJECTED")
        write_v1(connection)
        connection.commit()


def exercise_backfill_resume_and_down(dsn: str, *, observe: Observer) -> Mapping[str, object]:
    """Run the observed backfill, atomic down, and post-down rejection rehearsal."""
    _start_backfill(dsn)
    first, _ = _run_backfill_batch(dsn)
    second, _ = _run_backfill_batch(dsn)
    if (first, second) != (256, 256):
        raise RuntimeError("unexpected initial backfill batch sizes")

    actors: dict[str, int] = {}
    error_sqlstates: list[str] = []
    failpoint_batch_effective_role: str | None = None

    def record_psycopg_error(error: psycopg.Error) -> None:
        if error.sqlstate is not None:
            error_sqlstates.append(error.sqlstate)

    window = BackfillContentionWindow()
    with psycopg.connect(dsn) as batch:
        try:
            with batch.cursor() as cursor:
                _assume_migrator(cursor)
                cursor.execute("SELECT current_user")
                role_row = cursor.fetchone()
                if role_row != ("pmr_migrator",):
                    raise RuntimeError("failpoint batch role assertion failed")
                failpoint_batch_effective_role = str(role_row[0])
                cursor.execute("SET LOCAL lock_timeout = '250ms'")
                cursor.execute("SET LOCAL statement_timeout = '2s'")
                cursor.execute("SELECT pg_backend_pid()")
                batch_pid = cursor.fetchone()
                if batch_pid is None:
                    raise RuntimeError("cannot read batch PID")
                actors["batch_pid"] = int(batch_pid[0])
                cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (ADVISORY_KEY,))
                cursor.execute(
                    "SELECT phase::text, backfill_last_id FROM migration_control "
                    "WHERE singleton FOR UPDATE"
                )
                if cursor.fetchone() != ("backfill", 512):
                    raise RuntimeError("unexpected batch3 control state")
                cursor.execute(
                    "SELECT id FROM invoices WHERE id > 512 AND amount_minor IS NULL ORDER BY id "
                    "LIMIT 256 FOR UPDATE"
                )
                ids = [int(row[0]) for row in cursor.fetchall()]
                if ids != list(range(513, 769)):
                    raise RuntimeError("unexpected batch3 IDs")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_backfill_v1_actor, dsn, window=window, actors=actors)
                try:
                    window.coordinator_wait_for_worker_pid(future)
                except psycopg.Error as error:
                    record_psycopg_error(error)
                    raise
                deadline = time.monotonic() + BACKFILL_TIMEOUT_SECONDS
                blocked = False
                with psycopg.connect(dsn, autocommit=True) as observer:
                    while time.monotonic() < deadline:
                        with observer.cursor() as cursor:
                            cursor.execute("SELECT pg_blocking_pids(%s)", (actors["v1_pid"],))
                            row = cursor.fetchone()
                        if row == ([actors["batch_pid"]],):
                            blocked = True
                            break
                if not blocked:
                    raise RuntimeError("V1 writer did not block on batch3 row")
                with batch.cursor() as cursor:
                    cursor.execute(
                        "UPDATE invoices SET amount_minor = amount_cents, currency_code = 'EUR' "
                        "WHERE id = ANY(%s)",
                        (ids,),
                    )
                    cursor.execute(
                        "UPDATE migration_control SET backfill_last_id = 768, "
                        "backfill_scanned_rows = backfill_scanned_rows + 256 WHERE singleton"
                    )
                batch.rollback()
                with psycopg.connect(dsn, autocommit=True) as observer:
                    with observer.cursor() as cursor:
                        cursor.execute("SELECT backfill_last_id FROM migration_control")
                        checkpoint_row = cursor.fetchone()
                if checkpoint_row is None:
                    raise RuntimeError("cannot read checkpoint after rollback")
                checkpoint_after_failpoint = int(checkpoint_row[0])
                try:
                    window.coordinator_wait_for_worker_dml(future)
                except psycopg.Error as error:
                    record_psycopg_error(error)
                    raise
                observe("batch3_rolled_back", actors)
                window.release_commit()
                try:
                    future.result(timeout=BACKFILL_TIMEOUT_SECONDS)
                except psycopg.Error as error:
                    record_psycopg_error(error)
                    raise
            observe("v1_committed", actors)
        except Exception:
            window.abort()
            batch.rollback()
            raise

    with psycopg.connect(dsn, autocommit=True) as verifier:
        with verifier.cursor() as cursor:
            cursor.execute("SELECT amount_cents, amount_minor FROM invoices WHERE id = 513")
            preserved_row = cursor.fetchone()
    expected_513 = ((513 * 7919) % 1_000_000) + 18
    id_513_preserved = preserved_row == (expected_513, expected_513)

    causal_events: list[str] = []
    down_contention_batch_effective_role: str | None = None
    while _run_backfill_batch(dsn, causal_events=causal_events)[0] > 0:
        pass
    observe("backfill_complete", actors)
    digest = _backfill_digest(dsn)
    second_pass_rows, _ = _run_backfill_batch(dsn, causal_events=causal_events)
    if second_pass_rows != 0 or _backfill_digest(dsn) != digest:
        raise RuntimeError("backfill idempotence check failed")
    observe("second_pass_complete", actors)
    with psycopg.connect(dsn) as active_batch:
        try:
            with active_batch.cursor() as cursor:
                _assume_migrator(cursor)
                cursor.execute("SELECT current_user")
                role_row = cursor.fetchone()
                if role_row != ("pmr_migrator",):
                    raise RuntimeError("active batch role assertion failed")
                down_contention_batch_effective_role = str(role_row[0])
                cursor.execute("SET LOCAL lock_timeout = '250ms'")
                cursor.execute("SET LOCAL statement_timeout = '2s'")
                cursor.execute("SELECT pg_backend_pid()")
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("cannot read active batch PID")
                down_actors: dict[str, int] = {"batch_pid": int(row[0])}
                cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (ADVISORY_KEY,))
                cursor.execute(
                    "SELECT phase::text FROM migration_control WHERE singleton FOR UPDATE"
                )
                if cursor.fetchone() != ("backfill",):
                    raise RuntimeError("active batch is outside backfill")
                cursor.execute(
                    "SELECT id FROM invoices WHERE id BETWEEN 1 AND 256 ORDER BY id FOR UPDATE"
                )
                cursor.fetchall()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    _down_to_initial,
                    dsn,
                    observe_pid=lambda pid: down_actors.__setitem__("down_waiter_pid", pid),
                )
                deadline = time.monotonic() + BACKFILL_TIMEOUT_SECONDS
                down_waiter_granted = True
                while time.monotonic() < deadline:
                    if future.done():
                        try:
                            future.result(timeout=0)
                        except psycopg.Error as error:
                            record_psycopg_error(error)
                            raise
                        raise RuntimeError("down completed before lock observation")
                    waiter_pid = down_actors.get("down_waiter_pid")
                    if waiter_pid is None:
                        continue
                    with psycopg.connect(dsn, autocommit=True) as observer:
                        with observer.cursor() as cursor:
                            cursor.execute(
                                "SELECT granted FROM pg_locks WHERE pid = %s "
                                "AND locktype = 'advisory' AND mode = 'ExclusiveLock' "
                                "AND objsubid = 1 "
                                "AND ((classid::bigint << 32) | objid::bigint) = %s",
                                (waiter_pid, ADVISORY_KEY),
                            )
                            waiter_lock = cursor.fetchone()
                    if waiter_lock == (False,):
                        down_waiter_granted = False
                        break
                if down_waiter_granted:
                    raise RuntimeError("down exclusive advisory waiter was not observed")
                observe("down_waiting", down_actors)
                active_batch.commit()
                try:
                    future.result(timeout=BACKFILL_TIMEOUT_SECONDS)
                except psycopg.Error as error:
                    record_psycopg_error(error)
                    raise
        except Exception:
            active_batch.rollback()
            raise

    rejection: dict[str, int] = {}

    def observe_rejection(pid: int) -> None:
        rejection["new_batch_pid"] = pid
        observe("new_batch_rejected", rejection)

    try:
        _run_backfill_batch(
            dsn,
            rejected_observer=observe_rejection,
        )
    except RuntimeError as error:
        new_batch_error = str(error)
    else:
        raise RuntimeError("new backfill batch unexpectedly succeeded")
    if new_batch_error != "BACKFILL_PHASE_REJECTED" or set(rejection) != {"new_batch_pid"}:
        raise RuntimeError("post-down batch rejection was not observed")
    if failpoint_batch_effective_role != "pmr_migrator":
        raise RuntimeError("failpoint batch role was not observed")
    if down_contention_batch_effective_role != "pmr_migrator":
        raise RuntimeError("active batch role was not observed")
    return {
        "checkpoint_after_failpoint": checkpoint_after_failpoint,
        "id_513_preserved": id_513_preserved,
        "down_waiter_granted": down_waiter_granted,
        "deadlock_sqlstate": next((state for state in error_sqlstates if state == "40P01"), None),
        "new_batch_error": new_batch_error,
        "second_pass_rows": second_pass_rows,
        "causal_order": causal_events[:4],
        "error_sqlstates": list(error_sqlstates),
        "backfill_digest": digest,
        "failpoint_batch_effective_role": failpoint_batch_effective_role,
        "down_contention_batch_effective_role": down_contention_batch_effective_role,
    }


def _client_actor(dsn: str, *, v2: bool, window: ClientWindow) -> tuple[int, int, int]:
    with psycopg.connect(dsn) as connection:
        try:
            with connection.cursor() as cursor:
                if v2:
                    _assume_v2(cursor)
                else:
                    _assume_v1(cursor)
                cursor.execute("SET LOCAL lock_timeout = '1s'")
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (ADVISORY_KEY,))
                cursor.execute(
                    "SELECT phase::text, v2_enabled FROM migration_control WHERE singleton"
                )
                state = cursor.fetchone()
                if state is None or state[0] != "backfill" or (v2 and state[1] is not True):
                    raise RuntimeError("CLIENT_PHASE_REJECTED")
            window.worker_after_admission()
            updated = write_v2(connection) if v2 else write_v1(connection)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid(), txid_current()")
                identity = cursor.fetchone()
            if (
                identity is None
                or not isinstance(identity[0], int)
                or not isinstance(identity[1], int)
            ):
                raise RuntimeError("invalid client transaction identity")
            window.record_actor("v2" if v2 else "v1", (int(identity[0]), int(identity[1]), updated))
            window.worker_after_dml()
            window.worker_wait_for_commit_release()
            connection.commit()
            return int(identity[0]), int(identity[1]), updated
        except Exception:
            window.abort()
            connection.rollback()
            raise


def run_concurrent_clients(dsn: str, *, observe: Observer) -> Mapping[str, object]:
    """Runs both compatibility clients in a named, observed transaction window."""
    _enter_backfill(dsn)
    window = ClientWindow()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        v1_future = executor.submit(_client_actor, dsn, v2=False, window=window)
        v2_future = executor.submit(_client_actor, dsn, v2=True, window=window)
        try:
            window.coordinator_after_dml()
            actors = window.actors()
            v1_pid, v1_txid, v1_updates = actors["v1"]
            v2_pid, v2_txid, v2_updates = actors["v2"]
            observe(
                "transactions_open",
                {"v1_pid": v1_pid, "v2_pid": v2_pid, "v1_txid": v1_txid, "v2_txid": v2_txid},
            )
            window.release_commits()
            v1_future.result(timeout=5)
            v2_future.result(timeout=5)
        except Exception:
            window.abort()
            for future in (v1_future, v2_future):
                try:
                    future.result(timeout=5)
                except Exception:
                    pass
            raise
    return {"v1_updates": v1_updates, "v2_updates": v2_updates}
