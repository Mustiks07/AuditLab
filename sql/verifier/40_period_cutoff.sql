-- period_cutoff (SPEC §4, §6): the recognising document decides the period (policy/period):
-- invoice issue date for invoices, transaction date for cash. Only events that cross the
-- boundary of the target period [period_start, period_end) are reported.

CREATE VIEW invoice_events AS
SELECT invoice_id, MIN(currency) AS currency, MIN(total_minor) AS total_minor,
       MIN(issued_ts) AS issued_ts
FROM invoices
GROUP BY invoice_id
HAVING COUNT(DISTINCT issued_ts) = 1 AND COUNT(DISTINCT total_minor) = 1;

CREATE VIEW first_recognition AS
SELECT invoice_id, journal_id, ts FROM (
  SELECT r.invoice_id, r.journal_id, r.ts,
         ROW_NUMBER() OVER (PARTITION BY r.invoice_id ORDER BY r.ts, r.journal_id) AS n
  FROM recognition_journals r) ranked
WHERE n = 1;

CREATE VIEW cash_events AS
SELECT pay.payment_id, pay.currency, pay.amount_minor, pay.ts AS event_ts,
       (SELECT MIN(l.ts) FROM ledger l WHERE l.payment_id = pay.payment_id
          AND l.account = 'CASH' AND l.currency = pay.currency) AS posted_ts
FROM payments pay;

CREATE VIEW cutoff_candidates AS
SELECT 'invoice' AS kind, e.invoice_id AS entity_key, e.currency, e.total_minor AS amount_minor,
       e.issued_ts AS event_ts, r.ts AS posted_ts, r.journal_id
FROM invoice_events e JOIN first_recognition r ON r.invoice_id = e.invoice_id
UNION ALL
SELECT 'payment', c.payment_id, c.currency, c.amount_minor, c.event_ts, c.posted_ts, NULL
FROM cash_events c WHERE c.posted_ts IS NOT NULL;

CREATE VIEW cutoff_periods AS
SELECT c.*,
       CASE WHEN c.event_ts < p.period_start_epoch THEN -1
            WHEN c.event_ts < p.period_end_epoch THEN 0 ELSE 1 END AS event_period,
       CASE WHEN c.posted_ts < p.period_start_epoch THEN -1
            WHEN c.posted_ts < p.period_end_epoch THEN 0 ELSE 1 END AS posted_period
FROM cutoff_candidates c CROSS JOIN params p;

CREATE VIEW f_period_cutoff AS
SELECT 'period_cutoff|' || c.kind || '|' || c.entity_key AS finding_key,
       'period_cutoff' AS type, c.currency, c.amount_minor, 'policy/period' AS rule,
       c.kind || ' ' || c.entity_key || ': event in period ' || CAST(c.event_period AS TEXT)
         || ', posted in period ' || CAST(c.posted_period AS TEXT)
         || ' (0 = target period ending ' || p.period_end_text || ' exclusive); nominal '
         || CAST(c.amount_minor AS TEXT) || ' ' || c.currency || ' minor units' AS arithmetic,
       c.kind, c.entity_key, c.journal_id
FROM cutoff_periods c CROSS JOIN params p
WHERE c.event_period <> c.posted_period AND (c.event_period = 0 OR c.posted_period = 0);

CREATE VIEW a_period_cutoff AS
SELECT f.finding_key, f.kind || '/' || f.entity_key AS affected_id FROM f_period_cutoff f;

CREATE VIEW e_period_cutoff AS
SELECT f.finding_key, 'invoices/' || i.doc_id AS evidence_id, 0 AS prio, 0 AS ts
FROM f_period_cutoff f JOIN invoices i ON f.kind = 'invoice' AND i.invoice_id = f.entity_key
UNION
SELECT f.finding_key, 'payments/' || pay.doc_id, 0, 0
FROM f_period_cutoff f JOIN payments pay ON f.kind = 'payment' AND pay.payment_id = f.entity_key
UNION
SELECT f.finding_key, 'policy/period', 1, 0 FROM f_period_cutoff f
UNION
SELECT f.finding_key, 'ledger/' || l.row_id, 2, l.ts
FROM f_period_cutoff f JOIN ledger l ON f.kind = 'invoice' AND l.journal_id = f.journal_id
WHERE l.debit_minor > 0
UNION
SELECT f.finding_key, 'ledger/' || l.row_id, 2, l.ts
FROM f_period_cutoff f JOIN ledger l ON f.kind = 'payment' AND l.payment_id = f.entity_key
WHERE l.account = 'CASH';
