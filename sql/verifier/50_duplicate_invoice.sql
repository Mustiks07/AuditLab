-- duplicate_invoice (SPEC §6): several documents of one commercial invoice of one counterparty
-- (same entity_id and canonical key; the key defaults to invoice_id) and more than one recognition.
-- Error = recognised total - one invoice total, in the invoice currency.
-- Two archived copies with a single recognition, or two different invoices with equal sums, are clean.

CREATE VIEW dup_groups AS
SELECT entity_id, canonical_key, MIN(currency) AS currency, MIN(total_minor) AS total_minor,
       COUNT(*) AS doc_count
FROM invoices
GROUP BY entity_id, canonical_key
HAVING COUNT(*) > 1 AND COUNT(DISTINCT total_minor) = 1 AND COUNT(DISTINCT currency) = 1;

CREATE VIEW dup_recognitions AS
SELECT g.entity_id, g.canonical_key, r.invoice_id, r.journal_id, r.ts, r.debit_total
FROM dup_groups g
JOIN recognition_journals r ON r.currency = g.currency
 AND r.invoice_id IN (SELECT i.invoice_id FROM invoices i
                       WHERE i.entity_id = g.entity_id AND i.canonical_key = g.canonical_key);

CREATE VIEW dup_totals AS
SELECT g.entity_id, g.canonical_key, g.currency, g.total_minor, g.doc_count,
       COUNT(d.journal_id) AS recognitions, COALESCE(SUM(d.debit_total), 0) AS recognised
FROM dup_groups g
LEFT JOIN dup_recognitions d ON d.entity_id = g.entity_id AND d.canonical_key = g.canonical_key
GROUP BY g.entity_id, g.canonical_key, g.currency, g.total_minor, g.doc_count;

-- The affected invoice is the one recognised first (by time), never the smaller string ID.
CREATE VIEW dup_original AS
SELECT entity_id, canonical_key, invoice_id FROM (
  SELECT d.entity_id, d.canonical_key, d.invoice_id,
         ROW_NUMBER() OVER (PARTITION BY d.entity_id, d.canonical_key
                            ORDER BY d.ts, d.invoice_id, d.journal_id) AS n
  FROM dup_recognitions d) ranked
WHERE n = 1;

CREATE VIEW f_duplicate_invoice AS
SELECT 'duplicate_invoice|' || t.entity_id || '|' || t.canonical_key AS finding_key,
       'duplicate_invoice' AS type, t.currency, t.recognised - t.total_minor AS amount_minor,
       'invoice canonical key: one recognition per commercial invoice' AS rule,
       CAST(t.doc_count AS TEXT) || ' documents of ' || t.canonical_key || ' (' || t.entity_id
         || '), ' || CAST(t.recognitions AS TEXT) || ' recognitions: ' || CAST(t.recognised AS TEXT)
         || ' - ' || CAST(t.total_minor AS TEXT) || ' = ' || CAST(t.recognised - t.total_minor AS TEXT)
         || ' ' || t.currency || ' minor units recognised in excess' AS arithmetic,
       t.entity_id, t.canonical_key
FROM dup_totals t
WHERE t.recognitions > 1 AND t.recognised - t.total_minor > 0;

CREATE VIEW a_duplicate_invoice AS
SELECT f.finding_key, 'invoice/' || o.invoice_id AS affected_id
FROM f_duplicate_invoice f
JOIN dup_original o ON o.entity_id = f.entity_id AND o.canonical_key = f.canonical_key;

CREATE VIEW e_duplicate_invoice AS
SELECT f.finding_key, 'invoices/' || i.doc_id AS evidence_id, 0 AS prio, i.issued_ts AS ts
FROM f_duplicate_invoice f
JOIN invoices i ON i.entity_id = f.entity_id AND i.canonical_key = f.canonical_key
UNION
SELECT f.finding_key, 'ledger/' || l.row_id, 2, l.ts
FROM f_duplicate_invoice f
JOIN dup_recognitions d ON d.entity_id = f.entity_id AND d.canonical_key = f.canonical_key
JOIN ledger l ON l.journal_id = d.journal_id
WHERE l.debit_minor > 0;
