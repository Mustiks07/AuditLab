"""Financial counterfactuals and relationships checked against independent public answers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from auditlab.generator import generate_episode
from auditlab.generator.financial_cases import AMOUNT_RANGE
from auditlab.release.rehearsal import file_hashes
from auditlab.release.transforms import read_json, write_json
from auditlab.verifier.crosscheck import _read_units, _write_units
from datasets.audit_families import fingerprints

from .families import answers_match, renamed_snapshot


VERSION = "financial-pairs-1.0"
ALLOCATION = "payment_allocation"
FX = "fx_conversion"
CUTOFF = "period_cutoff"


@dataclass(frozen=True)
class FinancialVariant:
    kind: str
    snapshot: Path
    config: dict
    relationship: str
    reference_map: dict = field(default_factory=dict, repr=False)


def build_financial_variants(parent: Path, seed: int, config: dict, output: Path, *,
                             amount_minor: int | None = None) -> list[FinancialVariant]:
    """Publish controls in a fresh directory. Each returned snapshot is a child of parent."""
    financial = config.get("financial_case") or {}
    counts = {key: value for key, value in config.get("issue_counts", {}).items() if value}
    interaction = config.get("interaction")
    if interaction == "allocation_fx_same_payment" and counts == {ALLOCATION: 1, FX: 1}:
        specifications = (
            ("fixed", "Correct only the FX valuation, leaving the designated payment outstanding.",
             {"issue_counts": {ALLOCATION: 1, FX: 0}}),
            ("interaction", "Clear allocation on the shared payment and retain its original FX discrepancy.",
             {"issue_counts": {ALLOCATION: 0, FX: 1}}),
        )
    elif config.get("config_version") == "1.2" and interaction is None and (
        (financial.get("kind") == "allocation" and "settlement_offset_minutes" in financial
         and config.get("allocation_mode", "partial") == "partial" and counts == {ALLOCATION: 1})
        or (financial.get("kind") == "invoice_cutoff" and counts == {CUTOFF: 1})
    ):
        original_amount = financial["amount_minor"]
        delta = max(17, original_amount // 3)
        nominal = original_amount + delta if original_amount + delta <= AMOUNT_RANGE[1] else original_amount - delta
        if amount_minor is not None:
            nominal = amount_minor
        if type(nominal) is not int or not AMOUNT_RANGE[0] <= nominal <= AMOUNT_RANGE[1] or nominal == original_amount:
            raise ValueError("Amount control requires a different supported nominal amount")
        allocation = financial["kind"] == "allocation"
        amount_relation = ("Change the linked invoice and payment nominal at the same currency and pre-deadline share."
                           if allocation else "Change the invoice nominal without moving its recognition date.")
        crossing_relation = ("Move the same final settlement from at or after the deadline to before it."
                             if allocation else "Move the same recognition into the invoice's reporting period.")
        specifications = (
            ("amount", amount_relation, {"financial_case": {**financial, "amount_minor": nominal}}),
            ("period_crossing", crossing_relation, {"issue_counts": {ALLOCATION if allocation else CUTOFF: 0}}),
        )
    else:
        raise ValueError("Expected a partial delayed allocation, an invoice cutoff, or a two-fault allocation/FX focus")

    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".financial-pairs-", dir=output.parent) as temporary:
        staged = Path(temporary) / "controls"
        mapping = renamed_snapshot(parent, staged / "id_order", seed + 1)
        _append_refund(parent, staged / "lawful_distractor", seed + 2)
        variants = [FinancialVariant("id_order", output / "id_order", config,
                        "Rename IDs and shuffle rows without changing any finding.", mapping["references"]),
                    FinancialVariant("lawful_distractor", output / "lawful_distractor", config,
                        "Add one fully refunded unassigned deposit and leave existing records and findings intact.")]
        for kind, relationship, modifications in specifications:
            changed = {**config, **modifications}
            generate_episode(seed, changed, staged / kind)
            variants.append(FinancialVariant(kind, output / kind, changed, relationship))
        staged.rename(output)
    return variants


def _scoped_id(prefix, seed, episode, field):
    suffix = sha256(f"{VERSION}:{seed}:{episode}:{field}".encode()).hexdigest().upper()
    return prefix + suffix[:32 if prefix == "E" else 24]


def _append_refund(parent: Path, target: Path, seed: int) -> None:
    """Append only a source-backed receipt and refund to both books, retaining original records."""
    private = parent / "private"
    provenance = read_json(private / "provenance.json")
    transition = read_json(private / "source_transition.json")
    truth = read_json(private / "truth.json")
    manifest = read_json(parent / "public/manifest.json")
    policy = read_json(parent / "public/policy.json")
    parent_episode = manifest["episode_id"]
    deadline = datetime.fromisoformat(policy["allocation"]["deadline"])
    zone = ZoneInfo(manifest["timezone"])
    received = datetime.fromisoformat(manifest["period_start"]).astimezone(zone) + timedelta(days=2, hours=9)
    refunded = received + timedelta(days=2)
    currency = manifest["base_currency"]
    amount = (123 + seed % 911) * 10 ** manifest["currency_exponents"][currency]
    if refunded >= deadline:
        raise ValueError("Refund control does not fit before the reporting deadline")

    doc_id = _scoped_id("D", seed, parent_episode, "document")
    payment_id = _scoped_id("P", seed, parent_episode, "payment")
    entity_id = _scoped_id("C", seed, parent_episode, "entity")
    ledger = []
    for name, at, debit, credit in (("receipt", received, "CASH", "UNAPPLIED"),
                                    ("refund", refunded, "UNAPPLIED", "CASH")):
        common = {"timestamp": at.isoformat(), "currency": currency, "invoice_id": "null",
                  "entity_id": entity_id, "payment_id": payment_id,
                  "journal_id": _scoped_id("J", seed, parent_episode, name)}
        ledger.extend((
            {**common, "row_id": _scoped_id("R", seed, parent_episode, name + "-debit"), "account": debit,
             "debit_minor": amount, "credit_minor": 0},
            {**common, "row_id": _scoped_id("R", seed, parent_episode, name + "-credit"), "account": credit,
             "debit_minor": 0, "credit_minor": amount},
        ))
    payment = {"doc_id": doc_id, "payment_id": payment_id, "timestamp": received.isoformat(),
               "currency": currency, "amount_minor": amount, "invoice_refs": [],
               "settlement_index": {"before": deadline.isoformat(), "unapplied_row_ids": [ledger[1]["row_id"],
                                                                                          ledger[2]["row_id"]]}}

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    with TemporaryDirectory(prefix=".refund-control-", dir=target.parent) as temporary:
        staged = Path(temporary) / "snapshot"
        for origin, destination, label in ((parent / "public", staged / "public", "final"),
                                           (private / "source_book", staged / "private/source_book", "source")):
            units = _read_units(origin)
            if len(units["ledger"]) + 4 > 2000 or len(units["invoices"]) + len(units["payments"]) + 1 > 200:
                raise ValueError("Refund control would exceed the source or public company size")
            units["ledger"].extend(ledger)
            units["payments"].append(json.dumps(payment, sort_keys=True, separators=(",", ":")))
            rng = Random(f"{VERSION}:{seed}:{parent_episode}:{label}")
            rng.shuffle(units["ledger"])
            rng.shuffle(units["payments"])
            _write_units(origin, destination, units)
            updated = read_json(destination / "manifest.json")
            updated["episode_id"] = _scoped_id("E", seed, updated["episode_id"], label)
            write_json(destination / "manifest.json", updated)
            transition[f"{'target' if label == 'final' else 'source'}_episode_id"] = updated["episode_id"]
        truth["episode_id"] = transition["target_episode_id"]
        provenance["episode_id"] = truth["episode_id"]
        provenance["transform"] = {"version": VERSION, "kind": "lawful_distractor", "seed": seed,
                                   "parent_episode_id": parent_episode}
        provenance["events"].append({"kind": "refund", "payment_id": payment_id, "amount_minor": amount,
                                      "returned_at": refunded.isoformat()})
        staged_private = staged / "private"
        transition_path = staged_private / "source_transition.json"
        write_json(transition_path, transition)
        provenance["source_book"].update(episode_id=transition["source_episode_id"],
                                         files_sha256=file_hashes(staged_private / "source_book"),
                                         target_files_sha256=file_hashes(staged / "public"),
                                         transition_sha256=sha256(transition_path.read_bytes()).hexdigest())
        counts = provenance["source_book_validation"]
        counts["ledger_rows"] += 4
        counts["document_count"] += 1
        write_json(staged_private / "provenance.json", provenance)
        write_json(staged_private / "truth.json", truth)
        staged.rename(target)


class _Book:
    def __init__(self, public: Path):
        self.public = public
        self.manifest = read_json(public / "manifest.json")
        self.policy = read_json(public / "policy.json")
        self.deadline = datetime.fromisoformat(self.policy["allocation"]["deadline"])
        units = _read_units(public)
        self.rows = {r["row_id"]: r for r in units["ledger"]}
        self.invoices = {d["doc_id"]: d for d in map(json.loads, units["invoices"])}
        self.payments = {d["payment_id"]: d for d in map(json.loads, units["payments"])}
        self.rates = {(row["date"], row["source_currency"], row["target_currency"]): row["rate"] for row in units["fx"]}

    def for_payment(self, payment_id):
        return [row for row in self.rows.values() if row["payment_id"] == payment_id]


def _amount(row, side):
    return int(row[side + "_minor"])


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _affected_payment(finding):
    refs = [ref.split("/", 1)[1] for ref in finding["affected_ids"] if ref.startswith("payment/")]
    _require(len(refs) == 1, "Expected exactly one named focal payment")
    return refs[0]


def _allocation(book, finding):
    payment_id = _affected_payment(finding)
    payment = book.payments[payment_id]
    invoice_refs = payment["invoice_refs"]
    _require(len(invoice_refs) == 1 and set(finding["affected_ids"]) == {f"payment/{payment_id}",
              f"invoice/{invoice_refs[0]}"}, "Independent finding did not name the designated invoice and payment")
    invoice, = (doc for doc in book.invoices.values() if doc["invoice_id"] == invoice_refs[0])
    currency = payment["currency"]
    _require(invoice["total_minor"] == payment["amount_minor"] and invoice["currency"] == currency,
             "Invoice and designated payment disagree on amount or currency")
    _require(datetime.fromisoformat(payment["timestamp"]) < book.deadline,
             "Designated payment was received outside the allocation period")
    movements = [r for r in book.for_payment(payment_id) if r["account"] == "UNAPPLIED"]
    _require(all(row["currency"] == currency and row["invoice_id"] == invoice_refs[0] for row in movements),
             "Allocation movements do not link to the designated invoice and currency")
    before = [r for r in movements if datetime.fromisoformat(r["timestamp"]) < book.deadline]
    posted = {r["row_id"]: {"amount_minor": _amount(r, "debit"), "timestamp": r["timestamp"],
                           "journal_id": r["journal_id"]} for r in movements if _amount(r, "debit")}
    credits, debits = (sum(_amount(r, side) for r in before) for side in ("credit", "debit"))
    _require(credits == payment["amount_minor"], "Receipt does not substantiate the designated amount")
    return {"payment_id": payment_id, "source_refs": [f"payments/{payment['doc_id']}", f"invoices/{invoice['doc_id']}",
             "policy/allocation", *sorted(f"ledger/{r['row_id']}" for r in before)], "currency": currency,
            "nominal_minor": payment["amount_minor"], "deadline": book.deadline.isoformat(),
            "credits_before_minor": credits, "debits_before_minor": debits,
            "balance_minor": credits - debits, "settlements": posted}


def _fx(book, payment_id):
    payment = book.payments[payment_id]
    zone = ZoneInfo(book.manifest["timezone"])
    day = datetime.fromisoformat(payment["timestamp"]).astimezone(zone).date().isoformat()
    base, source = book.manifest["base_currency"], payment["currency"]
    exponent = book.manifest["currency_exponents"]
    rate = book.rates[day, source, base]
    rows = [row for row in book.for_payment(payment_id) if row["account"] == "FX_VALUATION"]
    _require(len(rows) == 1 and rows[0]["currency"] == base, "Expected one reporting valuation of the shared payment")
    exact = Fraction(payment["amount_minor"], 10 ** exponent[source]) * Fraction(rate) * 10 ** exponent[base]
    expected = int(exact + Fraction(1, 2))
    recorded = _amount(rows[0], "debit") - _amount(rows[0], "credit")
    return {"payment_id": payment_id, "quote_date": day, "rate": rate, "currency": base,
            "exact_base_minor": str(exact), "expected_minor": expected, "recorded_minor": recorded,
            "difference_minor": abs(recorded - expected), "source_refs": [f"payments/{payment['doc_id']}",
             f"ledger/{rows[0]['row_id']}", f"fx/{day}/{source}/{base}", "policy/fx_date"]}


def _finding(finding, amount=None):
    return {"type": finding["type"], "affected_ids": sorted(finding["affected_ids"]),
            "claimed_amount_minor": finding["claimed_amount_minor"] if amount is None else amount,
            "currency": finding["currency"]}


def _recognition(book, finding):
    focal, = finding["affected_ids"]
    kind, invoice_id = focal.split("/", 1)
    _require(kind == "invoice", "Expected a named focal invoice")
    invoice, = (doc for doc in book.invoices.values() if doc["invoice_id"] == invoice_id)
    amounts = [row for row in book.rows.values() if row["invoice_id"] == invoice_id and row["payment_id"] == "null"]
    ar, = (row for row in amounts if row["account"] == "AR" and _amount(row, "debit") > 0)
    revenue, = (row for row in amounts if row["account"] == "REVENUE" and row["journal_id"] == ar["journal_id"])
    _require(ar["currency"] == invoice["currency"] == revenue["currency"]
             and _amount(ar, "debit") == invoice["total_minor"] == _amount(revenue, "credit")
             and revenue["timestamp"] == ar["timestamp"], "Recognition does not match the public invoice")
    start, end = (datetime.fromisoformat(book.manifest[key]) for key in ("period_start", "period_end"))
    issued, recognized = (datetime.fromisoformat(value) for value in (invoice["issued_at"], ar["timestamp"]))
    _require(book.policy["period"]["end_exclusive"] is True and min(issued, recognized) >= start,
             "Invoice and recognition must use the published exclusive reporting period")
    return {"invoice_id": invoice_id, "nominal_minor": invoice["total_minor"], "currency": invoice["currency"],
            "issued_at": issued.isoformat(), "recognized_at": recognized.isoformat(),
            "issued_inside_period": issued < end, "recognized_inside_period": recognized < end,
            "period_end": end.isoformat(),
            "source_refs": [f"invoices/{invoice['doc_id']}", f"ledger/{ar['row_id']}", "policy/period"]}


def _invoice_change(before, after, variant, original, evidence):
    _require(len(original) == 1 and original[0]["type"] == CUTOFF,
             "Independent baseline lacks the exact declared invoice cutoff violation")
    finding = original[0]
    base, control = _recognition(before, finding), _recognition(after, finding)
    evidence.update(before_recognition=base, after_recognition=control)
    _require(base["issued_inside_period"] != base["recognized_inside_period"]
             and base["nominal_minor"] == finding["claimed_amount_minor"]
             and base["currency"] == finding["currency"]
             and all(base[key] == control[key] for key in ("invoice_id", "currency", "issued_at", "period_end", "source_refs")),
             "Baseline recognition is not across the deadline from the same supported invoice")
    if variant.kind == "amount":
        _require(control["recognized_at"] == base["recognized_at"]
                 and control["nominal_minor"] == variant.config["financial_case"]["amount_minor"] != base["nominal_minor"],
                 "Nominal change did not preserve the recognition event")
        return [_finding(finding, control["nominal_minor"])]
    _require(variant.kind == "period_crossing" and control["recognized_inside_period"] == base["issued_inside_period"]
             and control["nominal_minor"] == base["nominal_minor"],
             "Recognition did not cross back into the correct period at the same nominal amount")
    return []


def _refund_proof(before, after):
    mutable_manifest = {"episode_id", "ledger_rows", "document_count"}
    _require({key: value for key, value in before.manifest.items() if key not in mutable_manifest}
             == {key: value for key, value in after.manifest.items() if key not in mutable_manifest},
             "A lawful distractor changed the period, currencies or public manifest rules")
    _require(before.policy == after.policy and before.rates == after.rates and before.invoices == after.invoices,
             "A lawful distractor changed an existing invoice, rate or policy")
    _require(all(value == after.rows.get(key) for key, value in before.rows.items())
             and all(value == after.payments.get(key) for key, value in before.payments.items()),
             "A lawful distractor changed an existing posting or payment")
    payments = after.payments.keys() - before.payments.keys()
    added = after.rows.keys() - before.rows.keys()
    _require(len(payments) == 1 and len(added) == 4, "Expected only one new deposit and four sides")
    payment_id, = payments
    payment = after.payments[payment_id]
    _require(payment["invoice_refs"] == [], "A designated payment cannot be the lawful refund")
    rows = [after.rows[key] for key in added]
    at = datetime.fromisoformat(payment["timestamp"])
    dates = sorted({datetime.fromisoformat(row["timestamp"]) for row in rows})
    _require(len(dates) == 2 and dates[0] == at and dates[1] < after.deadline,
             "The public deposit and actual refund do not precede the deadline in order")
    amount = payment["amount_minor"]
    index = payment["settlement_index"]
    _require(datetime.fromisoformat(index["before"]) == after.deadline
             and set(index["unapplied_row_ids"]) == {row["row_id"] for row in rows if row["account"] == "UNAPPLIED"},
             "Public settlement index does not expose the deposit and refund")
    for instant, debit, credit in ((dates[0], "CASH", "UNAPPLIED"), (dates[1], "UNAPPLIED", "CASH")):
        sides = [row for row in rows if datetime.fromisoformat(row["timestamp"]) == instant]
        _require(len({row["journal_id"] for row in sides}) == 1
                 and all(row["payment_id"] == payment_id and row["currency"] == payment["currency"] for row in sides)
                 and {(row["account"], _amount(row, "debit"), _amount(row, "credit")) for row in sides}
                 == {(debit, amount, 0), (credit, 0, amount)}, "The new deposit/refund journal does not clear cash and liability")
    return {"payment_id": payment_id, "currency": payment["currency"], "received_minor": amount,
            "refunded_minor": amount, "balance_minor": 0, "received_at": dates[0].isoformat(),
            "refunded_at": dates[1].isoformat(), "source_refs": [f"payments/{payment['doc_id']}",
                                                               *(f"ledger/{key}" for key in sorted(added))]}


def _crossing(before, after):
    _require(before["payment_id"] == after["payment_id"] and before["nominal_minor"] == after["nominal_minor"]
             and before["currency"] == after["currency"] and before["deadline"] == after["deadline"],
             "Period crossing changed the underlying payment terms")
    old, new = before["settlements"], after["settlements"]
    _require(old.keys() == new.keys(), "Period crossing changed the set of settlement rows")
    changed = [row for row in old if old[row] != new[row]]
    _require(len(changed) == 1, "Expected exactly one settlement to cross the reporting boundary")
    key = changed[0]
    _require(old[key]["journal_id"] == new[key]["journal_id"]
             and old[key]["amount_minor"] == new[key]["amount_minor"] == before["balance_minor"]
             and datetime.fromisoformat(new[key]["timestamp"]) < datetime.fromisoformat(before["deadline"])
             <= datetime.fromisoformat(old[key]["timestamp"]) and after["balance_minor"] == 0,
             "The same settlement did not close the outstanding balance across the exclusive deadline")
    return {"row_ref": f"ledger/{key}", "from": old[key], "to": new[key]}


def check_financial_variant(parent: Path, variant: FinancialVariant, baseline_answer: dict,
                            variant_answer: dict) -> dict:
    """Check a paired relationship using fixed independent answers and only public records."""
    report = {"schema_version": "1.0", "relationship_protocol": VERSION,
              "kind": variant.kind, "relationship": variant.relationship,
              "expected": None, "observed": None, "public_calculation": {}}
    try:
        before, after = _Book(parent / "public"), _Book(variant.snapshot / "public")
        for book, answer in ((before, baseline_answer), (after, variant_answer)):
            _require(answer["episode_id"] == book.manifest["episode_id"] and not answer.get("notes"),
                     "Independent answer did not bind cleanly to the public snapshot")
        original = [_finding(item) for item in baseline_answer["findings"]]
        actual = [_finding(item) for item in variant_answer["findings"]]
        report["observed"] = actual
        evidence = report["public_calculation"]
        if variant.kind == "id_order":
            renamed = [variant.reference_map[f"ledger/{key}"].split("/", 1)[1] for key in before.rows]
            _require(before.policy == after.policy
                     and {key: value for key, value in before.manifest.items() if key != "episode_id"}
                     == {key: value for key, value in after.manifest.items() if key != "episode_id"}
                     and fingerprints(before.public) == fingerprints(after.public)
                     and set(renamed) == after.rows.keys() and not before.rows.keys() & after.rows.keys()
                     and renamed != list(after.rows), "The derivative did not preserve economics, rename IDs and change order")
            expected = [{**finding, "affected_ids": sorted(variant.reference_map[key] for key in finding["affected_ids"])}
                        for finding in original]
        elif variant.kind == "lawful_distractor":
            evidence["refund"] = _refund_proof(before, after)
            expected = original
        elif (variant.config.get("financial_case") or {}).get("kind") == "invoice_cutoff":
            expected = _invoice_change(before, after, variant, original, evidence)
        else:
            expected = _check_financial_change(before, after, variant, original, evidence)
        report["expected"] = expected
        _require(answers_match(expected, actual), "Independent findings did not match the expected relation")
        return {**report, "status": "passed", "reason": None}
    except (KeyError, ValueError) as error:
        return {**report, "status": "failed", "reason": str(error)}


def _check_financial_change(before, after, variant, original, evidence):
    findings = {item["type"]: item for item in original}
    interacting = variant.kind in {"fixed", "interaction"}
    _require(len(original) == len(findings) and findings.keys() == ({ALLOCATION, FX} if interacting else {ALLOCATION}),
             "Independent baseline lacks the exact declared focal violations")
    allocation = findings[ALLOCATION]
    base, control = _allocation(before, allocation), _allocation(after, allocation)
    evidence.update(before_allocation=base, after_allocation=control)
    _require(base["balance_minor"] == allocation["claimed_amount_minor"] and base["currency"] == allocation["currency"],
             "Independent baseline allocation did not match the public credit/debit remainder")
    if variant.kind == "amount":
        case = variant.config["financial_case"]
        applied_bps = case.get("applied_bps", 6000)
        from_postings, to_postings = base["settlements"], control["settlements"]
        _require(from_postings.keys() == to_postings.keys()
                 and all(from_postings[key][field] == to_postings[key][field]
                         for key in from_postings for field in ("timestamp", "journal_id"))
                 and sum(row["amount_minor"] for row in from_postings.values()) == base["nominal_minor"]
                 and sum(row["amount_minor"] for row in to_postings.values()) == control["nominal_minor"],
                 "Nominal change moved or replaced a settlement instead of adjusting the same obligations")
        _require(base["currency"] == control["currency"] and base["deadline"] == control["deadline"]
                 and control["nominal_minor"] == case["amount_minor"] != base["nominal_minor"]
                 and control["debits_before_minor"] == control["nominal_minor"] * applied_bps // 10_000
                 and base["debits_before_minor"] == base["nominal_minor"] * applied_bps // 10_000
                 and control["balance_minor"] > 0 and base["balance_minor"] != control["balance_minor"],
                 "Nominal change did not preserve the defined applied share or change the outstanding amount")
        return [_finding(allocation, control["balance_minor"])]
    if variant.kind == "period_crossing":
        evidence["crossing"] = _crossing(base, control)
        return []
    _require(interacting, "Unsupported financial relationship")
    fx = findings[FX]
    payment_id = base["payment_id"]
    _require(fx["affected_ids"] == [f"payment/{payment_id}"], "The two baseline violations do not share one payment")
    old_fx, new_fx = _fx(before, payment_id), _fx(after, payment_id)
    evidence.update(before_fx=old_fx, after_fx=new_fx, shared_payment_id=payment_id)
    _require(old_fx["difference_minor"] == fx["claimed_amount_minor"] and old_fx["currency"] == fx["currency"]
             and all(old_fx[k] == new_fx[k] for k in ("currency", "quote_date", "rate", "expected_minor")),
             "Public FX arithmetic changed the common valuation basis or disagreed with the baseline")
    if variant.kind == "fixed":
        _require(base == control and new_fx["difference_minor"] == 0,
                 "Fixing FX changed allocation or left a reporting discrepancy")
        return [allocation]
    evidence["crossing"] = _crossing(base, control)
    _require(old_fx == new_fx, "Fixing allocation changed the shared payment's original FX discrepancy")
    return [fx]
