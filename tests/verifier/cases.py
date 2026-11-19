"""Hand-built public snapshots (fictional, written by hand, not by the generator).

Every expected value below is computed by hand in the comment next to it. Amounts are minor
units: KZT and USD have 2 decimals, JPY 0, KWD 3.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import random
import re
from datetime import datetime, timezone

LEDGER_FIELDS = ["row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency",
                 "entity_id", "invoice_id", "payment_id", "journal_id"]
POLICY = {
    "allocation": {"deadline": "2026-12-01T00:00:00+05:00",
                   "rule": "Apply the entire designated payment before period_end (exclusive). UNAPPLIED is "
                           "credit-normal. Its remaining balance is credits minus debits."},
    "period": {"end_exclusive": True,
               "recognition": "invoice issue date for invoice; transaction date for cash and settlement"},
    "fx_date": "Local date of the source event",
    "rounding": "ROUND_HALF_UP",
}
EXPONENTS = {"KZT": 2, "USD": 2, "JPY": 0, "KWD": 3}


class Case:
    def __init__(self, episode_id: str, base: str = "KZT"):
        self.episode_id, self.base = episode_id, base
        self.ledger, self.invoices, self.payments, self.fx = [], [], [], []
        self.rows = self.journals = 0

    def journal(self, ts, *legs, currency="KZT", entity="C7", invoice=None, payment=None):
        """legs: (account, debit, credit). Returns row ids."""
        self.journals += 1
        ids = []
        for account, debit, credit in legs:
            self.rows += 1
            ids.append(f"R{self.rows}")
            self.ledger.append({"row_id": f"R{self.rows}", "timestamp": ts, "account": account,
                                "debit_minor": debit, "credit_minor": credit, "currency": currency,
                                "entity_id": entity, "invoice_id": invoice or "null",
                                "payment_id": payment or "null", "journal_id": f"J{self.journals}"})
        return ids

    def invoice(self, doc_id, invoice_id, issued_at, total, currency="KZT", entity="C7", **extra):
        self.invoices.append({"doc_id": doc_id, "invoice_id": invoice_id, "entity_id": entity,
                              "issued_at": issued_at, "due_at": "2026-12-31T23:59:59+05:00",
                              "currency": currency, "total_minor": total,
                              "line_items": [{"description": "Synthetic service", "amount_minor": total}],
                              **extra})

    def payment(self, doc_id, payment_id, ts, amount, refs, currency="KZT", **extra):
        self.payments.append({"doc_id": doc_id, "payment_id": payment_id, "timestamp": ts,
                              "currency": currency, "amount_minor": amount, "invoice_refs": refs, **extra})

    def rate(self, date, source, target, rate):
        self.fx.append({"date": date, "source_currency": source, "target_currency": target, "rate": rate})

    def index_payments(self):
        """settlement_index: every UNAPPLIED row of the payment before the boundary."""
        boundary = datetime.fromisoformat(POLICY["allocation"]["deadline"])
        for pay in self.payments:
            rows = [r["row_id"] for r in self.ledger if r["account"] == "UNAPPLIED"
                    and r["payment_id"] == pay["payment_id"]
                    and datetime.fromisoformat(r["timestamp"]) < boundary]
            pay["settlement_index"] = {"before": POLICY["allocation"]["deadline"], "unapplied_row_ids": rows}

    def write(self, root: Path) -> Path:
        self.index_payments()
        public = Path(root) / "public"
        public.mkdir(parents=True)
        currencies = sorted({self.base} | {r["currency"] for r in self.ledger}
                            | {d["currency"] for d in self.invoices + self.payments})
        manifest = {"schema_version": "1.0", "episode_id": self.episode_id, "base_currency": self.base,
                    "period_start": "2026-11-01T00:00:00+05:00", "period_end": "2026-12-01T00:00:00+05:00",
                    "timezone": "Asia/Almaty", "ledger_rows": len(self.ledger),
                    "document_count": len(self.invoices) + len(self.payments),
                    "currency_exponents": {c: EXPONENTS[c] for c in currencies}, "rounding": "ROUND_HALF_UP"}
        dump(public / "manifest.json", manifest)
        dump(public / "policy.json", POLICY)
        write_csv(public / "ledger.csv", LEDGER_FIELDS, self.ledger)
        write_csv(public / "fx.csv", ["date", "source_currency", "target_currency", "rate"], self.fx)
        write_jsonl(public / "invoices.jsonl", self.invoices)
        write_jsonl(public / "payments.jsonl", self.payments)
        return public


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")


def write_csv(path, fields, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows),
                    encoding="utf-8", newline="")


def F(type_, affected, amount, currency):
    return (type_, tuple(sorted(affected)), amount, currency)


# ------------------------------------------------------------------ single classes

def allocation_case(last_settlement: str | None, label="ALLOC"):
    """S01/S02 shape: 50 000 KZT received 14 Nov, 30 000 applied, last 20 000 at `last_settlement`."""
    c = Case(label)
    c.invoice("D4", "I4", "2026-11-01T10:00:00+05:00", 5_000_000)
    c.journal("2026-11-01T10:00:00+05:00", ("AR", 5_000_000, 0), ("REVENUE", 0, 5_000_000), invoice="I4")
    c.payment("D9", "P9", "2026-11-14T16:00:00+05:00", 5_000_000, ["I4"])
    c.journal("2026-11-14T16:00:00+05:00", ("CASH", 5_000_000, 0), ("UNAPPLIED", 0, 5_000_000),
              invoice="I4", payment="P9")
    c.journal("2026-11-14T16:01:00+05:00", ("UNAPPLIED", 3_000_000, 0), ("AR", 0, 3_000_000),
              invoice="I4", payment="P9")
    if last_settlement:
        c.journal(last_settlement, ("UNAPPLIED", 2_000_000, 0), ("AR", 0, 2_000_000), invoice="I4", payment="P9")
    return c


def split_payment_clean():
    """Two tranches 30 000 + 20 000 of one invoice, both fully applied in November -> clean."""
    c = Case("SPLIT")
    c.invoice("D4", "I4", "2026-11-01T10:00:00+05:00", 5_000_000)
    c.journal("2026-11-01T10:00:00+05:00", ("AR", 5_000_000, 0), ("REVENUE", 0, 5_000_000), invoice="I4")
    for n, (ts, amount) in enumerate((("2026-11-10T09:00:00+05:00", 3_000_000),
                                      ("2026-11-20T09:00:00+05:00", 2_000_000)), 1):
        c.payment(f"D{10 + n}", f"P{10 + n}", ts, amount, ["I4"])
        c.journal(ts, ("CASH", amount, 0), ("UNAPPLIED", 0, amount), invoice="I4", payment=f"P{10 + n}")
        c.journal(ts.replace(":00+", ":30+"), ("UNAPPLIED", amount, 0), ("AR", 0, amount),
                  invoice="I4", payment=f"P{10 + n}")
    return c


def refund_clean():
    """50 000 received, 30 000 applied, 20 000 refunded to the customer in November -> clean."""
    c = allocation_case(None, "REFUND")
    c.journal("2026-11-20T12:00:00+05:00", ("UNAPPLIED", 2_000_000, 0), ("CASH", 0, 2_000_000),
              invoice="I4", payment="P9")
    return c


def instructed_split(applied_second: int):
    """One payment of 80 000 for I4 (50 000) and I5 (30 000) by allocation_instructions."""
    c = Case("INSTR")
    for doc, inv, total in (("D4", "I4", 5_000_000), ("D5", "I5", 3_000_000)):
        c.invoice(doc, inv, "2026-11-02T10:00:00+05:00", total)
        c.journal("2026-11-02T10:00:00+05:00", ("AR", total, 0), ("REVENUE", 0, total), invoice=inv)
    c.payment("D9", "P9", "2026-11-14T16:00:00+05:00", 8_000_000, ["I4", "I5"],
              allocation_instructions=[{"invoice_id": "I4", "amount_minor": 5_000_000},
                                       {"invoice_id": "I5", "amount_minor": 3_000_000}])
    c.journal("2026-11-14T16:00:00+05:00", ("CASH", 8_000_000, 0), ("UNAPPLIED", 0, 8_000_000), payment="P9")
    c.journal("2026-11-14T16:01:00+05:00", ("UNAPPLIED", 5_000_000, 0), ("AR", 0, 5_000_000),
              invoice="I4", payment="P9")
    if applied_second:
        c.journal("2026-11-14T16:02:00+05:00", ("UNAPPLIED", applied_second, 0), ("AR", 0, applied_second),
                  invoice="I5", payment="P9")
    return c


def fx_payment_case(recorded_rate_minor: int, timestamp="2026-11-15T12:00:00+05:00"):
    """50 USD payment of a USD invoice, fully applied; KZT valuation journal of the whole payment."""
    c = Case("FXPAY")
    c.invoice("D4", "I4", "2026-11-03T10:00:00+05:00", 5_000, currency="USD")
    c.journal("2026-11-03T10:00:00+05:00", ("AR", 5_000, 0), ("REVENUE", 0, 5_000), currency="USD", invoice="I4")
    c.journal("2026-11-03T10:00:00+05:00", ("AR_VALUATION", 2_500_000, 0), ("REVENUE_VALUATION", 0, 2_500_000),
              invoice="I4")
    c.payment("D9", "P9", timestamp, 5_000, ["I4"], currency="USD")
    c.journal(timestamp, ("CASH", 5_000, 0), ("UNAPPLIED", 0, 5_000), currency="USD", invoice="I4", payment="P9")
    c.journal(timestamp, ("UNAPPLIED", 5_000, 0), ("AR", 0, 5_000), currency="USD", invoice="I4", payment="P9")
    c.journal(timestamp, ("CASH_VALUATION", recorded_rate_minor, 0), ("FX_CLEARING", 0, recorded_rate_minor),
              invoice="I4", payment="P9")
    c.rate("2026-11-03", "USD", "KZT", "500")
    c.rate("2026-11-15", "USD", "KZT", "500")
    c.rate("2026-12-01", "USD", "KZT", "510")
    return c


def cutoff_invoice_case(recognised_at: str):
    """KZT invoice issued 30 Nov 15:00; recognised at `recognised_at`; paid in December (clean part)."""
    c = Case("CUTOFF")
    c.invoice("D4", "I4", "2026-11-30T15:00:00+05:00", 1_000_000)
    c.journal(recognised_at, ("AR", 1_000_000, 0), ("REVENUE", 0, 1_000_000), invoice="I4")
    c.payment("D9", "P9", "2026-12-05T10:00:00+05:00", 1_000_000, ["I4"])
    c.journal("2026-12-05T10:00:00+05:00", ("CASH", 1_000_000, 0), ("UNAPPLIED", 0, 1_000_000),
              invoice="I4", payment="P9")
    c.journal("2026-12-05T10:01:00+05:00", ("UNAPPLIED", 1_000_000, 0), ("AR", 0, 1_000_000),
              invoice="I4", payment="P9")
    return c


def cutoff_payment_case(cash_posted_at: str, settled_at: str):
    """Payment dated 30 Nov 23:30 local; its CASH posting at `cash_posted_at`."""
    c = Case("CUTPAY")
    c.invoice("D4", "I4", "2026-11-10T10:00:00+05:00", 700_000)
    c.journal("2026-11-10T10:00:00+05:00", ("AR", 700_000, 0), ("REVENUE", 0, 700_000), invoice="I4")
    c.payment("D9", "P9", "2026-11-30T23:30:00+05:00", 700_000, ["I4"])
    c.journal(cash_posted_at, ("CASH", 700_000, 0), ("UNAPPLIED", 0, 700_000), invoice="I4", payment="P9")
    c.journal(settled_at, ("UNAPPLIED", 700_000, 0), ("AR", 0, 700_000), invoice="I4", payment="P9")
    return c


def fx_generic(currency: str, amount: int, rate: str, recorded: int, timestamp="2026-11-15T12:00:00+05:00"):
    """Payment in `currency` without an invoice; only its KZT valuation journal is checked."""
    c = Case("FX" + currency)
    c.payment("D9", "P9", timestamp, amount, [], currency=currency)
    c.journal(timestamp, ("CASH", amount, 0), ("SUSPENSE", 0, amount), currency=currency, payment="P9")
    c.journal(timestamp, ("CASH_VALUATION", recorded, 0), ("FX_CLEARING", 0, recorded), payment="P9")
    c.rate(timestamp[:10], currency, "KZT", rate)
    return c


def duplicate_case(second_recognition: bool, second_invoice_id="I4"):
    """Supplier invoice of 12 000 KZT imported twice (D4, D5). Clean when only one recognition."""
    c = Case("DUP")
    c.invoice("D4", "I4", "2026-11-05T10:00:00+05:00", 1_200_000, entity="V3", canonical_key="V3-2026-118")
    c.invoice("D5", second_invoice_id, "2026-11-05T10:00:00+05:00", 1_200_000, entity="V3",
              canonical_key="V3-2026-118")
    c.journal("2026-11-05T10:00:00+05:00", ("EXPENSE", 1_200_000, 0), ("AP", 0, 1_200_000), entity="V3", invoice="I4")
    if second_recognition:
        c.journal("2026-11-07T11:00:00+05:00", ("EXPENSE", 1_200_000, 0), ("AP", 0, 1_200_000), entity="V3",
                  invoice=second_invoice_id)
    return c


def equal_amount_different_suppliers():
    """Two real invoices of 12 000 KZT from different suppliers, each recognised once -> clean."""
    c = Case("EQUAL")
    for doc, inv, entity in (("D4", "I4", "V3"), ("D5", "I5", "V8")):
        c.invoice(doc, inv, "2026-11-05T10:00:00+05:00", 1_200_000, entity=entity)
        c.journal("2026-11-05T10:00:00+05:00", ("EXPENSE", 1_200_000, 0), ("AP", 0, 1_200_000),
                  entity=entity, invoice=inv)
    return c


# ------------------------------------------------------------------ interactions (SPEC §6)

def interaction_a(allocation_defect: bool, fx_defect: bool):
    """A: 50 USD for one USD invoice; 30 USD applied; rest stays unapplied if allocation_defect.
    Valuation of the whole payment at 510 instead of 500 if fx_defect."""
    c = Case("IA")
    c.invoice("D4", "I4", "2026-11-03T10:00:00+05:00", 5_000, currency="USD")
    c.journal("2026-11-03T10:00:00+05:00", ("AR", 5_000, 0), ("REVENUE", 0, 5_000), currency="USD", invoice="I4")
    c.journal("2026-11-03T10:00:00+05:00", ("AR_VALUATION", 2_500_000, 0), ("REVENUE_VALUATION", 0, 2_500_000),
              invoice="I4")
    ts = "2026-11-14T16:00:00+05:00"
    c.payment("D9", "P9", ts, 5_000, ["I4"], currency="USD")
    c.journal(ts, ("CASH", 5_000, 0), ("UNAPPLIED", 0, 5_000), currency="USD", invoice="I4", payment="P9")
    c.journal("2026-11-14T16:01:00+05:00", ("UNAPPLIED", 3_000, 0), ("AR", 0, 3_000), currency="USD",
              invoice="I4", payment="P9")
    if not allocation_defect:
        c.journal("2026-11-20T10:00:00+05:00", ("UNAPPLIED", 2_000, 0), ("AR", 0, 2_000), currency="USD",
                  invoice="I4", payment="P9")
    value = 2_550_000 if fx_defect else 2_500_000
    c.journal(ts, ("CASH_VALUATION", value, 0), ("FX_CLEARING", 0, value), invoice="I4", payment="P9")
    c.rate("2026-11-03", "USD", "KZT", "500")
    c.rate("2026-11-14", "USD", "KZT", "500")
    return c


def interaction_b(cutoff_defect: bool, fx_defect: bool):
    """B: 100 USD invoice of 30 Nov; must be recognised in November at 500 KZT/USD.
    cutoff_defect posts it in December; fx_defect values it at 510 (the December rate)."""
    c = Case("IB")
    c.invoice("D4", "I4", "2026-11-30T14:00:00+05:00", 10_000, currency="USD")
    posted = "2026-12-01T09:00:00+05:00" if cutoff_defect else "2026-11-30T14:00:00+05:00"
    c.journal(posted, ("AR", 10_000, 0), ("REVENUE", 0, 10_000), currency="USD", invoice="I4")
    value = 5_100_000 if fx_defect else 5_000_000
    c.journal(posted, ("AR_VALUATION", value, 0), ("REVENUE_VALUATION", 0, value), invoice="I4")
    c.rate("2026-11-30", "USD", "KZT", "500")
    c.rate("2026-12-01", "USD", "KZT", "510")
    return c


# ------------------------------------------------------------------ metamorphic transforms

ID_FIELDS = {"row_id": "R", "journal_id": "J", "invoice_id": "I", "payment_id": "P", "entity_id": "C",
             "doc_id": "D"}


def rename_ids(public: Path, target: Path, seed: int) -> dict[str, str]:
    """Bijective rename of every public ID (reversing their string order) and permute all rows."""
    rng = random.Random(seed)
    ledger = list(csv.DictReader(io.StringIO((public / "ledger.csv").read_text(encoding="utf-8"))))
    fx = list(csv.DictReader(io.StringIO((public / "fx.csv").read_text(encoding="utf-8"))))
    docs = {t: [json.loads(l) for l in (public / f"{t}.jsonl").read_text(encoding="utf-8").splitlines() if l]
            for t in ("invoices", "payments")}
    seen: dict[str, set] = {k: set() for k in ID_FIELDS}
    for row in ledger:
        for key in ("row_id", "journal_id", "invoice_id", "payment_id", "entity_id"):
            if row[key] != "null":
                seen[key].add(row[key])
    for table in docs.values():
        for doc in table:
            for key in ("doc_id", "invoice_id", "payment_id", "entity_id"):
                if doc.get(key):
                    seen[key].add(doc[key])
            for ref in doc.get("invoice_refs", []):
                seen["invoice_id"].add(ref)
    mapping: dict[str, dict[str, str]] = {}
    for key, values in seen.items():
        ordered = sorted(values)
        numbers = rng.sample(range(100, 999), len(ordered))
        numbers.sort(reverse=True)  # the smallest original ID gets the largest new ID
        prefix = {"row_id": "L", "journal_id": "G", "invoice_id": "B", "payment_id": "M",
                  "entity_id": "E", "doc_id": "Q"}[key]
        mapping[key] = {old: f"{prefix}{n}" for old, n in zip(ordered, numbers)}
    m = mapping
    for row in ledger:
        for key in ("row_id", "journal_id", "invoice_id", "payment_id", "entity_id"):
            if row[key] != "null":
                row[key] = m[key][row[key]]
    for table in docs.values():
        for doc in table:
            for key in ("doc_id", "invoice_id", "payment_id", "entity_id"):
                if doc.get(key):
                    doc[key] = m[key][doc[key]]
            if "invoice_refs" in doc:
                doc["invoice_refs"] = [m["invoice_id"][x] for x in doc["invoice_refs"]]
            for item in doc.get("allocation_instructions", []):
                item["invoice_id"] = m["invoice_id"][item["invoice_id"]]
            if "settlement_index" in doc:
                index = doc["settlement_index"]
                index["unapplied_row_ids"] = [m["row_id"][x] for x in index["unapplied_row_ids"]]
                rng.shuffle(index["unapplied_row_ids"])
    for table in (ledger, fx, docs["invoices"], docs["payments"]):
        rng.shuffle(table)
    target.mkdir(parents=True)
    for name in ("manifest.json", "policy.json"):
        (target / name).write_bytes((public / name).read_bytes())
    write_csv(target / "ledger.csv", LEDGER_FIELDS, ledger)
    write_csv(target / "fx.csv", ["date", "source_currency", "target_currency", "rate"], fx)
    write_jsonl(target / "invoices.jsonl", docs["invoices"])
    write_jsonl(target / "payments.jsonl", docs["payments"])
    # Public reference -> renamed public reference, to map findings back.
    refs = {}
    for old, new in m["row_id"].items():
        refs[f"ledger/{old}"] = f"ledger/{new}"
    for table in ("invoices", "payments"):
        for old, new in m["doc_id"].items():
            refs[f"{table}/{old}"] = f"{table}/{new}"
    for old, new in m["invoice_id"].items():
        refs[f"invoice/{old}"] = f"invoice/{new}"
    for old, new in m["payment_id"].items():
        refs[f"payment/{old}"] = f"payment/{new}"
    return refs


def to_utc(public: Path, target: Path):
    """Rewrite every timestamp as the same instant in UTC (offset +00:00)."""
    target.mkdir(parents=True)
    pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")

    def utc(match):
        return datetime.fromisoformat(match.group(0)).astimezone(timezone.utc).isoformat()

    for name in ("ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv", "policy.json"):
        (target / name).write_text(pattern.sub(utc, (public / name).read_text(encoding="utf-8")),
                                   encoding="utf-8", newline="")
    (target / "manifest.json").write_bytes((public / "manifest.json").read_bytes())
