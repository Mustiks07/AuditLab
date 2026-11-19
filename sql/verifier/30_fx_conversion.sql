-- fx_conversion (SPEC §4, §6): valuation of a foreign-currency source event in the base currency.
-- expected = ROUND_HALF_UP(src_minor / 10^src_exp * rate * 10^dst_exp), rate of the local date of
-- the source event (policy/fx_date). recorded = debits of base-currency rows citing the event.

CREATE VIEW fx_source_docs AS
SELECT 'payment' AS kind, pay.payment_id AS entity_key, 'payments/' || pay.doc_id AS doc_ref,
       pay.currency AS src_currency, pay.amount_minor AS src_minor, pay.local_date AS rate_date
FROM payments pay CROSS JOIN params p WHERE pay.currency <> p.base_currency
UNION ALL
SELECT 'invoice', i.invoice_id, 'invoices/' || i.doc_id, i.currency, i.total_minor, i.local_date
FROM invoices i CROSS JOIN params p WHERE i.currency <> p.base_currency;

-- One economic source per entity; conflicting copies are reported as ambiguity and skipped.
CREATE VIEW fx_sources AS
SELECT kind, entity_key, MIN(src_currency) AS src_currency, MIN(src_minor) AS src_minor,
       MIN(rate_date) AS rate_date
FROM fx_source_docs
GROUP BY kind, entity_key
HAVING COUNT(DISTINCT src_currency) = 1 AND COUNT(DISTINCT src_minor) = 1
   AND COUNT(DISTINCT rate_date) = 1;

CREATE VIEW valuation_rows AS
SELECT 'payment' AS kind, l.payment_id AS entity_key, l.row_id, l.ts, l.debit_minor, l.credit_minor
FROM ledger l CROSS JOIN params p
WHERE l.currency = p.base_currency AND l.payment_id IS NOT NULL
UNION ALL
SELECT 'invoice', l.invoice_id, l.row_id, l.ts, l.debit_minor, l.credit_minor
FROM ledger l CROSS JOIN params p
WHERE l.currency = p.base_currency AND l.payment_id IS NULL AND l.invoice_id IS NOT NULL;

CREATE VIEW fx_valuation AS
SELECT s.kind, s.entity_key, s.src_currency, s.src_minor, s.rate_date, p.base_currency,
       x.rate, se.exp AS src_exp, de.exp AS dst_exp,
       convert_half_up(s.src_minor, x.rate, se.exp, de.exp) AS expected_minor,
       (SELECT SUM(v.debit_minor) FROM valuation_rows v
         WHERE v.kind = s.kind AND v.entity_key = s.entity_key) AS recorded_minor
FROM fx_sources s
CROSS JOIN params p
JOIN fx x ON x.rate_date = s.rate_date AND x.source_currency = s.src_currency
         AND x.target_currency = p.base_currency
JOIN currency_exp se ON se.currency = s.src_currency
JOIN currency_exp de ON de.currency = p.base_currency
WHERE (SELECT COUNT(*) FROM fx x2 WHERE x2.rate_date = s.rate_date
         AND x2.source_currency = s.src_currency AND x2.target_currency = p.base_currency) = 1;

CREATE VIEW f_fx_conversion AS
SELECT 'fx_conversion|' || v.kind || '|' || v.entity_key AS finding_key,
       'fx_conversion' AS type, v.base_currency AS currency,
       abs(v.recorded_minor - v.expected_minor) AS amount_minor, 'policy/fx_date' AS rule,
       'ROUND_HALF_UP(' || CAST(v.src_minor AS TEXT) || ' ' || v.src_currency || ' minor / 10^'
         || CAST(v.src_exp AS TEXT) || ' * ' || v.rate || ' * 10^' || CAST(v.dst_exp AS TEXT)
         || ') = ' || CAST(v.expected_minor AS TEXT) || '; recorded ' || CAST(v.recorded_minor AS TEXT)
         || '; |difference| = ' || CAST(abs(v.recorded_minor - v.expected_minor) AS TEXT) || ' '
         || v.base_currency || ' minor units; rate date ' || v.rate_date AS arithmetic,
       v.kind, v.entity_key, v.rate_date, v.src_currency
FROM fx_valuation v
WHERE v.recorded_minor IS NOT NULL AND v.recorded_minor <> v.expected_minor;

CREATE VIEW a_fx_conversion AS
SELECT f.finding_key, f.kind || '/' || f.entity_key AS affected_id FROM f_fx_conversion f;

CREATE VIEW e_fx_conversion AS
SELECT f.finding_key, d.doc_ref AS evidence_id, 0 AS prio, 0 AS ts
FROM f_fx_conversion f JOIN fx_source_docs d ON d.kind = f.kind AND d.entity_key = f.entity_key
UNION
SELECT f.finding_key, 'fx/' || f.rate_date || '/' || f.src_currency || '/' || f.currency, 0, 1
FROM f_fx_conversion f
UNION
SELECT f.finding_key, 'policy/fx_date', 1, 0 FROM f_fx_conversion f
UNION
SELECT f.finding_key, 'ledger/' || v.row_id, 2, v.ts
FROM f_fx_conversion f JOIN valuation_rows v ON v.kind = f.kind AND v.entity_key = f.entity_key
WHERE v.debit_minor > 0;
