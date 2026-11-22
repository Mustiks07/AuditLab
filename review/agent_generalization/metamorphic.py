"""Metamorphic relations MR1-MR5 (protocol independent-assurance-2.0, section 3).

Every transformation here is the reviewer's own code on the public files (plus the matching truth,
so the official scorer can be checked on the transformed snapshot). The generator is used only to
create the starting episodes. For each relation two things are checked:

* the independent solver (adapter observations only) gives the answer the relation predicts;
* the official scorer accepts the predicted answer on the transformed truth and rejects the stale
  answer wherever the financial meaning changed (MR3, MR4) or the IDs changed (MR1).
"""
from __future__ import annotations

import csv, copy, hashlib, io, json, random, shutil
from datetime import datetime, timedelta
from pathlib import Path

from review.agent_generalization.solver import solve, NotSupported

ID_FIELDS = ("row_id", "journal_id", "invoice_id", "payment_id", "entity_id", "doc_id", "canonical_key")


# ------------------------------------------------------------------ book i/o
def load(public: Path) -> dict:
    def table(name):
        text = (public / name).read_text(encoding="utf-8")
        reader = csv.DictReader(io.StringIO(text))
        return reader.fieldnames, list(reader)
    lh, ledger = table("ledger.csv")
    fh, fx = table("fx.csv")
    docs = lambda n: [json.loads(l) for l in (public / n).read_text(encoding="utf-8").splitlines() if l.strip()]
    return {"manifest": json.loads((public / "manifest.json").read_text(encoding="utf-8")),
            "policy": json.loads((public / "policy.json").read_text(encoding="utf-8")),
            "ledger": ledger, "ledger_header": lh, "fx": fx, "fx_header": fh,
            "invoices": docs("invoices.jsonl"), "payments": docs("payments.jsonl")}


def save(book: dict, public: Path) -> None:
    public.mkdir(parents=True, exist_ok=False)
    m = dict(book["manifest"], ledger_rows=len(book["ledger"]), document_count=len(book["invoices"]) + len(book["payments"]))
    (public / "manifest.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    (public / "policy.json").write_text(json.dumps(book["policy"], indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    for name in ("invoices", "payments"):
        (public / f"{name}.jsonl").write_text("".join(json.dumps(d, sort_keys=True, separators=(",", ":")) + "\n" for d in book[name]),
                                              encoding="utf-8", newline="\n")
    for name in ("ledger", "fx"):
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=book[f"{name}_header"], lineterminator="\n")
        w.writeheader(); w.writerows(book[name])
        (public / f"{name}.csv").write_text(out.getvalue(), encoding="utf-8", newline="\n")


