CREATE OR REPLACE FUNCTION invoices_compatibility_write() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    legacy_changed BOOLEAN;
    target_changed BOOLEAN;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.amount_cents IS NOT NULL AND NEW.amount_minor IS NULL AND NEW.currency_code IS NULL THEN
            IF NEW.amount_cents < 0 THEN RAISE EXCEPTION 'invalid invoice pair' USING ERRCODE = 'check_violation'; END IF;
            NEW.amount_minor := NEW.amount_cents;
            NEW.currency_code := 'EUR';
        ELSIF NEW.amount_cents IS NULL AND NEW.amount_minor IS NOT NULL
              AND NEW.currency_code = 'EUR' AND NEW.amount_minor >= 0 THEN
            NEW.amount_cents := NEW.amount_minor;
        ELSIF NEW.amount_cents IS NOT NULL AND NEW.amount_minor IS NOT NULL
              AND NEW.amount_cents = NEW.amount_minor AND NEW.currency_code = 'EUR'
              AND NEW.amount_cents >= 0 THEN
            NULL;
        ELSE
            RAISE EXCEPTION 'invalid invoice pair' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    legacy_changed := NEW.amount_cents IS DISTINCT FROM OLD.amount_cents;
    target_changed := NEW.amount_minor IS DISTINCT FROM OLD.amount_minor
        OR NEW.currency_code IS DISTINCT FROM OLD.currency_code;
    IF legacy_changed AND NOT target_changed THEN
        IF NEW.amount_cents IS NULL OR NEW.amount_cents < 0 THEN
            RAISE EXCEPTION 'invalid invoice pair' USING ERRCODE = 'check_violation';
        END IF;
        NEW.amount_minor := NEW.amount_cents;
        NEW.currency_code := 'EUR';
    ELSIF target_changed AND NOT legacy_changed THEN
        IF NEW.amount_minor IS NULL OR NEW.amount_minor < 0
           OR NEW.currency_code IS DISTINCT FROM 'EUR' THEN
            RAISE EXCEPTION 'invalid invoice pair' USING ERRCODE = 'check_violation';
        END IF;
        NEW.amount_cents := NEW.amount_minor;
    ELSIF legacy_changed AND target_changed THEN
        IF NEW.amount_cents IS NULL OR NEW.amount_minor IS NULL OR NEW.amount_cents < 0
           OR NEW.amount_minor < 0 OR NEW.amount_cents <> NEW.amount_minor
           OR NEW.currency_code IS DISTINCT FROM 'EUR' THEN
            RAISE EXCEPTION 'invalid invoice pair' USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.amount_cents IS NULL OR NEW.amount_minor IS NULL OR NEW.amount_cents < 0
          OR NEW.amount_minor < 0 OR NEW.currency_code IS DISTINCT FROM 'EUR' THEN
        RAISE EXCEPTION 'invalid invoice pair' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER invoices_compatibility_write
BEFORE INSERT OR UPDATE ON invoices
FOR EACH ROW EXECUTE FUNCTION invoices_compatibility_write();

ALTER FUNCTION invoices_compatibility_write() OWNER TO pmr_owner;
REVOKE EXECUTE ON FUNCTION invoices_compatibility_write() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION invoices_compatibility_write() TO pmr_app_v1, pmr_app_v2;

UPDATE migration_control SET phase = 'compatibility' WHERE singleton;
