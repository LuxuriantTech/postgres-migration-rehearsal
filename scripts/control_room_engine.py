#!/usr/bin/env python3
"""Child-only disposable-database operation for the control-room adapter.

It never starts Compose, accepts a DSN, reads a browser request, or knows about
the historical one-shot artefacts.  The parent owns process and resource scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from migration_rehearsal import contract
from migration_rehearsal.control_room_adapter import SCENARIO_IDS, _validate_rows, load_scenarios

_DATABASE = "pmr_control_room_local"
_TEST_DATABASE_PASSWORD_PLACEHOLDER = "pmr-test-password-placeholder-not-a-secret"
_DSN = make_conninfo(
    host="127.0.0.1",
    hostaddr="127.0.0.1",
    port=55432,
    dbname="pmr_control_room_local",
    user="rehearsal_app",
    password=_TEST_DATABASE_PASSWORD_PLACEHOLDER,
    connect_timeout=3,
)
_ADMIN_DSN = make_conninfo(
    host="127.0.0.1",
    hostaddr="127.0.0.1",
    port=55432,
    dbname="postgres",
    user="rehearsal_app",
    password=_TEST_DATABASE_PASSWORD_PLACEHOLDER,
    connect_timeout=3,
)


def _as_owner(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute("SET LOCAL ROLE pmr_owner")


def _as_observer(cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
    cursor.execute("SET LOCAL ROLE pmr_observer")


def _reset_fixture(rows: list[dict[str, int]]) -> None:
    with psycopg.connect(_DSN) as connection:
        with connection.cursor() as cursor:
            _as_owner(cursor)
            cursor.execute("DELETE FROM invoices")
            for row in rows:
                cursor.execute(
                    "INSERT INTO invoices (id, amount_cents) VALUES (%s, %s)",
                    (row["invoice_id"], row["amount_cents"]),
                )
        connection.commit()


def _create_database() -> tuple[int, str]:
    with psycopg.connect(_ADMIN_DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            _verify_endpoint(connection, expected_database="postgres")
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (_DATABASE,))
            if cursor.fetchone() is not None:
                raise RuntimeError("fixed disposable database already exists")
            cursor.execute(
                "CREATE DATABASE pmr_control_room_local OWNER pmr_owner TEMPLATE template0"
            )
            cursor.execute(
                "SELECT database.oid, role.rolname FROM pg_database AS database "
                "JOIN pg_roles AS role ON role.oid = database.datdba WHERE database.datname = %s",
                (_DATABASE,),
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("cannot pin disposable database identity")
    if not isinstance(row[0], int) or row[1] != "pmr_owner":
        raise RuntimeError("cannot pin disposable database owner")
    return int(row[0]), "pmr_owner"


def _drop_database(expected_oid: int, expected_owner: str) -> None:
    with psycopg.connect(_ADMIN_DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            _verify_endpoint(connection, expected_database="postgres")
            cursor.execute(
                "SELECT database.oid, role.rolname FROM pg_database AS database "
                "JOIN pg_roles AS role ON role.oid = database.datdba WHERE database.datname = %s",
                (_DATABASE,),
            )
            row = cursor.fetchone()
            if row != (expected_oid, expected_owner):
                raise RuntimeError("disposable database identity drift")
            cursor.execute("ALTER DATABASE pmr_control_room_local WITH ALLOW_CONNECTIONS false")
            cursor.execute(
                "SELECT pg_catalog.pg_terminate_backend(pid) FROM pg_catalog.pg_stat_activity "
                "WHERE datid = %s AND pid <> pg_catalog.pg_backend_pid()",
                (expected_oid,),
            )
            cursor.execute(
                "SELECT database.oid, role.rolname FROM pg_database AS database "
                "JOIN pg_roles AS role ON role.oid = database.datdba WHERE database.datname = %s",
                (_DATABASE,),
            )
            if cursor.fetchone() != (expected_oid, expected_owner):
                raise RuntimeError("disposable database changed before drop")
            cursor.execute("DROP DATABASE pmr_control_room_local")


def _verify_endpoint(
    connection: psycopg.Connection[tuple[object, ...]], *, expected_database: str
) -> None:
    """Prove the fixed loopback session before it is allowed to issue DDL."""
    if connection.info.hostaddr != "127.0.0.1" or connection.info.port != 55432:
        raise RuntimeError("local database client target is not fixed loopback")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), current_user, current_setting('server_version_num'), "
            "inet_server_port()"
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("local database endpoint is unavailable")
    if (
        row[0] != expected_database
        or row[1] != "rehearsal_app"
        or row[2] != "180006"
        or row[3] != 5432
    ):
        raise RuntimeError("local database identity does not match the bounded engine")


def _backfill_cycle() -> tuple[int, int]:
    """Drive individual existing PMR batches so both idempotence observations are real."""
    contract._start_backfill(_DSN)
    first_pass = 0
    while True:
        batch_rows, _ = contract._run_backfill_batch(_DSN)
        first_pass += batch_rows
        if batch_rows == 0:
            break
    contract._finish_backfill_enable_v2(_DSN)
    second_pass, _ = contract._run_backfill_batch(_DSN)
    if second_pass != 0:
        raise RuntimeError("backfill idempotence check failed")
    return first_pass, second_pass


def _rows_at_current_shape(*, final: bool) -> list[dict[str, object]]:
    with psycopg.connect(_DSN) as connection:
        with connection.cursor() as cursor:
            _as_observer(cursor)
            if final:
                cursor.execute("SELECT id, amount_minor, currency_code FROM invoices ORDER BY id")
            else:
                cursor.execute("SELECT id, amount_cents FROM invoices ORDER BY id")
            fetched = cursor.fetchall()
    if final:
        return [
            {"invoice_id": int(row[0]), "amount_minor": int(row[1]), "currency_code": str(row[2])}
            for row in fetched
        ]
    return [{"invoice_id": int(row[0]), "amount_cents": int(row[1])} for row in fetched]


def _observe_final(rows: list[dict[str, int]]) -> dict[str, object]:
    expected = [
        {
            "invoice_id": row["invoice_id"],
            "amount_minor": row["amount_cents"],
            "currency_code": "EUR",
        }
        for row in rows
    ]
    destination = _rows_at_current_shape(final=True)
    with psycopg.connect(_DSN) as connection:
        with connection.cursor() as cursor:
            _as_observer(cursor)
            cursor.execute("SELECT phase::text FROM migration_control WHERE singleton")
            phase = cursor.fetchone()
            cursor.execute("SELECT version, sha256 FROM schema_migrations ORDER BY version")
            ledger = cursor.fetchall()
            cursor.execute(
                "SELECT count(*) FROM invoices WHERE amount_minor IS NULL OR currency_code IS NULL"
            )
            nulls = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'invoices' AND column_name = 'amount_cents'"
            )
            legacy = cursor.fetchone()
            cursor.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'invoices'::regclass "
                "AND conname IN ('invoices_amount_minor_final', 'invoices_currency_code_final')"
            )
            constraints = {str(row[0]) for row in cursor.fetchall()}
    hashes = {
        version: hashlib.sha256(contract._migration_bytes(version)).hexdigest()
        for version in ("0001", "0002", "0003", "0004", "0005")
    }
    expected_ledger = [(version, hashes[version]) for version in hashes]
    final_phase = phase == ("contract",)
    ledger_complete = ledger == expected_ledger
    no_target_nulls = nulls == (0,)
    legacy_column_absent = legacy == (0,)
    if not all((final_phase, ledger_complete, no_target_nulls, legacy_column_absent)):
        raise RuntimeError("final local database integrity check failed")
    if constraints != {"invoices_amount_minor_final", "invoices_currency_code_final"}:
        raise RuntimeError("final local database constraint check failed")
    destination_matches_source = destination == expected
    if not destination_matches_source:
        raise RuntimeError("final destination rows do not match the fixture")
    return {
        "destination": destination,
        "hashes": hashes,
        "row_count": len(destination),
        "final_phase": final_phase,
        "ledger_complete": ledger_complete,
        "no_target_nulls": no_target_nulls,
        "legacy_column_absent": legacy_column_absent,
        "destination_matches_source": destination_matches_source,
    }


def _response(
    scenario_id: str,
    rows: list[dict[str, int]],
    *,
    first_cycle: tuple[int, int],
    second_cycle: tuple[int, int],
    final: dict[str, object],
    rollback_restored: bool,
) -> dict[str, object]:
    hashes = final["hashes"]
    if not isinstance(hashes, dict):
        raise RuntimeError("final migration hash observation is unavailable")
    fixture_bytes = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    destination = final["destination"]
    if not isinstance(destination, list):
        raise RuntimeError("final destination observation is unavailable")
    verdict = "NO_ROWS_TO_MIGRATE" if not rows else "LOCAL_REHEARSAL_PASSED"
    label = "No rows to migrate" if not rows else "Local rehearsal passed"
    return {
        "schema_version": 1,
        "run_id": hashlib.sha256(fixture_bytes + "".join(hashes.values()).encode()).hexdigest(),
        "scenario_id": scenario_id,
        "mode": "bounded_disposable_database_operation",
        "verdict": verdict,
        "verdict_label": label,
        "summary": "The disposable local database completed the bounded migration path.",
        "source_rows": rows,
        "destination_rows": destination,
        "stages": [
            {
                "id": stage,
                "label": label,
                "status": "not_needed" if stage == "backfill" and not rows else "complete",
                "explanation": (
                    "No existing values needed copying."
                    if stage == "backfill" and not rows
                    else "Observed locally."
                ),
                "evidence_ids": [stage],
            }
            for stage, label in (
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
                "observed": rollback_restored,
                "evidence_id": "rollback",
            },
            {
                "order": 2,
                "label": "Roll forward through the contract",
                "observed": final["final_phase"],
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
            "migration_sha256": hashes,
            "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "backfill": {
                "initial_first_pass_rows": first_cycle[0],
                "initial_idempotent_pass_rows": first_cycle[1],
                "cycles": 2,
                "roll_forward_first_pass_rows": second_cycle[0],
                "roll_forward_idempotent_pass_rows": second_cycle[1],
            },
            "rollback": {
                "down_phase": "initial",
                "legacy_rows_restored": rollback_restored,
                "roll_forward_complete": final["final_phase"],
            },
            "integrity": {
                "final_phase": "contract" if final["final_phase"] else "unverified",
                "ledger_complete": final["ledger_complete"],
                "row_count": final["row_count"],
                "destination_matches_source": final["destination_matches_source"],
                "no_target_nulls": final["no_target_nulls"],
                "legacy_column_absent": final["legacy_column_absent"],
            },
            "operation_limit": "No full-volume, concurrent-client, or failpoint exercise.",
        },
        "next_action": "Review the local evidence and reset when ready.",
        "cleanup_state": "complete",
    }


def run(root: Path, scenario_id: str, run_nonce: str) -> dict[str, object]:
    if (
        scenario_id not in SCENARIO_IDS
        or len(run_nonce) != 16
        or any(char not in "0123456789abcdef" for char in run_nonce)
    ):
        raise RuntimeError("invalid closed engine invocation")
    selected = load_scenarios(root, scenario_id=scenario_id)[scenario_id]
    rows = _validate_rows(selected.get("rows"))
    oid, owner = _create_database()
    try:
        contract._apply_migration(_DSN, "0001")
        _reset_fixture([dict(row) for row in rows])
        contract._apply_migration(_DSN, "0002")
        contract._apply_migration(_DSN, "0003")
        first_cycle = _backfill_cycle()
        contract._apply_migration(_DSN, "0004")
        switch_rows = _rows_at_current_shape(final=True)
        expected_switch = [
            {
                "invoice_id": row["invoice_id"],
                "amount_minor": row["amount_cents"],
                "currency_code": "EUR",
            }
            for row in rows
        ]
        if switch_rows != expected_switch:
            raise RuntimeError("switch rows do not match the fixture")
        contract.attempt_down(_DSN)
        rollback_rows = _rows_at_current_shape(final=False)
        rollback_restored = rollback_rows == rows
        if not rollback_restored:
            raise RuntimeError("rollback did not restore the legacy fixture rows")
        contract._apply_migration(_DSN, "0002")
        contract._apply_migration(_DSN, "0003")
        second_cycle = _backfill_cycle()
        contract._apply_migration(_DSN, "0004")
        contract._apply_migration(_DSN, "0005")
        final = _observe_final([dict(row) for row in rows])
        return _response(
            scenario_id,
            [dict(row) for row in rows],
            first_cycle=first_cycle,
            second_cycle=second_cycle,
            final=final,
            rollback_restored=rollback_restored,
        )
    finally:
        _drop_database(oid, owner)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=SCENARIO_IDS, required=True)
    parser.add_argument("--run-nonce", required=True)
    arguments = parser.parse_args()
    try:
        print(json.dumps(run(Path.cwd(), arguments.scenario, arguments.run_nonce), sort_keys=True))
    except Exception as error:
        print(f"control-room engine failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
