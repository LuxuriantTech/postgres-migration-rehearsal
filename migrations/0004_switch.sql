ALTER TABLE invoices VALIDATE CONSTRAINT invoices_amount_minor_nonnegative;
ALTER TABLE invoices VALIDATE CONSTRAINT invoices_currency_code_eur;
ALTER TABLE invoices ALTER COLUMN amount_minor SET NOT NULL;
ALTER TABLE invoices ALTER COLUMN currency_code SET NOT NULL;
UPDATE migration_control SET phase = 'switch' WHERE singleton;
REVOKE SELECT (id, amount_cents), UPDATE (amount_cents) ON TABLE invoices FROM pmr_app_v1;
