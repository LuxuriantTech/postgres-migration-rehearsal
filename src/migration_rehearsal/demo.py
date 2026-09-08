"""Bounded, local-only AC13 rehearsal report."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import psycopg

_BACKFILL_STAGE_ORDER = (
    "batch3_rolled_back",
    "v1_committed",
    "backfill_complete",
    "second_pass_complete",
    "down_waiting",
    "new_batch_rejected",
)
_CAUSAL_ORDER = ("shared", "singleton", "invoice", "checkpoint")


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def render_demo_and_report(dsn: str, *, output_path: Path) -> Mapping[str, object]:
    """Run the local AC13 slice and write a compact, redacted report."""
    from migration_rehearsal import contract

    contract.verify_environment(dsn)
    observed_backfill_stages: list[str] = []

    def observe_backfill(stage: str, _actors: Mapping[str, int]) -> None:
        observed_backfill_stages.append(stage)

    backfill = contract.exercise_backfill_resume_and_down(dsn, observe=observe_backfill)
    if tuple(observed_backfill_stages) != _BACKFILL_STAGE_ORDER:
        raise RuntimeError("AC13 backfill stages were not fully observed")
    if (
        backfill["checkpoint_after_failpoint"] != 512
        or backfill["id_513_preserved"] is not True
        or backfill["second_pass_rows"] != 0
        or backfill["down_waiter_granted"] is not False
        or backfill["deadlock_sqlstate"] is not None
        or backfill["new_batch_error"] != "BACKFILL_PHASE_REJECTED"
        or backfill["causal_order"] != list(_CAUSAL_ORDER)
        or backfill["failpoint_batch_effective_role"] != "pmr_migrator"
        or backfill["down_contention_batch_effective_role"] != "pmr_migrator"
    ):
        raise RuntimeError("AC13 backfill evidence mismatch")

    for version in ("0001", "0002", "0003"):
        contract._apply_migration(dsn, version)

    client_identity_observed = False

    def observe_clients(stage: str, actors: Mapping[str, int]) -> None:
        nonlocal client_identity_observed
        if stage != "transactions_open" or set(actors) != {
            "v1_pid",
            "v2_pid",
            "v1_txid",
            "v2_txid",
        }:
            raise RuntimeError("AC13 client observation mismatch")
        with psycopg.connect(dsn, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pid, backend_xid FROM pg_catalog.pg_stat_activity "
                    "WHERE pid = ANY(%s) ORDER BY pid",
                    ([actors["v1_pid"], actors["v2_pid"]],),
                )
                rows = cursor.fetchall()
        observed = {int(row[0]): str(row[1]) for row in rows if row[1] is not None}
        client_identity_observed = (
            len(observed) == 2
            and actors["v1_pid"] != actors["v2_pid"]
            and actors["v1_txid"] != actors["v2_txid"]
            and observed
            == {
                actors["v1_pid"]: str(actors["v1_txid"]),
                actors["v2_pid"]: str(actors["v2_txid"]),
            }
        )
        if not client_identity_observed:
            raise RuntimeError("AC13 client identities were not distinct")

    clients = contract.run_concurrent_clients(dsn, observe=observe_clients)
    if clients != {"v1_updates": 8, "v2_updates": 8} or not client_identity_observed:
        raise RuntimeError("AC13 client evidence mismatch")
    contract._apply_migration(dsn, "0004")
    contract._apply_migration(dsn, "0005")

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SET LOCAL ROLE pmr_observer")
            cursor.execute("SELECT session_user, current_user")
            observer_identity = cursor.fetchone() == ("rehearsal_app", "pmr_observer")
            cursor.execute("SELECT phase::text FROM migration_control WHERE singleton")
            phase_contract = cursor.fetchone() == ("contract",)
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
            ledger = cursor.fetchall()
            cursor.execute("SELECT count(*) FROM invoices")
            row_count = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM invoices WHERE amount_minor IS NULL OR currency_code IS NULL"
            )
            null_count = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM pg_catalog.pg_attribute "
                "WHERE attrelid = 'invoices'::regclass "
                "AND attname = 'amount_cents' AND NOT attisdropped"
            )
            legacy_count = cursor.fetchone()
    contract_checks = {
        "ledger_0001_to_0005": ledger == [("0001",), ("0002",), ("0003",), ("0004",), ("0005",)],
        "legacy_amount_cents_absent": legacy_count == (0,),
        "no_target_nulls": null_count == (0,),
        "observer_effective_role": observer_identity,
        "phase_contract": phase_contract,
        "rows_4096": row_count == (4096,),
    }
    if not all(contract_checks.values()):
        raise RuntimeError("AC13 contract evidence mismatch")
    report: dict[str, object] = {
        "backfill": {
            "checkpoint_after_failpoint_512": True,
            "causal_order_exact": True,
            "down_advisory_waited": True,
            "id_513_preserved": True,
            "new_batch_rejected": True,
            "no_deadlock_40p01": True,
            "second_pass_zero": True,
            "two_migrator_batch_roles": True,
        },
        "clients": {
            "distinct_pid_and_txid_observed": True,
            "v1_writes_8": True,
            "v2_writes_8": True,
        },
        "contract": contract_checks,
        "environment": {"postgresql_18_6": True},
        "local_only": True,
        "production_claim": False,
        "report_schema_version": 1,
        "synthetic_data_only": True,
    }
    report_bytes = _canonical_json(report)
    if len(report_bytes) > 5 * 1024 * 1024:
        raise RuntimeError("AC13 report exceeds 5 MiB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(report_bytes)
    print("env: PostgreSQL 18.6 local")
    print("backfill: resume/down observed")
    print("lock/down: advisory wait observed")
    print("V1/V2: eight writes each with distinct identities observed")
    print("contract: observer verification complete")
    print(f"report: {output_path}")
    return cast(Mapping[str, object], report)
