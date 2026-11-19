-- Integrity checks and policy ambiguities. These never become findings; they are reported
-- separately so that a disagreement with truth can be classified (SPEC §10).

CREATE VIEW notes AS
SELECT 'integrity' AS kind, 'journal/' || journal_id || '/' || currency AS subject,
       'debits ' || CAST(SUM(debit_minor) AS TEXT) || ' <> credits ' || CAST(SUM(credit_minor) AS TEXT) AS detail
FROM ledger GROUP BY journal_id, currency HAVING SUM(debit_minor) <> SUM(credit_minor)
UNION ALL
SELECT 'integrity', 'payments/' || pay.doc_id || '/settlement_index',
       'index lists ' || 'ledger/' || s.row_id || ' which is not an UNAPPLIED row of the payment before the index boundary'
FROM settlement_index s JOIN payments pay ON pay.payment_id = s.payment_id
WHERE s.row_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM unapplied u WHERE u.row_id = s.row_id AND u.payment_id = s.payment_id AND u.ts < s.before_ts)
UNION ALL
SELECT 'integrity', 'payments/' || pay.doc_id || '/settlement_index',
       'index omits ledger/' || u.row_id || ' (UNAPPLIED row of the payment before the boundary)'
FROM settlement_index s JOIN payments pay ON pay.payment_id = s.payment_id
JOIN unapplied u ON u.payment_id = s.payment_id AND u.ts < s.before_ts
WHERE s.row_id IS NULL AND NOT EXISTS (
  SELECT 1 FROM settlement_index s2 WHERE s2.payment_id = s.payment_id AND s2.row_id = u.row_id)
UNION ALL
SELECT 'integrity', 'payments/' || pay.doc_id || '/settlement_index',
       'index boundary differs from the allocation deadline ' || p.deadline_text
FROM settlement_index s JOIN payments pay ON pay.payment_id = s.payment_id CROSS JOIN params p
WHERE s.row_id IS NULL AND s.before_ts <> p.deadline_epoch
UNION ALL
SELECT 'integrity', 'invoices/' || i.doc_id,
       'line items sum to ' || CAST(SUM(li.amount_minor) AS TEXT) || ', total_minor is ' || CAST(i.total_minor AS TEXT)
FROM invoices i JOIN invoice_lines li ON li.doc_id = i.doc_id
GROUP BY i.doc_id, i.total_minor HAVING SUM(li.amount_minor) <> i.total_minor
UNION ALL
SELECT 'integrity', 'payments/' || pay.doc_id, 'invoice_refs cites unknown invoice ' || r.invoice_id
FROM payment_refs r JOIN payments pay ON pay.payment_id = r.payment_id
WHERE NOT EXISTS (SELECT 1 FROM invoices i WHERE i.invoice_id = r.invoice_id)
UNION ALL
SELECT 'integrity', 'payment/' || b.payment_id,
       'UNAPPLIED balance is negative (' || CAST(b.credits - b.debits AS TEXT) || '): payment over-applied'
FROM payment_balance b WHERE b.credits - b.debits < 0
UNION ALL
SELECT 'integrity', 'payment/' || u.payment_id, 'UNAPPLIED row ledger/' || u.row_id || ' is in ' || u.currency
  || ', payment is in ' || pay.currency
FROM unapplied u JOIN payments pay ON pay.payment_id = u.payment_id WHERE u.currency <> pay.currency
UNION ALL
SELECT 'ambiguity', 'payment/' || b.payment_id,
       'positive UNAPPLIED balance ' || CAST(b.credits - b.debits AS TEXT) || ' with '
       || CAST(b.ref_count AS TEXT) || ' invoice_refs and no allocation_instructions: target invoice not unique'
