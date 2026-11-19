-- payment_allocation (SPEC §6): a designated payment must be fully applied before the deadline.
-- Balance of UNAPPLIED for the payment = credits - debits over rows strictly before the deadline.

CREATE VIEW payment_balance AS
SELECT pay.payment_id, pay.doc_id, pay.currency, pay.amount_minor,
       (SELECT COALESCE(SUM(u.credit_minor), 0) FROM unapplied u
         WHERE u.payment_id = pay.payment_id AND u.before_deadline = 1
           AND u.currency = pay.currency) AS credits,
       (SELECT COALESCE(SUM(u.debit_minor), 0) FROM unapplied u
         WHERE u.payment_id = pay.payment_id AND u.before_deadline = 1
           AND u.currency = pay.currency) AS debits,
       (SELECT COUNT(*) FROM payment_refs r WHERE r.payment_id = pay.payment_id) AS ref_count,
       (SELECT COUNT(*) FROM payment_alloc a WHERE a.payment_id = pay.payment_id) AS alloc_count
FROM payments pay;

-- Per-invoice application from UNAPPLIED debits that cite the invoice (instructions case only).
CREATE VIEW instructed_shortfall AS
SELECT a.payment_id, a.invoice_id, a.amount_minor AS instructed,
       (SELECT COALESCE(SUM(u.debit_minor), 0) FROM unapplied u
         WHERE u.payment_id = a.payment_id AND u.invoice_id = a.invoice_id
           AND u.before_deadline = 1) AS applied
FROM payment_alloc a;

CREATE VIEW f_payment_allocation AS
SELECT 'payment_allocation|' || b.payment_id AS finding_key,
       'payment_allocation' AS type, b.currency AS currency,
       b.credits - b.debits AS amount_minor, 'policy/allocation' AS rule,
       'UNAPPLIED ' || b.payment_id || ': credits ' || CAST(b.credits AS TEXT) || ' - debits '
         || CAST(b.debits AS TEXT) || ' = ' || CAST(b.credits - b.debits AS TEXT) || ' '
         || b.currency || ' minor units on rows before ' || p.deadline_text
         || ' (exclusive); designated amount ' || CAST(b.amount_minor AS TEXT) AS arithmetic,
       b.payment_id AS payment_id, CAST(NULL AS TEXT) AS invoice_id
FROM payment_balance b CROSS JOIN params p
WHERE b.credits - b.debits > 0 AND b.ref_count > 0 AND b.alloc_count = 0
UNION ALL
SELECT 'payment_allocation|' || s.payment_id || '|' || s.invoice_id,
       'payment_allocation', b.currency, s.instructed - s.applied, 'policy/allocation',
       'instructed ' || CAST(s.instructed AS TEXT) || ' - applied ' || CAST(s.applied AS TEXT)
         || ' = ' || CAST(s.instructed - s.applied AS TEXT) || ' ' || b.currency
         || ' minor units for ' || s.invoice_id || ' before ' || p.deadline_text || ' (exclusive)',
       s.payment_id, s.invoice_id
FROM instructed_shortfall s
JOIN payment_balance b ON b.payment_id = s.payment_id
CROSS JOIN params p
WHERE s.instructed - s.applied > 0;

CREATE VIEW a_payment_allocation AS
SELECT f.finding_key, 'payment/' || f.payment_id AS affected_id FROM f_payment_allocation f
UNION
SELECT f.finding_key, 'invoice/' || f.invoice_id FROM f_payment_allocation f
WHERE f.invoice_id IS NOT NULL
UNION
SELECT f.finding_key, 'invoice/' || r.invoice_id FROM f_payment_allocation f
JOIN payment_refs r ON r.payment_id = f.payment_id
WHERE f.invoice_id IS NULL AND (SELECT COUNT(*) FROM payment_refs r2 WHERE r2.payment_id = f.payment_id) = 1;

-- Evidence priority: 0 source document, 1 rule, 2 dated ledger rows.
CREATE VIEW e_payment_allocation AS
SELECT f.finding_key, 'payments/' || pay.doc_id AS evidence_id, 0 AS prio, 0 AS ts
FROM f_payment_allocation f JOIN payments pay ON pay.payment_id = f.payment_id
UNION
SELECT f.finding_key, 'policy/allocation', 1, 0 FROM f_payment_allocation f
UNION
SELECT f.finding_key, 'ledger/' || u.row_id, 2, u.ts
FROM f_payment_allocation f JOIN unapplied u ON u.payment_id = f.payment_id
WHERE u.before_deadline = 1 AND (f.invoice_id IS NULL OR u.invoice_id = f.invoice_id OR u.credit_minor > 0);
