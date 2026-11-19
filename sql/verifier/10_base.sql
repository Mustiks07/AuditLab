-- AuditLab independent verifier: shared views. Portable DuckDB / SQLite SQL.
-- Money is integer minor units. There is no division and no floating point in these files;
-- the only conversion goes through convert_half_up() (engine primitive, see auditlab/verifier/engine.py).
-- Period semantics (SPEC §4): [period_start, period_end) in company time, right side exclusive.

CREATE VIEW period_of_row AS
SELECT l.row_id,
       CASE WHEN l.ts < p.period_start_epoch THEN -1
            WHEN l.ts < p.period_end_epoch THEN 0
            ELSE 1 END AS period
FROM ledger l CROSS JOIN params p;

-- UNAPPLIED is credit-normal (policy/allocation). before_deadline marks the half-open boundary.
CREATE VIEW unapplied AS
SELECT l.row_id, l.ts, l.ts_text, l.debit_minor, l.credit_minor, l.currency,
       l.invoice_id, l.payment_id, l.journal_id, l.src_order,
       CASE WHEN l.ts < p.deadline_epoch THEN 1 ELSE 0 END AS before_deadline
FROM ledger l CROSS JOIN params p
WHERE l.account = 'UNAPPLIED' AND l.payment_id IS NOT NULL;

CREATE VIEW invoice_keys AS
SELECT DISTINCT invoice_id, entity_id, currency, canonical_key FROM invoices;

-- Recognition journals: rows that cite an invoice, no payment, in the invoice currency.
CREATE VIEW recognition_journals AS
SELECT l.invoice_id, l.journal_id, l.currency,
       MIN(l.ts) AS ts, SUM(l.debit_minor) AS debit_total, SUM(l.credit_minor) AS credit_total
FROM ledger l
JOIN invoice_keys k ON k.invoice_id = l.invoice_id AND k.currency = l.currency
WHERE l.payment_id IS NULL
GROUP BY l.invoice_id, l.journal_id, l.currency;
