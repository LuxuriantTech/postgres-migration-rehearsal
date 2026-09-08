from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from collections.abc import Generator
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.conninfo import make_conninfo


def connection_fields(database: str) -> dict[str, str]:
    fields = {
        "host": os.environ.get("PMR_PGHOST", "127.0.0.1"),
        "port": os.environ.get("PMR_PGPORT", "55432"),
        "dbname": database,
        "user": os.environ.get("PMR_PGUSER", "rehearsal_app"),
        "password": os.environ.get("PMR_PGPASSWORD", "synthetic-local-only"),
    }
    if fields["host"] not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("PMR test database host must be loopback")
    if fields["port"] != "55432":
        raise RuntimeError("PMR test database port must be 55432")
    if fields["user"] != "rehearsal_app":
        raise RuntimeError("PMR test database user must be rehearsal_app")
    return fields


def dsn_for(database: str) -> str:
    return make_conninfo(**connection_fields(database))


def deterministic_database_name(nodeid: str) -> str:
    raw_run_id = os.environ.get("PMR_TEST_RUN_ID", str(os.getpid()))
    if re.fullmatch(r"[A-Za-z0-9]+", raw_run_id) is None:
        raise RuntimeError("PMR_TEST_RUN_ID must be ASCII alphanumeric")
    run_token = f"{os.getpid()}_{raw_run_id[:20]}"
    name = f"pmr_test_{run_token}_{hashlib.sha256(nodeid.encode()).hexdigest()[:20]}"
    if len(name.encode("utf-8")) > 63:
        raise RuntimeError("PMR test database name exceeds PostgreSQL limit")
    return name


@dataclass(frozen=True)
class TestDatabase:
    name: str
    dsn: str
    oid: int
    prior_oid: int | None


def exact_test_database_name(name: str) -> None:
    if re.fullmatch(r"pmr_test_[A-Za-z0-9_]+", name) is None:
        raise RuntimeError("refusing a database outside the PMR test prefix")
    if len(name.encode("utf-8")) > 63:
        raise RuntimeError("refusing an overlong PostgreSQL database name")


