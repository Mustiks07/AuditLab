"""Hand-written financial fixtures X1/X2 (NOT generator output, NOT generator coverage).

Every number and the expected answer are written by hand below. The public format follows a real
profile-1.1 snapshot; policy.json is copied from it with only allocation.deadline set to this
fixture's period_end.

usage: python fixtures.py TEMPLATE_POLICY_JSON WORKDIR
"""
import csv, io, json, sys
from datetime import datetime
from pathlib import Path

FIELDS = ["row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency", "entity_id", "invoice_id", "payment_id", "journal_id"]


class Book:
    def __init__(self, eid, base, exps, tz, start, end):
        self.eid, self.base, self.exps, self.tz, self.start, self.end = eid, base, exps, tz, start, end
        self.rows, self.inv, self.pay, self.fx, self.n, self.j = [], [], [], [], 0, 0

    def post(self, ts, cur, amt, dr, cr, ent, inv=None, pay=None):
        self.j += 1; ids = []
        for acc, d, c in ((dr, amt, 0), (cr, 0, amt)):
            self.n += 1; ids.append(f"R{self.n}")
            self.rows.append({"row_id": f"R{self.n}", "timestamp": ts, "account": acc, "debit_minor": d, "credit_minor": c,
                              "currency": cur, "entity_id": ent, "invoice_id": inv or "null", "payment_id": pay or "null", "journal_id": f"J{self.j}"})
        return ids

    def invoice(self, doc, inv, ent, ts, cur, total, recognised_at=None):
        self.inv.append({"currency": cur, "doc_id": doc, "due_at": self.end, "entity_id": ent, "invoice_id": inv, "issued_at": ts,
                         "line_items": [{"amount_minor": total, "description": "Hand fixture service"}], "total_minor": total})
        return self.post(recognised_at or ts, cur, total, "AR", "REVENUE", ent, inv=inv)

    def payment(self, doc, pid, ent, ts, cur, amt, inv, settled_at, valuation=None):
        rows = {"receipt": self.post(ts, cur, amt, "CASH", "UNAPPLIED", ent, inv=inv, pay=pid)}
        if valuation is not None:
            rows["valuation"] = self.post(ts, self.base, valuation, "FX_VALUATION", "FX_OFFSET", ent, inv=inv, pay=pid)
        rows["settle"] = self.post(settled_at, cur, amt, "UNAPPLIED", "AR", ent, inv=inv, pay=pid)
        end = datetime.fromisoformat(self.end)
        idx = [r["row_id"] for r in self.rows if r["payment_id"] == pid and r["account"] == "UNAPPLIED" and datetime.fromisoformat(r["timestamp"]) < end]
        self.pay.append({"amount_minor": amt, "currency": cur, "doc_id": doc, "invoice_refs": [inv], "payment_id": pid,
                         "settlement_index": {"before": self.end, "unapplied_row_ids": idx}, "timestamp": ts})
        return rows

    def write(self, out, policy):
        pub = Path(out) / "public"; pub.mkdir(parents=True)
        man = {"base_currency": self.base, "currency_exponents": self.exps, "document_count": len(self.inv) + len(self.pay),
               "episode_id": self.eid, "ledger_rows": len(self.rows), "period_end": self.end, "period_end_exclusive": True,
               "period_start": self.start, "rounding": "ROUND_HALF_UP", "schema_version": "1.0", "timezone": self.tz}
        pol = json.loads(json.dumps(policy)); pol["allocation"]["deadline"] = self.end
        for name, obj in (("manifest.json", man), ("policy.json", pol)):
            (pub / name).write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        for name, fields, rows in (("ledger.csv", FIELDS, self.rows), ("fx.csv", ["date", "source_currency", "target_currency", "rate"], self.fx)):
            buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n"); w.writeheader(); w.writerows(rows)
            (pub / name).write_text(buf.getvalue(), encoding="utf-8", newline="\n")
        for name, rows in (("invoices.jsonl", self.inv), ("payments.jsonl", self.pay)):
            (pub / name).write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8", newline="\n")


