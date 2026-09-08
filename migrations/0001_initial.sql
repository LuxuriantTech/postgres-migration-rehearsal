CREATE TYPE migration_phase AS ENUM ('initial', 'expand', 'compatibility', 'backfill', 'switch', 'contract');

CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY,
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE invoices (
    id BIGINT PRIMARY KEY,
    amount_cents BIGINT NOT NULL CHECK (amount_cents >= 0)
);

CREATE TABLE migration_control (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    phase migration_phase NOT NULL,
    backfill_last_id BIGINT NOT NULL DEFAULT 0 CHECK (backfill_last_id >= 0),
    backfill_scanned_rows BIGINT NOT NULL DEFAULT 0 CHECK (backfill_scanned_rows >= 0),
    v2_enabled BOOLEAN NOT NULL DEFAULT false,
    protocol_version TEXT NOT NULL DEFAULT 'PMR-CONTRACT-1'
);

CREATE OR REPLACE FUNCTION enforce_migration_phase_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.phase = OLD.phase THEN
        RETURN NEW;
    END IF;
    IF (OLD.phase, NEW.phase) IN (
        ('initial', 'expand'), ('expand', 'compatibility'), ('compatibility', 'backfill'),
        ('backfill', 'switch'), ('switch', 'contract')
    ) THEN
        RETURN NEW;
    END IF;
    IF OLD.phase IN ('expand', 'compatibility', 'backfill', 'switch') AND NEW.phase = 'initial'
       AND EXISTS (
           SELECT 1
           FROM pg_catalog.pg_locks
           WHERE pid = pg_catalog.pg_backend_pid()
             AND locktype = 'advisory'
             AND mode = 'ExclusiveLock'
             AND granted
             AND objsubid = 1
             AND ((classid::bigint << 32) | objid::bigint) = 7241830001
       ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid migration phase transition' USING ERRCODE = 'check_violation';
END;
$$;

CREATE TRIGGER migration_control_phase_transition
BEFORE UPDATE OF phase ON migration_control
FOR EACH ROW EXECUTE FUNCTION enforce_migration_phase_transition();

INSERT INTO migration_control (singleton, phase) VALUES (true, 'initial');

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO pmr_owner, pmr_migrator, pmr_app_v1, pmr_app_v2, pmr_observer;

ALTER TYPE migration_phase OWNER TO pmr_owner;
ALTER TABLE schema_migrations OWNER TO pmr_owner;
ALTER TABLE invoices OWNER TO pmr_owner;
ALTER TABLE migration_control OWNER TO pmr_owner;
ALTER FUNCTION enforce_migration_phase_transition() OWNER TO pmr_owner;

REVOKE ALL ON TABLE schema_migrations, invoices, migration_control FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION enforce_migration_phase_transition() FROM PUBLIC;

GRANT SELECT (id, amount_cents), UPDATE (amount_cents) ON TABLE invoices TO pmr_app_v1;
GRANT SELECT (singleton, phase, v2_enabled) ON TABLE migration_control TO pmr_app_v1, pmr_app_v2;
GRANT SELECT ON TABLE schema_migrations, invoices, migration_control TO pmr_observer;
GRANT USAGE ON TYPE migration_phase TO pmr_app_v1, pmr_app_v2, pmr_observer;