@dataclass
class DatabaseFactory:
    admin_dsn: str
    nodeid: str
    created: list[TestDatabase]

    def create(self, label: str) -> TestDatabase:
        name = deterministic_database_name(f"{self.nodeid}:{label}")
        exact_test_database_name(name)
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            self._assert_safe_admin_connection(admin)
            with admin.cursor() as cursor:
                cursor.execute("SELECT oid FROM pg_database WHERE datname = %s", (name,))
                prior = cursor.fetchone()
            self._terminate_and_drop(admin, name)
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {} OWNER pmr_owner TEMPLATE template0").format(
                        sql.Identifier(name)
                    )
                )
                cursor.execute(
                    "SELECT oid, pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                    (name,),
                )
                row = cursor.fetchone()
        assert row is not None
        oid, owner = row
        assert owner == "pmr_owner"
        database = TestDatabase(
            name=name,
            dsn=dsn_for(name),
            oid=int(oid),
            prior_oid=int(prior[0]) if prior is not None else None,
        )
        assert database.prior_oid is None or database.oid != database.prior_oid
        self.created.append(database)
        return database

    def recreate(self, database: TestDatabase) -> TestDatabase:
        exact_test_database_name(database.name)
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            self._assert_safe_admin_connection(admin)
            self._terminate_and_drop(admin, database.name)
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {} OWNER pmr_owner TEMPLATE template0").format(
                        sql.Identifier(database.name)
                    )
                )
                cursor.execute(
                    "SELECT oid, pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                    (database.name,),
                )
                row = cursor.fetchone()
        assert row is not None
        oid, owner = row
        assert owner == "pmr_owner"
        recreated = TestDatabase(
            name=database.name,
            dsn=dsn_for(database.name),
            oid=int(oid),
            prior_oid=database.oid,
        )
        assert recreated.oid != database.oid
        self.created.append(recreated)
        return recreated

    def cleanup(self) -> None:
        names = {database.name for database in self.created}
        if not names:
            return
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            self._assert_safe_admin_connection(admin)
            for name in names:
                self._terminate_and_drop(admin, name)

    @staticmethod
    def _terminate_and_drop(admin: Connection[tuple[str, ...]], name: str) -> None:
        exact_test_database_name(name)
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))

    @staticmethod
    def _assert_safe_admin_connection(admin: Connection[tuple[str, ...]]) -> None:
        if admin.info.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("refusing a non-loopback Psycopg admin target")
        if admin.info.port != 55432:
            raise RuntimeError("refusing an unexpected Psycopg admin target port")
        try:
            hostaddr = ipaddress.ip_address(admin.info.hostaddr)
        except (TypeError, ValueError) as error:
            raise RuntimeError("refusing an invalid Psycopg admin target address") from error
        if not hostaddr.is_loopback:
            raise RuntimeError("refusing a non-loopback Psycopg admin target address")
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_user, current_setting('server_version_num')"
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("cannot verify PMR test database admin connection")
        database, user, version = row
        if database != "postgres":
            raise RuntimeError("refusing database lifecycle outside postgres admin database")
        if user != "rehearsal_app":
            raise RuntimeError("refusing database lifecycle outside rehearsal_app")
        if version != "180006":
            raise RuntimeError("refusing unexpected PostgreSQL server version")
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT rolname, rolsuper::text, rolcanlogin::text, rolcreatedb::text, "
                "rolcreaterole::text, rolreplication::text, rolbypassrls::text, rolinherit::text "
                "FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
                (["pmr_app_v1", "pmr_app_v2", "pmr_migrator", "pmr_observer", "pmr_owner"],),
            )
            roles = cursor.fetchall()
            cursor.execute(
                "SELECT parent.rolname, member.rolname "
                "FROM pg_auth_members membership "
                "JOIN pg_roles parent ON parent.oid = membership.roleid "
                "JOIN pg_roles member ON member.oid = membership.member "
                "WHERE parent.rolname = ANY(%s) AND member.rolname = ANY(%s) "
                "ORDER BY parent.rolname, member.rolname",
                (
                    ["pmr_app_v1", "pmr_app_v2", "pmr_migrator", "pmr_observer", "pmr_owner"],
                    ["pmr_migrator", "rehearsal_app"],
                ),
            )
            memberships = cursor.fetchall()
        expected_roles = [
            ("pmr_app_v1", "false", "false", "false", "false", "false", "false", "false"),
            ("pmr_app_v2", "false", "false", "false", "false", "false", "false", "false"),
            ("pmr_migrator", "false", "false", "false", "false", "false", "false", "true"),
            ("pmr_observer", "false", "false", "false", "false", "false", "false", "false"),
            ("pmr_owner", "false", "false", "false", "false", "false", "false", "false"),
        ]
        if roles != expected_roles:
            raise RuntimeError("refusing unexpected PMR role attributes")
        expected_memberships = [
            ("pmr_app_v1", "rehearsal_app"),
            ("pmr_app_v2", "rehearsal_app"),
            ("pmr_migrator", "rehearsal_app"),
            ("pmr_observer", "rehearsal_app"),
            ("pmr_owner", "pmr_migrator"),
            ("pmr_owner", "rehearsal_app"),
        ]
        if memberships != expected_memberships:
            raise RuntimeError("refusing unexpected PMR role memberships")


@pytest.fixture
def admin_dsn() -> str:
    return dsn_for("postgres")


@pytest.fixture
def database_factory(
    admin_dsn: str, request: pytest.FixtureRequest
) -> Generator[DatabaseFactory, None, None]:
    factory = DatabaseFactory(admin_dsn=admin_dsn, nodeid=request.node.nodeid, created=[])
    try:
        yield factory
    finally:
        factory.cleanup()


@pytest.fixture
def test_database(database_factory: DatabaseFactory) -> TestDatabase:
    return database_factory.create("primary")


@pytest.fixture
def postgres_dsn(test_database: TestDatabase) -> str:
    return test_database.dsn


@pytest.fixture
def postgres_connection(
    test_database: TestDatabase,
) -> Generator[Connection[tuple[str, str]], None, None]:
    with psycopg.connect(test_database.dsn) as connection:
        yield connection
