DROP TRIGGER invoices_compatibility_write ON invoices;
DROP FUNCTION invoices_compatibility_write();
ALTER TABLE invoices DROP CONSTRAINT invoices_amount_minor_nonnegative;
ALTER TABLE invoices DROP CONSTRAINT invoices_currency_code_eur;
ALTER TABLE invoices ADD CONSTRAINT invoices_amount_minor_final CHECK (amount_minor >= 0);
ALTER TABLE invoices ADD CONSTRAINT invoices_currency_code_final CHECK (currency_code IN ('EUR', 'USD'));
ALTER TABLE invoices DROP COLUMN amount_cents;
UPDATE migration_control SET phase = 'contract' WHERE singleton;