def x1():
    """America/New_York, Nov 2026. Fall back 2026-11-01 02:00 EDT -> 01:00 EST. Base USD, EUR foreign."""
    b = Book("X1-NY-FALLBACK", "USD", {"EUR": 2, "USD": 2}, "America/New_York", "2026-11-01T00:00:00-04:00", "2026-12-01T00:00:00-05:00")
    b.fx = [{"date": d, "source_currency": "EUR", "target_currency": "USD", "rate": r}
            for d, r in (("2026-10-31", "1.11"), ("2026-11-01", "1.10"), ("2026-11-30", "1.20"), ("2026-12-01", "1.25"))]
    b.invoice("DI1", "I1", "C1", "2026-10-31T09:00:00-04:00", "EUR", 10000)
    # P1 at the FIRST 01:30 (EDT): local date 2026-11-01, 100.00 x 1.10 = 110.00 USD = 11000 = recorded -> clean
    b.payment("DP1", "P1", "C1", "2026-11-01T01:30:00-04:00", "EUR", 10000, "I1", "2026-11-01T01:31:00-04:00", valuation=11000)
    b.invoice("DI2", "I2", "C2", "2026-10-31T10:00:00-04:00", "EUR", 10000)
    # P2 at the SECOND 01:30 (EST, one hour later): same local date, expected 11000; recorded 11100 (rate 1.11 of 10-31) -> 100
    p2 = b.payment("DP2", "P2", "C2", "2026-11-01T01:30:00-05:00", "EUR", 10000, "I2", "2026-11-01T01:31:00-05:00", valuation=11100)
    b.invoice("DI3", "I3", "C3", "2026-11-20T10:00:00-05:00", "EUR", 5000)
    # P3 at 23:30 EST on 11-30 (= 04:30Z on 12-01): local date 11-30, 50.00 x 1.20 = 60.00 = 6000 = recorded -> clean
    # (a fixed -04:00 offset would read 12-01 00:30 and use 1.25 -> a false 250 finding)
    b.payment("DP3", "P3", "C3", "2026-11-30T23:30:00-05:00", "EUR", 5000, "I3", "2026-11-30T23:59:59-05:00", valuation=6000)
    # I4 issued 10-31 23:59 EDT (October), recognised exactly at period start 11-01 00:00 EDT -> period_cutoff 25000 USD
    i4 = b.invoice("DI4", "I4", "C4", "2026-10-31T23:59:00-04:00", "USD", 25000, recognised_at="2026-11-01T00:00:00-04:00")
    # I5 issued exactly at period start (inclusive) -> clean recognition
    b.invoice("DI5", "I5", "C5", "2026-11-01T00:00:00-04:00", "USD", 8000)
    # P4 received 11-30 23:59 EST, settled exactly at period_end (exclusive) -> 8000 USD unapplied at the boundary
    p4 = b.payment("DP4", "P4", "C5", "2026-11-30T23:59:00-05:00", "USD", 8000, "I5", "2026-12-01T00:00:00-05:00")
    truth = [
        {"issue_id": "X1-1", "type": "fx_conversion", "affected_ids": ["payment/P2"], "expected_amount_minor": 100, "currency": "USD",
         "evidence_sets": [["payments/DP2", "fx/2026-11-01/EUR/USD", "policy/fx_date", f"ledger/{p2['valuation'][0]}"]]},
        {"issue_id": "X1-2", "type": "period_cutoff", "affected_ids": ["invoice/I4"], "expected_amount_minor": 25000, "currency": "USD",
         "evidence_sets": [["invoices/DI4", f"ledger/{i4[0]}", "policy/period"]]},
        {"issue_id": "X1-3", "type": "payment_allocation", "affected_ids": ["invoice/I5", "payment/P4"], "expected_amount_minor": 8000, "currency": "USD",
         "evidence_sets": [["payments/DP4", f"ledger/{p4['receipt'][1]}", "policy/allocation"]]},
    ]
    return b, truth


def x2():
    """Europe/Berlin, Mar 2026. Spring forward 2026-03-29 02:00 CET -> 03:00 CEST. Base EUR, USD foreign."""
    b = Book("X2-BERLIN-SPRING", "EUR", {"EUR": 2, "USD": 2}, "Europe/Berlin", "2026-03-01T00:00:00+01:00", "2026-04-01T00:00:00+02:00")
    b.fx = [{"date": d, "source_currency": "USD", "target_currency": "EUR", "rate": r}
            for d, r in (("2026-03-29", "0.90"), ("2026-03-30", "0.91"), ("2026-03-31", "0.95"), ("2026-04-01", "0.96"))]
    b.invoice("DJ1", "J1", "K1", "2026-03-20T10:00:00+01:00", "USD", 20000)
    # Q1 at 01:59 CET (00:59Z), settled at 03:00 CEST (01:00Z, one real minute later): 200.00 x 0.90 = 180.00 = 18000 = recorded
    b.payment("DQ1", "Q1", "K1", "2026-03-29T01:59:00+01:00", "USD", 20000, "J1", "2026-03-29T03:00:00+02:00", valuation=18000)
    b.invoice("DJ2", "J2", "K2", "2026-03-20T11:00:00+01:00", "USD", 20000)
    # Q2 at 03:00 CEST: local date 03-29, expected 18000; recorded 18200 (0.91 of 03-30) -> 200
    q2 = b.payment("DQ2", "Q2", "K2", "2026-03-29T03:00:00+02:00", "USD", 20000, "J2", "2026-03-29T03:01:00+02:00", valuation=18200)
    b.invoice("DJ3", "J3", "K3", "2026-03-25T09:00:00+01:00", "USD", 3310)
    # Q3: 33.10 x 0.95 = 31.445 EUR = 3144.5 cents -> ROUND_HALF_UP 3145 = recorded (banker's rounding would give 3144)
    b.payment("DQ3", "Q3", "K3", "2026-03-31T23:30:00+02:00", "USD", 3310, "J3", "2026-03-31T23:59:00+02:00", valuation=3145)
    # J4 issued 03-31 23:59 CEST, recognised exactly at period_end 04-01 00:00 CEST -> period_cutoff 9000 EUR
    j4 = b.invoice("DJ4", "J4", "K4", "2026-03-31T23:59:00+02:00", "EUR", 9000, recognised_at="2026-04-01T00:00:00+02:00")
    truth = [
        {"issue_id": "X2-1", "type": "fx_conversion", "affected_ids": ["payment/Q2"], "expected_amount_minor": 200, "currency": "EUR",
         "evidence_sets": [["payments/DQ2", "fx/2026-03-29/USD/EUR", "policy/fx_date", f"ledger/{q2['valuation'][0]}"]]},
        {"issue_id": "X2-2", "type": "period_cutoff", "affected_ids": ["invoice/J4"], "expected_amount_minor": 9000, "currency": "EUR",
         "evidence_sets": [["invoices/DJ4", f"ledger/{j4[0]}", "policy/period"]]},
    ]
    return b, truth


if __name__ == "__main__":
    policy = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    work = Path(sys.argv[2])
    for build in (x1, x2):
        book, truth = build()
        out = work / "fixtures" / book.eid
        book.write(out, policy)
        (out / "hand-truth.json").write_text(json.dumps({"episode_id": book.eid, "issues": truth}, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(book.eid, "rows", len(book.rows), "docs", len(book.inv) + len(book.pay), "hand issues", len(truth))
