"""Console arithmetic over public records: where does an amount come from?

Each derivation lists its operands with the evidence key and file line they were
read from, the rule applied and the result in minor units. Only public data is
used. This is a display aid for the judge and a stand-in until the independent
SQL verifier (auditlab.verifier) is merged; it is always labelled as such.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from decimal import ROUND_HALF_UP, Decimal

from .snapshot import Record, Snapshot


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts))


def local_date(ts: str, tz_name: str | None) -> str:
    moment = _parse(ts)
    if not tz_name:
        return moment.date().isoformat()
    fixed = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", tz_name)
    if fixed:
        hours, minutes = int(fixed[2]), int(fixed[3])
        if hours > 23 or minutes > 59:
            raise ValueError("Invalid published timezone offset")
        offset = timedelta(hours=hours, minutes=minutes)
        return moment.astimezone(timezone(-offset if fixed[1] == "-" else offset)).date().isoformat()
    from zoneinfo import ZoneInfo
    # Never silently use the source timestamp's offset as the company timezone.
    return moment.astimezone(ZoneInfo(tz_name)).date().isoformat()


def _operand(record: Record, field: str, sign: int, label: str) -> dict[str, object]:
    value = int(record.fields.get(field) or 0)
    return {"key": record.key, "file": record.file, "line": record.line, "field": field,
            "value_minor": value, "currency": record.fields.get("currency"), "sign": sign, "label": label}


def allocation(snapshot: Snapshot, payment: Record) -> dict[str, object]:
    pid = payment.fields.get("payment_id")
    rule = snapshot.policy.get("allocation") or {}
    boundary_text = (payment.fields.get("settlement_index") or {}).get("before") or \
        (rule.get("deadline") if isinstance(rule, dict) else None) or snapshot.manifest.get("period_end")
    boundary = _parse(str(boundary_text))
    operands, context = [], []
    for record in snapshot.by_table("ledger"):
        f = record.fields
        if f.get("account") != "UNAPPLIED" or f.get("payment_id") != pid:
            continue
        credit = int(f.get("credit_minor") or 0)
        field, sign = ("credit_minor", 1) if credit else ("debit_minor", -1)
        label = "поступление на нераспределённый остаток" if sign > 0 else "зачёт с нераспределённого остатка"
        item = _operand(record, field, sign, label)
        item["timestamp"] = f.get("timestamp")
        if _parse(str(f.get("timestamp"))) < boundary:
            operands.append(item)
        else:
            item["label"] = "после границы периода — не входит в остаток"
            context.append(item)
    index_ids = list((payment.fields.get("settlement_index") or {}).get("unapplied_row_ids") or [])
    grouped_ids = [str(o["key"]).split("/", 1)[1] for o in operands]
    result = sum(int(o["value_minor"]) * int(o["sign"]) for o in operands)
    currency = payment.fields.get("currency")
    formula = " ".join(("+ " if o["sign"] > 0 else "− ") + str(o["value_minor"]) for o in operands).lstrip("+ ")
    return {
        "type": "payment_allocation",
        "title": f"Нераспределённый остаток платежа {pid} на границе периода",
        "subject": payment.key,
        "operands": operands,
        "context": context,
        "rule": {"key": "policy/allocation", "text": rule.get("rule") if isinstance(rule, dict) else rule,
                 "boundary": str(boundary_text), "boundary_exclusive": True},
        "formula": f"{formula or '0'} = {result}",
        "result_minor": result,
        "currency": currency,
        "affected_ids": sorted([f"payment/{pid}"] + [f"invoice/{i}" for i in payment.fields.get("invoice_refs") or []]),
        "index_check": {"published": index_ids, "grouped": grouped_ids,
                        "complete": sorted(index_ids) == sorted(grouped_ids)},
        "evidence": [payment.key] + [str(o["key"]) for o in operands] + ["policy/allocation"],
    }


def _valuation_row(snapshot: Snapshot, source: Record) -> Record | None:
    """FX_VALUATION memorandum row for a source document (policy/fx_date)."""
    base = snapshot.manifest.get("base_currency")
    for row in snapshot.by_table("ledger"):
        f = row.fields
        if f.get("account") != "FX_VALUATION" or f.get("currency") != base:
            continue
        if source.table == "payments" and f.get("payment_id") == source.fields.get("payment_id"):
            return row
        if source.table == "invoices" and f.get("payment_id") is None and f.get("invoice_id") == source.fields.get("invoice_id"):
            return row
    return None


def fx_valuation(snapshot: Snapshot, source: Record) -> dict[str, object] | None:
    """Reporting valuation of a foreign payment or invoice against its FX_VALUATION posting."""
    f = source.fields
    base = str(snapshot.manifest.get("base_currency") or "KZT")
    src = str(f.get("currency"))
    if src == base:
        return None
    valuation = _valuation_row(snapshot, source)
    if valuation is None:
        return None
    is_payment = source.table == "payments"
    amount_field, time_field = ("amount_minor", "timestamp") if is_payment else ("total_minor", "issued_at")
    local_id = f.get("payment_id") if is_payment else f.get("invoice_id")
    affected = f"{'payment' if is_payment else 'invoice'}/{local_id}"
    exps = snapshot.manifest.get("currency_exponents") or {}
    src_exp, dst_exp = int(exps.get(src, 2)), int(exps.get(base, 2))
    date = local_date(str(f.get(time_field)), snapshot.manifest.get("timezone"))
    rate_rec = snapshot.get(f"fx/{date}/{src}/{base}")
    amount = abs(int(f.get(amount_field) or 0))
    noun = "платежа" if is_payment else "счёта"
    out: dict[str, object] = {
        "type": "fx_conversion",
        "title": f"Оценка {noun} {local_id} в {base} по курсу местной даты события",
        "subject": source.key,
        "currency": base,
        "affected_ids": [affected],
        "rule": {"key": "policy/fx_date", "text": snapshot.policy.get("fx_date"),
                 "local_date": date, "timezone": snapshot.manifest.get("timezone"),
                 "rounding_key": "policy/rounding", "rounding": snapshot.policy.get("rounding", "ROUND_HALF_UP")},
    }
    recorded = int(valuation.fields.get("debit_minor") or 0)
    if rate_rec is None:
        out.update({"operands": [], "formula": f"нет опубликованного курса {src}/{base} на {date}",
                    "result_minor": None, "evidence": [source.key, valuation.key, "policy/fx_date"]})
        return out
    rate = Decimal(str(rate_rec.fields["rate"]))
    expected = int((Decimal(amount) / (Decimal(10) ** src_exp) * rate * (Decimal(10) ** dst_exp))
                   .quantize(Decimal(1), rounding=ROUND_HALF_UP))
    out.update({
        "operands": [
            {"key": source.key, "file": source.file, "line": source.line, "field": amount_field,
             "value_minor": amount, "currency": src, "sign": 1, "label": f"сумма {noun}, {src} (10^{src_exp})"},
            {"key": rate_rec.key, "file": rate_rec.file, "line": rate_rec.line, "field": "rate",
             "value": str(rate), "sign": 1, "label": f"курс {src}/{base} на местную дату {date}"},
            {"key": valuation.key, "file": valuation.file, "line": valuation.line, "field": "debit_minor",
             "value_minor": recorded, "currency": base, "sign": -1, "label": f"записанная оценка FX_VALUATION, {base}"},
        ],
        "expected_minor": expected,
        "recorded_minor": recorded,
        "formula": f"{amount}/10^{src_exp} × {rate} × 10^{dst_exp} = {expected}; |{expected} − {recorded}| = {abs(expected - recorded)}",
        "result_minor": abs(expected - recorded),
        "evidence": [source.key, rate_rec.key, valuation.key, "policy/fx_date"],
    })
    return out


def derive_for(snapshot: Snapshot, finding: dict[str, object]) -> dict[str, object] | None:
    """Derivation matching a submitted finding's type and affected payment or invoice."""
    affected = [str(a) for a in finding.get("affected_ids") or []]
    payment = next((snapshot.resolve_affected(a) for a in affected if a.startswith("payment/")), None)
    if finding.get("type") == "payment_allocation":
        return allocation(snapshot, payment) if payment else None
    if finding.get("type") == "fx_conversion":
        source = payment or next((snapshot.resolve_affected(a) for a in affected if a.startswith("invoice/")), None)
        return fx_valuation(snapshot, source) if source else None
    return None


def scan(snapshot: Snapshot) -> list[dict[str, object]]:
    """All non-zero discrepancies the console arithmetic sees in public data."""
    found = []
    for payment in snapshot.by_table("payments"):
        calc = allocation(snapshot, payment)
        if payment.fields.get("settlement_index") and calc.get("result_minor"):
            found.append(calc)
    for source in snapshot.by_table("payments") + snapshot.by_table("invoices"):
        calc = fx_valuation(snapshot, source)
        if calc and calc.get("result_minor"):
            found.append(calc)
    return found