def write_episode(book: dict, truth: dict, out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    save(book, out / "public")
    (out / "private").mkdir()
    truth = dict(truth, episode_id=book["manifest"]["episode_id"])
    (out / "private" / "truth.json").write_text(json.dumps(truth, indent=1, sort_keys=True), encoding="utf-8", newline="\n")
    return out


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _new_id(prefix_from: str, salt: str) -> str:
    return prefix_from[0] + hashlib.sha256(salt.encode()).hexdigest()[: len(prefix_from) - 1].upper()


def _deadline(book) -> datetime:
    return _t(book["policy"]["allocation"]["deadline"])


# ------------------------------------------------------------------ MR1 rename and reorder
def rename(book: dict, truth: dict, seed: int):
    rng = random.Random(seed)
    values = set()
    for r in book["ledger"]:
        values |= {r[k] for k in ID_FIELDS if r.get(k) not in (None, "", "null")}
    for d in book["invoices"] + book["payments"]:
        values |= {d[k] for k in ID_FIELDS if isinstance(d.get(k), str)}
    m = {v: _new_id(v, f"mr1:{seed}:{v}") for v in sorted(values)}
    new = copy.deepcopy(book)
    for r in new["ledger"]:
        for k in ID_FIELDS:
            if r.get(k) in m: r[k] = m[r[k]]
    for d in new["invoices"] + new["payments"]:
        for k in ID_FIELDS:
            if d.get(k) in m: d[k] = m[d[k]]
        if "invoice_refs" in d: d["invoice_refs"] = [m[i] for i in d["invoice_refs"]]
        if "settlement_index" in d:
            idx = dict(d["settlement_index"]); ids = [m[i] for i in idx["unapplied_row_ids"]]; rng.shuffle(ids)
            idx["unapplied_row_ids"] = ids; d["settlement_index"] = idx
        if "allocation_instructions" in d:
            d["allocation_instructions"] = [{**a, "invoice_id": m[a["invoice_id"]]} for a in d["allocation_instructions"]]
    for k in ("ledger", "invoices", "payments"):
        rng.shuffle(new[k])
    new["manifest"]["episode_id"] = _new_id(book["manifest"]["episode_id"], f"mr1:{seed}:episode")

    def ref(key: str) -> str:
        kind, _, rest = key.partition("/")
        return f"{kind}/{m.get(rest, rest)}" if kind in ("ledger", "invoices", "payments", "invoice", "payment") else key
    new_truth = {"episode_id": new["manifest"]["episode_id"],
                 "issues": [{**i, "affected_ids": sorted(map(ref, i["affected_ids"])),
                             "evidence_sets": [sorted(map(ref, g)) for g in i["evidence_sets"]]} for i in truth["issues"]]}
    return new, new_truth, ref


# ------------------------------------------------------------------ MR2 lawful look-alikes
def _cited(answer: dict) -> set[str]:
    out = set()
    for f in answer["findings"]:
        out |= {a.split("/", 1)[1] for a in f["affected_ids"]}
        out |= {e.split("/", 1)[1] for e in f["evidence_ids"]}
    return out


def _recognition(book, invoice_id):
    js = {}
    for r in book["ledger"]:
        js.setdefault(r["journal_id"], []).append(r)
    for j, rows in js.items():
        if {r["account"] for r in rows} == {"AR", "REVENUE"} and all(r["invoice_id"] == invoice_id and r["payment_id"] in ("null", "") for r in rows):
            return rows
    return None


def _plain_invoice(book, answer, base_only=True):
    """An in-period invoice untouched by any finding, recognised once by a plain AR/REVENUE journal."""
    cited = _cited(answer)
    start, end = _t(book["manifest"]["period_start"]), _t(book["manifest"]["period_end"])
    count = {}
    for d in book["invoices"]:
        count[d["invoice_id"]] = count.get(d["invoice_id"], 0) + 1
    for d in sorted(book["invoices"], key=lambda d: d["doc_id"]):
        if d["invoice_id"] in cited or d["doc_id"] in cited or count[d["invoice_id"]] != 1:
            continue
        if base_only and d["currency"] != book["manifest"]["base_currency"]:
            continue
        rows = _recognition(book, d["invoice_id"])
        if rows and start <= _t(d["issued_at"]) < end and all(start <= _t(r["timestamp"]) < end for r in rows):
            if any(p for p in book["payments"] if d["invoice_id"] in p.get("invoice_refs", [])):
                continue
            return d, rows
    return None, None


def archive_copy(book, answer, seed):
    """A second document of an already recognised invoice, with no second recognition (lawful)."""
    d, _ = _plain_invoice(book, answer)
    if d is None:
        return None
    new = copy.deepcopy(book)
    new["invoices"].append({**d, "doc_id": _new_id(d["doc_id"], f"mr2a:{seed}")})
    return new


def other_supplier_same_amount(book, answer, seed):
    """A new invoice of the same amount to a different customer, recognised once (lawful)."""
    d, rows = _plain_invoice(book, answer)
    if d is None:
        return None
    others = sorted({x["entity_id"] for x in book["invoices"]} - {d["entity_id"]})
    if not others:
        return None
    new = copy.deepcopy(book)
    inv, ent = _new_id(d["invoice_id"], f"mr2b:{seed}:i"), others[0]
    doc = {**d, "doc_id": _new_id(d["doc_id"], f"mr2b:{seed}:d"), "invoice_id": inv, "entity_id": ent}
    if "canonical_key" in doc:
        doc["canonical_key"] = _new_id(d["canonical_key"], f"mr2b:{seed}:k")
    new["invoices"].append(doc)
    j = _new_id(rows[0]["journal_id"], f"mr2b:{seed}:j")
    for k, r in enumerate(rows):
        new["ledger"].append({**r, "row_id": _new_id(r["row_id"], f"mr2b:{seed}:r{k}"), "journal_id": j, "invoice_id": inv, "entity_id": ent})
    return new


# ------------------------------------------------------------------ MR3 / MR4 allocation edits
def _unapplied(book, pid):
    return [r for r in book["ledger"] if r["account"] == "UNAPPLIED" and r["payment_id"] == pid]


def _reindex(book, pid):
    p = next(x for x in book["payments"] if x["payment_id"] == pid)
    before = _t(p["settlement_index"]["before"])
    p["settlement_index"] = {**p["settlement_index"],
                             "unapplied_row_ids": [r["row_id"] for r in sorted(_unapplied(book, pid), key=lambda r: (r["timestamp"], r["row_id"])) if _t(r["timestamp"]) < before]}


def fix_allocation(book, pid, remaining, seed):
    """Apply the remaining designated amount before the deadline.
    A late tranche (after the deadline) is moved before it; otherwise a new application journal is posted."""
    new = copy.deepcopy(book)
    deadline = _deadline(new)
    late = [r for r in _unapplied(new, pid) if _t(r["timestamp"]) >= deadline and int(r["debit_minor"]) > 0]
    if late and sum(int(r["debit_minor"]) for r in late) == remaining:
        journals = {r["journal_id"] for r in late}
        at = (deadline - timedelta(minutes=1)).isoformat()
        for r in new["ledger"]:
            if r["journal_id"] in journals:
                r["timestamp"] = at
    else:
        pre = [r for r in _unapplied(new, pid) if _t(r["timestamp"]) < deadline]
        receipt = next(r for r in pre if int(r["credit_minor"]) > 0)
        last = max(_t(r["timestamp"]) for r in pre)
        at = min(last + timedelta(minutes=1), deadline - timedelta(minutes=1)).isoformat()
        j = _new_id(receipt["journal_id"], f"mr3:{seed}:j")
        base = {k: receipt[k] for k in ("currency", "entity_id", "invoice_id", "payment_id")}
        new["ledger"].append({**base, "row_id": _new_id(receipt["row_id"], f"mr3:{seed}:a"), "timestamp": at, "account": "AR",
                              "debit_minor": "0", "credit_minor": str(remaining), "journal_id": j})
        new["ledger"].append({**base, "row_id": _new_id(receipt["row_id"], f"mr3:{seed}:u"), "timestamp": at, "account": "UNAPPLIED",
                              "debit_minor": str(remaining), "credit_minor": "0", "journal_id": j})
    _reindex(new, pid)
    return new


def change_allocation(book, pid, delta):
    """Change the designated balance by +delta: shrink the pre-deadline application, or (full omission)
    enlarge the receipt and the payment document. Returns None when neither shape is present."""
    new = copy.deepcopy(book)
    deadline = _deadline(new)
    pre = [r for r in _unapplied(new, pid) if _t(r["timestamp"]) < deadline]
    apps = [r for r in pre if int(r["debit_minor"]) > delta]
    if apps:
        j = sorted(apps, key=lambda r: (r["timestamp"], r["row_id"]))[0]["journal_id"]
        for r in new["ledger"]:
            if r["journal_id"] == j:
                side = "debit_minor" if int(r["debit_minor"]) else "credit_minor"
                r[side] = str(int(r[side]) - delta)
        return new
    if all(int(r["debit_minor"]) == 0 for r in pre):  # nothing applied: grow the receipt
        receipt = next(r for r in pre if int(r["credit_minor"]) > 0)
        for r in new["ledger"]:
            if r["journal_id"] == receipt["journal_id"] and r["account"] != "FX_VALUATION" and r["account"] != "FX_OFFSET":
                side = "debit_minor" if int(r["debit_minor"]) else "credit_minor"
                r[side] = str(int(r[side]) + delta)
        p = next(x for x in new["payments"] if x["payment_id"] == pid)
        p["amount_minor"] += delta
        return new
    return None


def change_fx(book, affected_kind, affected_id, delta):
    """Grow the recorded FX valuation memorandum of one source event by delta (both sides of the journal)."""
    new = copy.deepcopy(book)
    key = "payment_id" if affected_kind == "payment" else "invoice_id"
    journals = {r["journal_id"] for r in new["ledger"] if r["account"] == "FX_VALUATION" and r[key] == affected_id}
    if len(journals) != 1:
        return None
    for r in new["ledger"]:
        if r["journal_id"] in journals:
            side = "debit_minor" if int(r["debit_minor"]) else "credit_minor"
            r[side] = str(int(r[side]) + delta)
    return new


# ------------------------------------------------------------------ MR3 / MR5 revert one mutation from the transition
def revert(book: dict, episode: Path, mutation_index: int):
    """Undo exactly the source_transition changes that belong to one declared mutation.
    Membership: a changed record cites one of the mutation's affected invoice/payment ids, or shares a
    journal with such a record. Returns None when the changes cannot be partitioned unambiguously."""
    priv = episode / "private"
    prov = json.loads((priv / "provenance.json").read_text(encoding="utf-8"))
    tr = json.loads((priv / "source_transition.json").read_text(encoding="utf-8"))
    changes = tr["changes"] if isinstance(tr, dict) else tr
    raw = [{a.split("/", 1)[1] for a in m["affected_ids"]} for m in prov["mutations"]]

    def ids(rec):
        if rec is None: return set()
        return {rec.get("invoice_id"), rec.get("payment_id")} | set(rec.get("invoice_refs", []))
    owner = {}
    for n, c in enumerate(changes):
        hit = [k for k, s in enumerate(raw) if (ids(c["before"]) | ids(c["after"])) & s]
        if len(hit) == 1: owner[n] = hit[0]
        elif len(hit) > 1: return None
    journal_owner = {}
    for n, k in owner.items():
        for rec in (changes[n]["before"], changes[n]["after"]):
            if rec and rec.get("journal_id"): journal_owner.setdefault(rec["journal_id"], set()).add(k)
    for n, c in enumerate(changes):
        if n in owner: continue
        ks = set().union(*[journal_owner.get(rec.get("journal_id"), set()) for rec in (c["before"], c["after"]) if rec]) if any((c["before"], c["after"])) else set()
        if len(ks) != 1: return None
        owner[n] = ks.pop()
    new = copy.deepcopy(book)
    keyf = {"ledger": "row_id", "invoices": "doc_id", "payments": "doc_id"}
    for n, c in enumerate(changes):
        if owner[n] != mutation_index: continue
        table, key = c["table"], keyf[c["table"]]
        rows = new[table]
        pos = next((i for i, r in enumerate(rows) if r[key] == c["id"]), None)
        before = c["before"]
        if table == "ledger" and before is not None:
            before = {k: ("null" if v is None else str(v)) for k, v in before.items()}
        if before is None:
            if pos is not None: rows.pop(pos)
        elif pos is None:
            rows.append(before)
        else:
            rows[pos] = before
    for p in new["payments"]:
        if "settlement_index" in p:
            _reindex(new, p["payment_id"])
    return new


# ------------------------------------------------------------------ running a relation
def solver_answer(adapter, episode: Path) -> dict:
    env = adapter.make_env()
    first, _ = env.reset(0, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
    return solve(lambda a: env.step(a)[0], first)


def sig(f: dict) -> tuple:
    return (f["type"], tuple(sorted(f["affected_ids"])), f["claimed_amount_minor"], f["currency"], tuple(sorted(f["evidence_ids"])))


def official(adapter, episode: Path, findings: list[dict], steps: int = 10) -> dict:
    eid = json.loads((episode / "public" / "manifest.json").read_text(encoding="utf-8"))["episode_id"]
    sub = [{**{k: f[k] for k in ("type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids")}, "explanation": "mr"} for f in findings]
    return adapter.score_episode(str(episode), {"episode_id": eid, "findings": sub}, steps)
