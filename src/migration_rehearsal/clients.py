"""The two deliberately small application write adapters used during compatibility."""

from __future__ import annotations

import psycopg


def write_v1(connection: psycopg.Connection[tuple[object, ...]]) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE invoices SET amount_cents = amount_cents + 101 WHERE id BETWEEN 4001 AND 4008"
        )
        return cursor.rowcount


def write_v2(connection: psycopg.Connection[tuple[object, ...]]) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE invoices SET amount_minor = amount_minor + 202, currency_code = 'EUR' "
            "WHERE id BETWEEN 4017 AND 4024"
        )
        return cursor.rowcount
