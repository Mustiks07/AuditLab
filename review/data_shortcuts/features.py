"""Record-level features and labels (protocol data-shortcuts-3.0, section 2).

One row per ledger row, invoice document and payment document of a public snapshot. Labels come
from private/truth.json: `affected` (the record's invoice_id/payment_id is an affected entity) and
`evidence` (the record's key is in an evidence set). Features are split by their a priori class.
The financial-rule columns come from the reviewer's independent calculator on public/ only.
"""
from __future__ import annotations

import csv, io, json
from collections import Counter, defaultdict
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from review.genesis_acceptance.independent_calc import audit

FIXED_RATIOS = {Fraction(2, 5), Fraction(3, 5), Fraction(1, 2), Fraction(5, 8), Fraction(3, 8)}
ARTIFICIAL = ["id_prefix1", "id_prefix2", "id_len", "id_rank", "file_pos", "file_from_end", "round_major", "ratio_linked",
              "ratio_is_fixed", "minute", "second", "hour", "day", "amount_repeat", "co_time", "journal_size", "entity_docs",
              "entity_rows", "index_len", "refs_len", "account", "equal_tranches", "early_applications"]
CONTEXT = ["minutes_to_boundary", "doc_key_repeat", "payment_without_application"]
FINANCIAL = ["calc_affected", "calc_evidence"]
CATEGORICAL = {"id_prefix1", "id_prefix2", "account"}


def _trailing_zeros(n: int) -> int:
    if n == 0:
        return 0
    k = 0
    while n % 10 == 0:
        n //= 10; k += 1
    return k