FROM payment_balance b WHERE b.credits - b.debits > 0 AND b.ref_count > 1 AND b.alloc_count = 0
UNION ALL
SELECT 'ambiguity', 'payment/' || s.payment_id,
       'sum of per-invoice shortfalls ' || CAST(SUM(s.instructed - s.applied) AS TEXT)
       || ' differs from UNAPPLIED balance ' || CAST(MIN(b.credits - b.debits) AS TEXT)
FROM instructed_shortfall s JOIN payment_balance b ON b.payment_id = s.payment_id
GROUP BY s.payment_id HAVING SUM(s.instructed - s.applied) <> MIN(b.credits - b.debits)
UNION ALL
SELECT 'ambiguity', d.kind || '/' || d.entity_key, 'source documents disagree on currency, amount or rate date'
FROM fx_source_docs d GROUP BY d.kind, d.entity_key
HAVING COUNT(DISTINCT d.src_currency) > 1 OR COUNT(DISTINCT d.src_minor) > 1 OR COUNT(DISTINCT d.rate_date) > 1
UNION ALL
SELECT 'ambiguity', s.kind || '/' || s.entity_key,
       CAST((SELECT COUNT(*) FROM fx x WHERE x.rate_date = s.rate_date AND x.source_currency = s.src_currency
             AND x.target_currency = p.base_currency) AS TEXT)
       || ' rates for ' || s.rate_date || ' ' || s.src_currency || '/' || p.base_currency || ' (need exactly 1)'
FROM fx_sources s CROSS JOIN params p
WHERE (SELECT COUNT(*) FROM fx x WHERE x.rate_date = s.rate_date AND x.source_currency = s.src_currency
       AND x.target_currency = p.base_currency) <> 1
   AND EXISTS (SELECT 1 FROM valuation_rows v WHERE v.kind = s.kind AND v.entity_key = s.entity_key)
UNION ALL
SELECT 'ambiguity', s.kind || '/' || s.entity_key, 'currency exponent missing in manifest'
FROM fx_sources s CROSS JOIN params p
WHERE NOT EXISTS (SELECT 1 FROM currency_exp e WHERE e.currency = s.src_currency)
   OR NOT EXISTS (SELECT 1 FROM currency_exp e WHERE e.currency = p.base_currency)
UNION ALL
SELECT 'ambiguity', 'invoice/' || r.invoice_id,
       CAST(COUNT(*) AS TEXT) || ' recognition journals for a single invoice document (double posting without a duplicate document)'
FROM recognition_journals r
WHERE NOT EXISTS (SELECT 1 FROM dup_groups g JOIN invoices i ON i.entity_id = g.entity_id
                  AND i.canonical_key = g.canonical_key WHERE i.invoice_id = r.invoice_id)
GROUP BY r.invoice_id HAVING COUNT(*) > 1
UNION ALL
SELECT 'ambiguity', 'invoice/' || i.canonical_key, 'documents sharing a canonical key disagree on total or currency'
FROM invoices i GROUP BY i.entity_id, i.canonical_key
HAVING COUNT(*) > 1 AND (COUNT(DISTINCT i.total_minor) > 1 OR COUNT(DISTINCT i.currency) > 1)
UNION ALL
SELECT 'ambiguity', 'invoice/' || d.canonical_key,
       'first recognitions of duplicate documents share one timestamp; original invoice is not determined by time'
FROM dup_recognitions d
WHERE d.ts = (SELECT MIN(d2.ts) FROM dup_recognitions d2 WHERE d2.entity_id = d.entity_id AND d2.canonical_key = d.canonical_key)
GROUP BY d.entity_id, d.canonical_key HAVING COUNT(DISTINCT d.invoice_id) > 1
UNION ALL
SELECT 'ambiguity', 'invoice/' || r.invoice_id,
       'recognition journals of one invoice fall into different periods'
FROM recognition_journals r CROSS JOIN params p
GROUP BY r.invoice_id
HAVING COUNT(DISTINCT CASE WHEN r.ts < p.period_start_epoch THEN -1 WHEN r.ts < p.period_end_epoch THEN 0 ELSE 1 END) > 1;
