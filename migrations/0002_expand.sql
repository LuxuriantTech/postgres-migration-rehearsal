ALTER TABLE invoices ADD COLUMN amount_minor BIGINT;
ALTER TABLE invoices ADD COLUMN currency_code TEXT;
ALTER TABLE invoices ADD CONSTRAINT invoices_amount_minor_nonnegative
    CHECK (amount_minor IS NULL OR amount_minor >= 0) NOT VALID;
ALTER TABLE invoices ADD CONSTRAINT invoices_currency_code_eur
    CHECK (currency_code IS NULL OR currency_code = 'EUR') NOT VALID;
UPDATE migration_control SET phase = 'expand' WHERE singleton;
