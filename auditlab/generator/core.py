"""Build the economic history and its public accounting representation."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from hashlib import sha256
from random import Random

from auditlab.contracts import MAX_EVIDENCE

from .validation import validate_accounting

LEDGER_FIELDS = (
    "row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency",
    "entity_id", "invoice_id", "payment_id", "journal_id",
)
FX_FIELDS = ("date", "source_currency", "target_currency", "rate")
PREFIXES = {
    "entity": "C", "invoice": "I", "payment": "P",
    "document": "D", "journal": "J", "row": "R", "issue": "X",
}


def random_stream(seed, domain):
    key = sha256(f"AuditLab/v1/{domain}/{seed}".encode("ascii")).digest()
    return Random(key)


class Company:
    def __init__(self, seed, period_start, period_end, zone, base_currency, exponents, *, config_version="1.0"):
        self.period_start = period_start
        self.period_end = period_end
        self.zone = zone
        self.base_currency = base_currency
        self.exponents = exponents
        self.config_version = config_version
        self.money_currency = next((c for c in exponents if c != base_currency), base_currency)
        self.events_rng = random_stream(seed, "events")
        self._scenario_seed = seed
        self._case_counts = Counter()
        self.noise_rng = random_stream(seed, "distractors")
        self.order_rng = random_stream(seed, "order")
        self._id_streams = {kind: random_stream(seed, "ids/" + kind) for kind in PREFIXES}
        self.invoices = []
        self.payments = []
        self.ledger = []
        self.fx = []
        self.issues = []
        self.events = []
        self.mutations = []

    def case_parameters(self, kind, units, *, day=6, hour=10, minute=0):
        """Independent event stream: toggling another defect does not change this case."""
        if self.config_version == "1.0":
            return units, day, hour, minute
        rng = random_stream(self._scenario_seed, f"case/{kind}/{self._case_counts[kind]}")
        self._case_counts[kind] += 1
        return (rng.randrange(max(1, units // 3), units * 3 + 1),
                rng.randrange(2, 21), rng.randrange(8, 18), rng.randrange(60))

    def id(self, kind):
        return f"{PREFIXES[kind]}{self._id_streams[kind].getrandbits(96):024X}"

    def invoice(self, entity_id, issued_at, currency, total_minor, *, invoice_id=None):
        due = max(self.period_end, issued_at.astimezone(self.zone) + timedelta(days=14))
        record = {
            "doc_id": self.id("document"), "invoice_id": invoice_id or self.id("invoice"),
            "entity_id": entity_id, "issued_at": issued_at.isoformat(), "due_at": due.isoformat(),
            "currency": currency, "total_minor": total_minor,
            "line_items": [{"description": "Commercial services", "amount_minor": total_minor}],
        }
        self.invoices.append(record)
        return record

    def post(self, timestamp, currency, amount, debit_account, credit_account, entity_id,
             *, invoice_id=None, payment_id=None):
        common = {
            "timestamp": timestamp.isoformat(), "currency": currency, "entity_id": entity_id,
            "invoice_id": invoice_id, "payment_id": payment_id, "journal_id": self.id("journal"),
        }
        debit = dict(common, row_id=self.id("row"), account=debit_account, debit_minor=amount, credit_minor=0)
        credit = dict(common, row_id=self.id("row"), account=credit_account, debit_minor=0, credit_minor=amount)
        self.ledger.extend((debit, credit))
        return debit, credit

    def issue(self, issue_type, affected_ids, amount_minor, currency, evidence_ids):
        self.issues.append({
            "issue_id": self.id("issue"), "type": issue_type, "affected_ids": sorted(affected_ids),
            "expected_amount_minor": amount_minor, "currency": currency,
            "evidence_sets": [list(evidence_ids)],
        })

    def payment(self, entity_id, timestamp, currency, amount, invoice_id=None):
        payment = {
            "doc_id": self.id("document"), "payment_id": self.id("payment"),
            "timestamp": timestamp.isoformat(), "currency": currency, "amount_minor": amount,
            "invoice_refs": [invoice_id] if invoice_id else [],
        }
        self.payments.append(payment)
        _, credit = self.post(timestamp, currency, amount, "CASH", "UNAPPLIED", entity_id,
                              invoice_id=invoice_id, payment_id=payment["payment_id"])
        return payment, credit

    def allocation(self, entity_id, currency, amount, first_amount, receipt_at, first_at, last_at,
                   *, leave_remainder=False):
        issued_at = receipt_at - timedelta(days=1)
        invoice = self.invoice(entity_id, issued_at, currency, amount)
        invoice_id = invoice["invoice_id"]
        self.post(issued_at, currency, amount, "AR", "REVENUE", entity_id, invoice_id=invoice_id)
        payment, receipt = self.payment(entity_id, receipt_at, currency, amount, invoice_id)
        payment_id = payment["payment_id"]
        first, _ = self.post(first_at, currency, first_amount, "UNAPPLIED", "AR", entity_id,
                             invoice_id=invoice_id, payment_id=payment_id)
        last = None
        if not leave_remainder:
            last, _ = self.post(last_at, currency, amount - first_amount, "UNAPPLIED", "AR", entity_id,
                                invoice_id=invoice_id, payment_id=payment_id)
        affected = [f"invoice/{invoice_id}", f"payment/{payment_id}"]
        self.events.append({
            "kind": "designated_payment", "affected_ids": affected, "currency": currency,
            "amount_minor": amount, "scheduled_tranches_minor": [first_amount, amount - first_amount],
            "receipt_at": receipt_at.isoformat(), "settled_at": last_at.isoformat() if last else None,
        })
        if leave_remainder or last_at >= self.period_end:
            self.mutations.append({
                "type": "payment_allocation", "affected_ids": affected,
                "before": {"second_tranche_minor": amount - first_amount,
                           "settled_at": min(last_at, self.period_end - timedelta(minutes=1)).isoformat()},
                "after": {"settled_at": last_at.isoformat() if last else None,
                          "remaining_minor": amount - first_amount},
                "effect": "Part of the designated payment is outstanding at the reporting boundary.",
            })
        return invoice, payment, receipt, first, last

    def boundary_pair_member(self, minute):
        receipt_at = self.period_start + timedelta(days=13, hours=16)
        scale = 10 ** self.exponents[self.base_currency]
        self.allocation(
            self.id("entity"), self.base_currency, 50_000 * scale, 30_000 * scale,
            receipt_at, receipt_at + timedelta(minutes=1), self.period_end + timedelta(minutes=minute),
        )

    def allocation_issue(self):
        _, day, hour, minute = self.case_parameters("allocation", 50_000, day=11, hour=10)
        receipt_at = self.period_start + timedelta(days=day, hours=hour, minutes=minute)
        scale = 10 ** self.exponents[self.base_currency]
        amount = self.events_rng.randrange(350, 900) * 100 * scale
        self.allocation(
            self.id("entity"), self.base_currency, amount, amount * 3 // 5, receipt_at,
            receipt_at + timedelta(days=1), receipt_at + timedelta(days=2), leave_remainder=True,
        )

    def close_settlement_indexes(self):
        movements = defaultdict(list)
        for row in self.ledger:
            if row["account"] == "UNAPPLIED" and datetime.fromisoformat(row["timestamp"]) < self.period_end:
                movements[row["payment_id"]].append(row)
        for payment in self.payments:
            payment_id = payment["payment_id"]
            rows = sorted(movements[payment_id], key=lambda row: (datetime.fromisoformat(row["timestamp"]), row["row_id"]))
            payment["settlement_index"] = {
                "before": self.period_end.isoformat(), "unapplied_row_ids": [row["row_id"] for row in rows],
            }
            remaining = sum(row["credit_minor"] - row["debit_minor"] for row in rows)
            received = datetime.fromisoformat(payment["timestamp"]) < self.period_end
            if payment["invoice_refs"] and received and remaining > 0:
                self.issue(
                    "payment_allocation",
                    [f"payment/{payment_id}", *(f"invoice/{key}" for key in payment["invoice_refs"])],
                    remaining, payment["currency"],
                    [f"payments/{payment['doc_id']}", *(f"ledger/{row['row_id']}" for row in rows),
                     "policy/allocation"],
                )

    def validate(self, expected_counts, policy):
        journals = defaultdict(int)
        row_keys = {row["row_id"] for row in self.ledger}
        invoice_keys = {invoice["invoice_id"] for invoice in self.invoices}
        payment_keys = {payment["payment_id"] for payment in self.payments}
        if len(row_keys) != len(self.ledger) or len(payment_keys) != len(self.payments):
            raise RuntimeError("duplicate_public_key")
        for row in self.ledger:
            debit, credit = row["debit_minor"], row["credit_minor"]
            if min(debit, credit) != 0 or max(debit, credit) <= 0 or row["currency"] not in self.exponents:
                raise RuntimeError("invalid_ledger_posting")
            if row["invoice_id"] is not None and row["invoice_id"] not in invoice_keys:
                raise RuntimeError("unknown_ledger_invoice")
            if row["payment_id"] is not None and row["payment_id"] not in payment_keys:
                raise RuntimeError("unknown_ledger_payment")
            journals[row["journal_id"], row["currency"]] += debit - credit
        if any(journals.values()):
            raise RuntimeError("unbalanced_journal")
        documents = self.invoices + self.payments
        if len({doc["doc_id"] for doc in documents}) != len(documents):
            raise RuntimeError("duplicate_document_key")
        for invoice in self.invoices:
            if invoice["total_minor"] != sum(item["amount_minor"] for item in invoice["line_items"]):
                raise RuntimeError("inconsistent_invoice_total")
        evidence = {f"ledger/{key}" for key in row_keys}
        evidence.update(f"invoices/{invoice['doc_id']}" for invoice in self.invoices)
        evidence.update(f"payments/{payment['doc_id']}" for payment in self.payments)
        evidence.update(f"policy/{key}" for key in policy)
        fx_keys = {"fx/" + "/".join(row[key] for key in FX_FIELDS[:-1]) for row in self.fx}
        if len(fx_keys) != len(self.fx):
            raise RuntimeError("duplicate_fx_key")
        evidence.update(fx_keys)
        for issue in self.issues:
            if issue["expected_amount_minor"] <= 0 or issue["currency"] not in self.exponents:
                raise RuntimeError("invalid_truth_amount")
            for group in issue["evidence_sets"]:
                if not 0 < len(group) <= MAX_EVIDENCE or len(group) != len(set(group)) or not evidence.issuperset(group):
                    raise RuntimeError("invalid_evidence_set")
        if Counter(issue["type"] for issue in self.issues) != Counter(expected_counts):
            raise RuntimeError("unintended_issue_counts")
        validate_accounting(self)