def records(episode: Path) -> list[dict]:
    pub = episode / "public"
    m = json.loads((pub / "manifest.json").read_text(encoding="utf-8"))
    truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
    ledger = list(csv.DictReader(io.StringIO((pub / "ledger.csv").read_text(encoding="utf-8"))))
    docs = {t: [json.loads(l) for l in (pub / f"{t}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] for t in ("invoices", "payments")}
    exps = m["currency_exponents"]
    start, end = datetime.fromisoformat(m["period_start"]), datetime.fromisoformat(m["period_end"])
    affected = {a.split("/", 1)[1] for i in truth["issues"] for a in i["affected_ids"]}
    evidence = {e for i in truth["issues"] for g in i["evidence_sets"] for e in g}
    try:
        mine = audit(pub)
    except (KeyError, ValueError, IndexError):
        mine = {"findings": []}
    calc_aff = {a.split("/", 1)[1] for f in mine["findings"] for a in f["affected_ids"]}
    calc_ev = {e for f in mine["findings"] for e in f["evidence_ids"]}
    nul = lambda v: None if v in (None, "", "null") else v
    pay_by = {p["payment_id"]: p for p in docs["payments"]}
    inv_total = {}
    for d in docs["invoices"]:
        inv_total.setdefault(d["invoice_id"], d["total_minor"])
    journals = Counter(r["journal_id"] for r in ledger)
    ent_rows = Counter(nul(r["entity_id"]) for r in ledger)
    ent_docs = Counter(d["entity_id"] for d in docs["invoices"]) + Counter(
        next((r["entity_id"] for r in ledger if r["payment_id"] == p["payment_id"]), None) for p in docs["payments"])
    amount_count = Counter(max(int(r["debit_minor"]), int(r["credit_minor"])) for r in ledger)
    ts_journals = defaultdict(set)
    for r in ledger:
        ts_journals[r["timestamp"]].add(r["journal_id"])
    deadline = datetime.fromisoformat(json.loads((pub / "policy.json").read_text(encoding="utf-8"))["allocation"]["deadline"])
    early = defaultdict(list)
    for r in ledger:
        if r["account"] == "UNAPPLIED" and int(r["debit_minor"]) > 0 and nul(r["payment_id"]) and datetime.fromisoformat(r["timestamp"]) < deadline:
            early[r["payment_id"]].append(int(r["debit_minor"]))
    applied = {p: any(r["account"] == "UNAPPLIED" and int(r["debit_minor"]) > 0 and r["payment_id"] == p for r in ledger) for p in pay_by}
    key_repeat = Counter((d["entity_id"], d.get("canonical_key") or d["invoice_id"]) for d in docs["invoices"])

    def common(key, keys_sorted, idx, n, t, amount, currency):
        ts = datetime.fromisoformat(t)
        major = amount // 10 ** exps.get(currency, 2) if amount % 10 ** exps.get(currency, 2) == 0 else 0
        return {"id_prefix1": key[:1], "id_prefix2": key[:2], "id_len": len(key), "id_rank": keys_sorted[key] / max(1, n - 1),
                "file_pos": idx / max(1, n - 1), "file_from_end": n - 1 - idx, "round_major": _trailing_zeros(major) if major else 0,
                "minute": ts.minute, "second": ts.second, "hour": ts.hour, "day": ts.day,
                "minutes_to_boundary": min(abs((ts - start).total_seconds()), abs((ts - end).total_seconds())) / 60}

    out = []
    rank = {k: i for i, k in enumerate(sorted(r["row_id"] for r in ledger))}
    for i, r in enumerate(ledger):
        amount = max(int(r["debit_minor"]), int(r["credit_minor"]))
        pid, iid = nul(r["payment_id"]), nul(r["invoice_id"])
        linked = pay_by[pid]["amount_minor"] if pid in pay_by else inv_total.get(iid)
        ratio = Fraction(amount, linked) if linked else None
        out.append({"table": "ledger", "key": "ledger/" + r["row_id"], **common(r["row_id"], rank, i, len(ledger), r["timestamp"], amount, r["currency"]),
                    "ratio_linked": float(ratio) if ratio is not None else -1.0, "ratio_is_fixed": int(ratio in FIXED_RATIOS),
                    "amount_repeat": amount_count[amount] - 1, "co_time": len(ts_journals[r["timestamp"]]) - 1,
                    "journal_size": journals[r["journal_id"]], "entity_docs": ent_docs.get(nul(r["entity_id"]), 0),
                    "entity_rows": ent_rows[nul(r["entity_id"])], "index_len": -1, "refs_len": -1, "account": r["account"],
                    "equal_tranches": -1, "early_applications": -1,
                    "doc_key_repeat": -1, "payment_without_application": int(pid is not None and not applied.get(pid, True)),
                    "affected": int(pid in affected or iid in affected), "evidence": int("ledger/" + r["row_id"] in evidence),
                    "calc_affected": int(pid in calc_aff or iid in calc_aff), "calc_evidence": int("ledger/" + r["row_id"] in calc_ev)})
    for table in ("invoices", "payments"):
        ds = docs[table]
        rank = {k: i for i, k in enumerate(sorted(d["doc_id"] for d in ds))}
        for i, d in enumerate(ds):
            if table == "invoices":
                amount, t, cur, ent = d["total_minor"], d["issued_at"], d["currency"], d["entity_id"]
                ratio, refs, idx_len, link_ids = None, -1, -1, {d["invoice_id"]}
                rep = key_repeat[(d["entity_id"], d.get("canonical_key") or d["invoice_id"])] - 1
                no_app = -1
            else:
                amount, t, cur = d["amount_minor"], d["timestamp"], d["currency"]
                ent = next((r["entity_id"] for r in ledger if r["payment_id"] == d["payment_id"]), None)
                tot = [inv_total[x] for x in d.get("invoice_refs", []) if x in inv_total]
                ratio = Fraction(amount, sum(tot)) if tot and sum(tot) else None
                refs, idx_len = len(d.get("invoice_refs", [])), len((d.get("settlement_index") or {}).get("unapplied_row_ids", []))
                link_ids = {d["payment_id"], *d.get("invoice_refs", [])}
                rep, no_app = -1, int(not applied.get(d["payment_id"], True))
            key = f"{table}/{d['doc_id']}"
            out.append({"table": table, "key": key, **common(d["doc_id"], rank, i, len(ds), t, amount, cur),
                        "ratio_linked": float(ratio) if ratio is not None else -1.0, "ratio_is_fixed": int(ratio in FIXED_RATIOS),
                        "amount_repeat": sum(1 for x in ds if (x.get("total_minor") or x.get("amount_minor")) == amount) - 1, "co_time": -1,
                        "journal_size": -1, "entity_docs": ent_docs.get(ent, 0), "entity_rows": ent_rows.get(ent, 0),
                        "index_len": idx_len, "refs_len": refs, "account": "-",
                        "equal_tranches": (int(len(early.get(d["payment_id"], [])) >= 2 and max(early[d["payment_id"]]) - min(early[d["payment_id"]]) <= 1)
                                           if table == "payments" else -1),
                        "early_applications": len(early.get(d["payment_id"], [])) if table == "payments" else -1,
                        # financial fact used only to adjudicate rules: the designated payment's whole amount is still unapplied
                        "full_unapplied": (int(bool(d.get("invoice_refs")) and d["amount_minor"] > 0 and sum(
                            int(x["credit_minor"]) - int(x["debit_minor"]) for x in ledger if x["payment_id"] == d["payment_id"]
                            and x["account"] == "UNAPPLIED" and datetime.fromisoformat(x["timestamp"]) < deadline) == d["amount_minor"])
                                           if table == "payments" else -1), "doc_key_repeat": rep, "payment_without_application": no_app,
                        "affected": int(bool(link_ids & affected) if table == "invoices" else d["payment_id"] in affected),
                        "evidence": int(key in evidence),
                        "calc_affected": int(bool(link_ids & calc_aff) if table == "invoices" else d["payment_id"] in calc_aff),
                        "calc_evidence": int(key in calc_ev)})
    for r in out:
        r["company_has_issue"] = int(bool(truth["issues"]))
    return out
