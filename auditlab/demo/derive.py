"""Console arithmetic over public records: where does an amount come from?

Each derivation lists its operands with the evidence key and file line they were
read from, the rule applied and the result in minor units. Only public data is
used. This is a display aid for the judge and a stand-in until the independent
SQL verifier (auditlab.verifier) is merged; it is always labelled as such.
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from .snapshot import Record, Snapshot


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts))


def local_date(ts: str, tz_name: str | None) -> str:
    moment = _parse(ts)
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return moment.astimezone(ZoneInfo(tz_name)).date().isoformat()
        except Exception:  # tz database missing: fall back to the published offset
            pass
    return moment.date().isoformat()


def _operand(record: Record, field: str, sign: int, label: str) -> dict[str, object]:
    value = int(record.fields.get(field) or 0)
    return {"key": record.key, "file": record.file, "line": record.line, "field": field,
            "value_minor": value, "sign": sign, "label": label}


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
        credit, debit = int(f.get("credit_minor") or 0), int(f.get("debit_minor") or 0)
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


def fx_valuation(snapshot: Snapshot, payment: Record) -> dict[str, object] | None:
    f = payment.fields
    base = str(snapshot.manifest.get("base_currency") or "KZT")
    src = str(f.get("currency"))
    if src == base:
        return None
    exps = snapshot.manifest.get("currency_exponents") or {}
    src_exp, dst_exp = int(exps.get(src, 2)), int(exps.get(base, 2))
    date = local_date(str(f.get("timestamp")), snapshot.manifest.get("timezone"))
    rate_rec = snapshot.get(f"fx/{date}/{src}/{base}")
    cash = next((r for r in snapshot.by_table("ledger")
                 if r.fields.get("payment_id") == f.get("payment_id") and r.fields.get("account") == "CASH"
                 and r.fields.get("currency") == base), None)
    amount = int(f.get("amount_minor") or 0)
    out: dict[str, object] = {
        "type": "fx_conversion",
        "title": f"Оценка платежа {f.get('payment_id')} в {base} по курсу даты платежа",
        "subject": payment.key,
        "currency": base,
        "affected_ids": [f"payment/{f.get('payment_id')}"],
        "rule": {"key": "policy/fx_date", "text": snapshot.policy.get("fx_date"),
                 "local_date": date, "timezone": snapshot.manifest.get("timezone"),
                 "rounding_key": "policy/rounding", "rounding": snapshot.policy.get("rounding", "ROUND_HALF_UP")},
    }
    if rate_rec is None or cash is None:
        out.update({"operands": [], "formula": "нет курса или проводки", "result_minor": None,
                    "evidence": [payment.key]})
        return out
    rate = Decimal(str(rate_rec.fields["rate"]))
    expected = int((Decimal(amount) / (Decimal(10) ** src_exp) * rate * (Decimal(10) ** dst_exp))
                   .quantize(Decimal(1), rounding=ROUND_HALF_UP))
    recorded = int(cash.fields.get("debit_minor") or 0)
    out.update({
        "operands": [
            {"key": payment.key, "file": payment.file, "line": payment.line, "field": "amount_minor",
             "value_minor": amount, "sign": 1, "label": f"сумма платежа, {src} (10^{src_exp})"},
            {"key": rate_rec.key, "file": rate_rec.file, "line": rate_rec.line, "field": "rate",
             "value": str(rate), "sign": 1, "label": f"курс {src}/{base} на {date}"},
            {"key": cash.key, "file": cash.file, "line": cash.line, "field": "debit_minor",
             "value_minor": recorded, "sign": -1, "label": f"записано в книге, {base}"},
        ],
        "expected_minor": expected,
        "recorded_minor": recorded,
        "formula": f"{amount}/10^{src_exp} × {rate} × 10^{dst_exp} = {expected}; {expected} − {recorded} = {expected - recorded}",
        "result_minor": expected - recorded,
        "evidence": [payment.key, cash.key, rate_rec.key, "policy/fx_date"],
    })
    return out


def derive_for(snapshot: Snapshot, finding: dict[str, object]) -> dict[str, object] | None:
    """Derivation matching a submitted finding's type and affected payment."""
    payment = None
    for affected in finding.get("affected_ids") or []:
        if str(affected).startswith("payment/"):
            payment = snapshot.resolve_affected(str(affected))
            break
    if payment is None:
        return None
    if finding.get("type") == "payment_allocation":
        return allocation(snapshot, payment)
    if finding.get("type") == "fx_conversion":
        return fx_valuation(snapshot, payment)
    return None


def scan(snapshot: Snapshot) -> list[dict[str, object]]:
    """All non-zero discrepancies the console arithmetic sees in public data."""
    found = []
    for payment in snapshot.by_table("payments"):
        for calc in (allocation(snapshot, payment), fx_valuation(snapshot, payment)):
            if calc and calc.get("result_minor"):
                found.append(calc)
    return found


