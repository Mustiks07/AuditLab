"""Hand-built fixtures for the published reward vectors (1-20) and open examples M01-M12.

Placeholders of the definitions map to AuditLab's documented canonical keys:
  reward fixture (one affected record): T=period_cutoff, R1=invoice/I1, D1=invoices/DI1, D2=ledger/R1
  M fixture (affected {R1,R2}): T=payment_allocation, R1=payment/P1, R2=invoice/I1,
                                D1=payments/DP1, D2=ledger/R4, D3=invoices/DI1 (available extra),
                                R3=invoice/I2 (existing, not affected), U=ledger/R404 (checked absent)
These are matcher fixtures (truth chosen by hand), not generator data.
"""
from __future__ import annotations

import csv, io, json
from pathlib import Path

FIELDS = ["row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency", "entity_id", "invoice_id", "payment_id", "journal_id"]


def _write(root: Path, episode_id, ledger, invoices, payments, policy, truth):
    pub = root / "public"; pub.mkdir(parents=True)
    manifest = {"schema_version": "1.0", "episode_id": episode_id, "base_currency": "KZT", "currency_exponents": {"KZT": 2},
                "period_start": "2026-11-01T00:00:00+05:00", "period_end": "2026-12-01T00:00:00+05:00", "period_end_exclusive": True,
                "timezone": "Asia/Almaty", "ledger_rows": len(ledger), "document_count": len(invoices) + len(payments), "rounding": "ROUND_HALF_UP"}
    (pub / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8", newline="\n")
    (pub / "policy.json").write_text(json.dumps(policy, indent=2), encoding="utf-8", newline="\n")
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n"); w.writeheader(); w.writerows(ledger)
    (pub / "ledger.csv").write_text(buf.getvalue(), encoding="utf-8", newline="\n")
    (pub / "fx.csv").write_text("date,source_currency,target_currency,rate\n", encoding="utf-8", newline="\n")
    for name, rows in (("invoices.jsonl", invoices), ("payments.jsonl", payments)):
        (pub / name).write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8", newline="\n")
    priv = root / "private"; priv.mkdir()
    (priv / "truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8", newline="\n")
    (priv / "provenance.json").write_text(json.dumps({"note": "hand matcher fixture for the review"}), encoding="utf-8", newline="\n")
    return root


def _row(i, ts, acc, d, c, ent, inv, pay, j):
    return {"row_id": f"R{i}", "timestamp": ts, "account": acc, "debit_minor": d, "credit_minor": c, "currency": "KZT",
            "entity_id": ent, "invoice_id": inv or "null", "payment_id": pay or "null", "journal_id": j}


def _invoice(n, amount, issued):
    return {"currency": "KZT", "doc_id": f"DI{n}", "due_at": "2026-12-15T00:00:00+05:00", "entity_id": f"C{n}", "invoice_id": f"I{n}",
            "issued_at": issued, "line_items": [{"amount_minor": amount, "description": "fixture"}], "total_minor": amount}


def reward_fixture(root: Path, policy: dict, with_b: bool):
    """I1 (10000) and I2 (20000) issued 11-30, recognised 12-01 (cut-offs); I3-I5 recognised on issue."""
    ledger, invoices = [], []
    specs = [(1, 10000, "2026-11-30T10:00:00+05:00", "2026-12-01T10:00:00+05:00"), (2, 20000, "2026-11-30T11:00:00+05:00", "2026-12-01T11:00:00+05:00"),
             (3, 30000, "2026-11-10T10:00:00+05:00", None), (4, 40000, "2026-11-11T10:00:00+05:00", None), (5, 50000, "2026-11-12T10:00:00+05:00", None)]
    for n, amount, issued, recognised in specs:
        invoices.append(_invoice(n, amount, issued))
        k = len(ledger)
        ledger += [_row(k + 1, recognised or issued, "AR", amount, 0, f"C{n}", f"I{n}", None, f"J{n}"),
                   _row(k + 2, recognised or issued, "REVENUE", 0, amount, f"C{n}", f"I{n}", None, f"J{n}")]
    issues = [{"issue_id": "A", "type": "period_cutoff", "affected_ids": ["invoice/I1"], "expected_amount_minor": 10000, "currency": "KZT",
               "evidence_sets": [["invoices/DI1", "ledger/R1"]]}]
    if with_b:
        issues.append({"issue_id": "B", "type": "period_cutoff", "affected_ids": ["invoice/I2"], "expected_amount_minor": 20000, "currency": "KZT",
                       "evidence_sets": [["invoices/DI2", "ledger/R3"]]})
    eid = "GR-B" if with_b else "GR"
    return _write(root, eid, ledger, invoices, [], policy, {"episode_id": eid, "issues": issues})


def m_fixture(root: Path, policy: dict):
    """Payment P1 of 10000 designated to I1 is never applied before the boundary; I2 is a separate paid invoice."""
    ledger = [_row(1, "2026-11-05T10:00:00+05:00", "AR", 10000, 0, "C1", "I1", None, "J1"), _row(2, "2026-11-05T10:00:00+05:00", "REVENUE", 0, 10000, "C1", "I1", None, "J1"),
              _row(3, "2026-11-06T10:00:00+05:00", "CASH", 10000, 0, "C1", "I1", "P1", "J2"), _row(4, "2026-11-06T10:00:00+05:00", "UNAPPLIED", 0, 10000, "C1", "I1", "P1", "J2"),
              _row(5, "2026-11-07T10:00:00+05:00", "AR", 7000, 0, "C2", "I2", None, "J3"), _row(6, "2026-11-07T10:00:00+05:00", "REVENUE", 0, 7000, "C2", "I2", None, "J3")]
    invoices = [_invoice(1, 10000, "2026-11-05T10:00:00+05:00"), _invoice(2, 7000, "2026-11-07T10:00:00+05:00")]
    payments = [{"amount_minor": 10000, "currency": "KZT", "doc_id": "DP1", "invoice_refs": ["I1"], "payment_id": "P1",
                 "settlement_index": {"before": "2026-12-01T00:00:00+05:00", "unapplied_row_ids": ["R4"]}, "timestamp": "2026-11-06T10:00:00+05:00"}]
    truth = {"episode_id": "GM", "issues": [{"issue_id": "A", "type": "payment_allocation", "affected_ids": ["payment/P1", "invoice/I1"],
                                             "expected_amount_minor": 10000, "currency": "KZT", "evidence_sets": [["payments/DP1", "ledger/R4"]]}]}
    return _write(root, "GM", ledger, invoices, payments, policy, truth)


def public_refs(public: Path) -> set[str]:
    """Every evidence key the fixture's public files can serve (evaluator side, from the data itself)."""
    refs = {"ledger/" + r["row_id"] for r in csv.DictReader((public / "ledger.csv").open(encoding="utf-8"))}
    for t in ("invoices", "payments"):
        refs |= {f"{t}/" + json.loads(l)["doc_id"] for l in (public / f"{t}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    refs |= {f"fx/{r['date']}/{r['source_currency']}/{r['target_currency']}" for r in csv.DictReader((public / "fx.csv").open(encoding="utf-8"))}
    refs |= {"policy/" + k for k in json.loads((public / "policy.json").read_text(encoding="utf-8"))}
    return refs
