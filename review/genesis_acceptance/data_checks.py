"""D01-D20 (data) and P01-P20 (pairs), with the reviewer's own calculation frozen before truth.

Episodes are generated through the team's documented generator config (profile 1.1). The
independent answer (independent_calc.audit, reviewer code) is written with its SHA-256 before any
private file is read. Pairs use the reviewer's own transformer, not auditlab.release.transforms.
Execution goes through AuditLab's internal runner as a local analog; the jury adapter transport is
reported separately (not_checked while judge_adapter.py is absent).
"""
from __future__ import annotations

import copy, csv, hashlib, io, json, random, re, shutil
from datetime import datetime
from pathlib import Path

from .independent_calc import audit

Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}
MED = {"ledger_rows_target": 400, "documents_target": 65}


def cfg(**o):
    c = {"config_version": "1.1", "company_size": dict(MED), "base_currency": "KZT", "currencies": ["KZT", "USD"],
         "timezone": "Asia/Almaty", "target_period": "2026-11", "issue_counts": dict(Z), "interaction": None, "clean_distractors": []}
    c.update(o)
    return c


# Published meaning of each scenario -> the team's documented settings. Seeds 9101-9120 are new.
SCENARIOS = {
    "D01": (9101, cfg(), "clean: ordinary reconciliation"),
    "D02": (9102, cfg(clean_distractors=["split_payment"]), "clean: correctly split payment"),
    "D03": (9103, cfg(clean_distractors=["refund"]), "clean: correct refund"),
    "D04": (9104, cfg(clean_distractors=["equal_amount_different_supplier", "archive_copy"]), "clean: legitimate repeated amounts"),
    "D05": (9105, cfg(settlement_timing="before", currencies=["KZT", "USD", "JPY"]), "clean: currency/period boundary per policy"),
    "D06": (9106, cfg(issue_counts={**Z, "duplicate_invoice": 1}, clean_distractors=["equal_amount_different_supplier"]), "duplicate invoice"),
    "D07": (9107, cfg(issue_counts={**Z, "duplicate_invoice": 1}, base_currency="JPY", currencies=["JPY", "USD"], clean_distractors=["archive_copy"]), "duplicate invoice"),
    "D08": (9108, cfg(issue_counts={**Z, "duplicate_invoice": 1}, company_size={"ledger_rows_target": 2000, "documents_target": 200},
                      clean_distractors=["equal_amount_different_supplier", "archive_copy", "refund", "split_payment"]), "duplicate invoice (max size)"),
    "D09": (9109, cfg(issue_counts={**Z, "payment_allocation": 1}), "allocation: partial (generator applies 3/5)"),
    "D10": (9110, cfg(issue_counts={**Z, "payment_allocation": 1}, settlement_timing="after"), "allocation: remainder settled after the boundary"),
    "D11": (9111, cfg(issue_counts={**Z, "payment_allocation": 1}, settlement_timing="at"), "allocation: remainder settled exactly at the boundary"),
    "D12": (9112, cfg(issue_counts={**Z, "fx_conversion": 1}), "FX date/rate"),
    "D13": (9113, cfg(issue_counts={**Z, "fx_conversion": 1}, base_currency="KWD", currencies=["KWD", "JPY"]), "FX date/rate (3/0 decimals)"),
    "D14": (9114, cfg(issue_counts={**Z, "period_cutoff": 1}), "period cutoff"),
    "D15": (9115, cfg(issue_counts={**Z, "period_cutoff": 1}, base_currency="EUR", currencies=["EUR", "USD"], timezone="Europe/Berlin", target_period="2026-10"), "period cutoff (DST zone)"),
    "D16": (9116, cfg(issue_counts={**Z, "payment_allocation": 1, "fx_conversion": 1}, interaction="allocation_fx_same_payment"), "interaction A"),
    "D17": (9117, cfg(issue_counts={**Z, "payment_allocation": 1, "fx_conversion": 1}, interaction="allocation_fx_same_payment", base_currency="JPY", currencies=["JPY", "USD", "KWD"]), "interaction A (JPY base)"),
    "D18": (9118, cfg(issue_counts={**Z, "payment_allocation": 2, "fx_conversion": 2}, interaction="allocation_fx_same_payment", timezone="America/New_York", base_currency="USD", currencies=["USD", "EUR"], target_period="2026-03"), "interaction A (NY, more issues)"),
    "D19": (9119, cfg(issue_counts={**Z, "period_cutoff": 1, "fx_conversion": 1}, interaction="cutoff_fx_same_invoice"), "interaction B"),
    "D20": (9120, cfg(issue_counts={**Z, "period_cutoff": 1, "fx_conversion": 1}, interaction="cutoff_fx_same_invoice", base_currency="KWD", currencies=["KWD", "USD"], timezone="Europe/Berlin", target_period="2026-03"), "interaction B (KWD, Berlin spring)"),
}
PAIR_KIND = {**{f"P{n:02d}": "permute" for n in range(1, 6)}, **{f"P{n:02d}": "rename" for n in range(6, 11)}, **{f"P{n:02d}": "both" for n in range(11, 21)}}


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def phase_answers(generate, work: Path) -> dict:
    """Generate, seal private/ unread, compute the independent answer, write it with its SHA-256."""
    answers = {}
    for did, (seed, config, meaning) in SCENARIOS.items():
        ep = work / "episodes" / did
        if not ep.exists():
            generate(seed, config, ep)
        r = audit(ep / "public")
        answers[did] = {"seed": seed, "config": config, "meaning": meaning, "episode_id": r["episode_id"],
                        "findings": r["findings"], "notes": r["notes"], "arithmetic": r["arithmetic"]}
    path = work / "answers_before_truth.json"
    path.write_text(json.dumps(answers, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return {"path": str(path), "sha256": sha(path), "written_utc": datetime.utcnow().isoformat() + "Z"}


# ------------------------------------------------------------------ data conditions
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$")
RATE = re.compile(r"^\d+(\.\d+)?$")
K = lambda f, a: (f["type"], tuple(sorted(f["affected_ids"])), f[a], f["currency"])


def _formats(public: Path) -> list[str]:
    problems = []
    for r in csv.DictReader((public / "ledger.csv").open(encoding="utf-8")):
        if not TS.match(r["timestamp"]): problems.append(f"ledger timestamp {r['timestamp']}")
        if not re.fullmatch(r"-?\d+", r["debit_minor"]) or not re.fullmatch(r"-?\d+", r["credit_minor"]): problems.append("ledger money not integer")
        for k in ("invoice_id", "payment_id", "entity_id", "journal_id"):
            if r[k] == "": problems.append(f"ledger {k} empty instead of null")
    for t, keys in (("invoices", ("issued_at", "due_at")), ("payments", ("timestamp",))):
        for line in (public / f"{t}.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            for k in keys:
                if not TS.match(d[k]): problems.append(f"{t} {k} {d[k]}")
            for k in ("total_minor", "amount_minor"):
                if k in d and type(d[k]) is not int: problems.append(f"{t} {k} not int")
    for r in csv.DictReader((public / "fx.csv").open(encoding="utf-8")):
        if not RATE.match(r["rate"]): problems.append(f"fx rate {r['rate']}")
    return problems[:10]


def _transition_ok(priv: Path, public: Path) -> dict:
    """Own replay: every difference between private/source_book and public/ is exactly one transition entry."""
    src = priv / "source_book"
    if not src.is_dir():
        return {"ok": False, "reason": "no source_book"}

    def table(folder, name):
        fname, key = {"ledger": ("ledger.csv", "row_id"), "invoices": ("invoices.jsonl", "doc_id"), "payments": ("payments.jsonl", "doc_id")}[name]
        if fname.endswith(".csv"):
            rows = [{k: (int(v) if k in ("debit_minor", "credit_minor") else (None if v == "null" else v)) for k, v in r.items()}
                    for r in csv.DictReader((folder / fname).open(encoding="utf-8"))]
        else:
            rows = [json.loads(l) for l in (folder / fname).read_text(encoding="utf-8").splitlines() if l.strip()]
        return {r[key]: r for r in rows}
    norm = lambda r: None if r is None else {k: (None if v == "null" else v) for k, v in r.items()}
    trans = json.loads((priv / "source_transition.json").read_text(encoding="utf-8"))
    entries = trans.get("changes", []) if isinstance(trans, dict) else trans
    expected = {(e["table"], e["id"]): (norm(e["before"]), norm(e["after"])) for e in entries}
    actual = {}
    for name in ("ledger", "invoices", "payments"):
        a, b = table(src, name), table(public, name)
        for i in set(a) | set(b):
            if norm(a.get(i)) != norm(b.get(i)):
                actual[(name, i)] = (norm(a.get(i)), norm(b.get(i)))
    fx_same = sorted((src / "fx.csv").read_text(encoding="utf-8").splitlines()) == sorted((public / "fx.csv").read_text(encoding="utf-8").splitlines())
    pol_same = json.loads((src / "policy.json").read_text(encoding="utf-8")) == json.loads((public / "policy.json").read_text(encoding="utf-8"))
    ok = actual.keys() == expected.keys() and all(expected[k] == actual[k] for k in actual) and fx_same and pol_same
    return {"ok": ok, "changed": len(actual), "entries": len(expected), "fx_same_lines": fx_same, "policy_same": pol_same}


def phase_compare(work: Path, open_session, verify_report, evaluate) -> list[dict]:
    answers = json.loads((work / "answers_before_truth.json").read_text(encoding="utf-8"))
    rows = []
    for did, a in answers.items():
        ep = work / "episodes" / did
        pub, priv = ep / "public", ep / "private"
        truth = json.loads((priv / "truth.json").read_text(encoding="utf-8"))           # opened only now
        clean = audit(priv / "source_book") if (priv / "source_book").is_dir() else None
        c1 = clean is not None and not clean["findings"] and not clean["notes"]
        tr = _transition_ok(priv, pub)
        mine = {K(f, "claimed_amount_minor"): f for f in a["findings"]}
        theirs = {K(i, "expected_amount_minor"): i for i in truth["issues"]}
        c3 = mine.keys() == theirs.keys()
        c2 = tr["ok"] and c3
        session = open_session(pub)

        def reach(ref):
            kind, _, rest = ref.partition("/")
            act = {"action": "get_document", "args": {"doc_id": rest}} if kind in ("invoices", "payments") else {"action": "get_record", "args": {"table": kind, "id": rest}}
            return session.inspect(act).get("ok") is True
        c4 = all(any(all(reach(r) for r in g) and set(g) <= set(mine[k]["evidence_ids"]) for g in theirs[k]["evidence_sets"])
                 for k in theirs if k in mine)
        fmt = _formats(pub)
        c5 = not fmt
        vr = verify_report(pub)
        c6 = not vr["notes"] and not a["notes"]
        clean_scenario = did in ("D01", "D02", "D03", "D04", "D05")
        if clean_scenario:
            c3 = c3 and not truth["issues"]
        conds = {"1_source_book_consistent": c1, "2_injection_is_declared_change": c2, "3_truth_matches_change": c3,
                 "4_evidence_available": c4, "5_formats": c5, "6_no_contradictions": c6}
        verifier_tp = evaluate({"episode_id": truth["episode_id"], "findings": vr["findings"]}, truth, 0).score
        rows.append({"id": did, "block": "data", "meaning": a["meaning"], "seed": a["seed"], "size": json.loads((pub / "manifest.json").read_text())["ledger_rows"],
                     "truth_issues": len(truth["issues"]), "answer_findings": len(a["findings"]), "conditions": conds,
                     "transition": tr, "format_problems": fmt, "verifier_tp_fp_fn": [verifier_tp["tp"], verifier_tp["fp"], verifier_tp["fn"]],
                     "status": "passed" if all(conds.values()) else "failed",
                     "adapter_transport": "not_checked (generated with auditlab.generator directly; judge_adapter.generate_episode absent)"})
    kinds = {did: [i["type"] for i in json.loads((work / "episodes" / did / "private" / "truth.json").read_text())["issues"]] for did in answers}
    rows.append({"id": "D09-D11-matrix", "block": "data", "status": "failed",
                 "reason": "The definitions need at least one omission (whole designated payment not applied) and one partial case. "
                           "Generator 1.6 profile 1.1 only offers partial allocation (3/5 applied, or the remainder at/after the boundary); "
                           "no documented setting produces a full omission.", "types": {d: kinds[d] for d in ("D09", "D10", "D11")}})
    return rows


# ------------------------------------------------------------------ pairs
ID_SPACES = ("row_id", "journal_id", "invoice_id", "payment_id", "entity_id", "doc_id", "canonical_key")
PREFIX = {"row_id": "ROWX", "journal_id": "JRNX", "invoice_id": "INVX", "payment_id": "PAYX", "entity_id": "ENTX", "doc_id": "DOCX", "canonical_key": "KEYX"}


def transform(src: Path, dst: Path, seed: int, kind: str) -> dict:
    """Reviewer-side copy: separate maps per ID space, null/money/line_items order kept, FX keys untouched."""
    rng = random.Random(seed)
    ledger = list(csv.DictReader(io.StringIO((src / "public" / "ledger.csv").read_text(encoding="utf-8"))))
    header = list(ledger[0].keys()) if ledger else []
    fx_text = (src / "public" / "fx.csv").read_text(encoding="utf-8").splitlines()
    docs = {t: [json.loads(l) for l in (src / "public" / f"{t}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] for t in ("invoices", "payments")}
    seen = {s: [] for s in ID_SPACES}
    add = lambda s, v: v not in (None, "null") and v not in seen[s] and seen[s].append(v)
    for r in ledger:
        for s in ("row_id", "journal_id", "invoice_id", "payment_id", "entity_id"): add(s, r[s])
    for t, rows in docs.items():
        for d in rows:
            for s in ("doc_id", "invoice_id", "payment_id", "entity_id", "canonical_key"): add(s, d.get(s))
            for x in d.get("invoice_refs", []): add("invoice_id", x)
    maps = {s: ({v: f"{PREFIX[s]}{i:05d}" for i, v in enumerate(rng.sample(vals, len(vals)))} if kind in ("rename", "both") else {v: v for v in vals})
            for s, vals in seen.items()}
    m = lambda s, v: v if v in (None, "null") else maps[s][v]
    for r in ledger:
        for s in ("row_id", "journal_id", "invoice_id", "payment_id", "entity_id"): r[s] = m(s, r[s])
    for t, rows in docs.items():
        for d in rows:
            for s in ("doc_id", "invoice_id", "payment_id", "entity_id", "canonical_key"):
                if s in d: d[s] = m(s, d[s])
            if "invoice_refs" in d: d["invoice_refs"] = [m("invoice_id", x) for x in d["invoice_refs"]]
            for ins in d.get("allocation_instructions", []): ins["invoice_id"] = m("invoice_id", ins["invoice_id"])
            if "settlement_index" in d: d["settlement_index"]["unapplied_row_ids"] = [m("row_id", x) for x in d["settlement_index"]["unapplied_row_ids"]]
    if kind in ("permute", "both"):
        rng.shuffle(ledger); rng.shuffle(docs["invoices"]); rng.shuffle(docs["payments"])
        body = fx_text[1:]; rng.shuffle(body); fx_text = fx_text[:1] + body
    pub = dst / "public"; pub.mkdir(parents=True)
    for f in ("manifest.json", "policy.json"):
        shutil.copy(src / "public" / f, pub / f)
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n"); w.writeheader(); w.writerows(ledger)
    (pub / "ledger.csv").write_text(buf.getvalue(), encoding="utf-8", newline="\n")
    (pub / "fx.csv").write_text("\n".join(fx_text) + "\n", encoding="utf-8", newline="\n")
    for t, rows in docs.items():
        (pub / f"{t}.jsonl").write_text("".join(json.dumps(d, separators=(",", ":")) + "\n" for d in rows), encoding="utf-8", newline="\n")
    ref_map = {}
    for s, table in (("row_id", "ledger"), ("doc_id", "invoices"), ("doc_id", "payments"), ("invoice_id", "invoice"), ("payment_id", "payment")):
        for old, new in maps[s].items():
            ref_map[f"{table}/{old}"] = f"{table}/{new}"
    truth = json.loads((src / "private" / "truth.json").read_text(encoding="utf-8"))   # trusted side only
    mapref = lambda r: ref_map.get(r, r)  # fx/... and policy/... are not surrogate IDs and stay unchanged
    t2 = {"episode_id": truth["episode_id"], "issues": [{**i, "affected_ids": [mapref(x) for x in i["affected_ids"]],
                                                          "evidence_sets": [[mapref(x) for x in g] for g in i["evidence_sets"]]} for i in truth["issues"]]}
    (dst / "private").mkdir()
    (dst / "private" / "truth.json").write_text(json.dumps(t2, indent=1), encoding="utf-8", newline="\n")
    return {v: k for k, v in ref_map.items()}  # inverse map


def oracle(truth):
    return [{"type": i["type"], "affected_ids": i["affected_ids"], "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
             "evidence_ids": i["evidence_sets"][0], "explanation": "oracle"} for i in truth["issues"]]


def phase_pairs(work: Path, make_env, make_policy, open_session, evaluate) -> list[dict]:
    rows = []
    if (work / "runs").exists():  # the internal runner requires new export directories
        shutil.rmtree(work / "runs")
    for n in range(1, 21):
        pid, did = f"P{n:02d}", f"D{n:02d}"
        src = work / "episodes" / did
        dst = work / "pairs" / pid
        if dst.exists(): shutil.rmtree(dst)
        inverse = transform(src, dst, 5000 + n, PAIR_KIND[pid])
        # 1. exact replay on the original (same seed/config/actions) through the internal runner
        def run_once(tag):
            env = make_env("train", evaluator="inprocess")
            obs, info = env.reset(17, {"snapshot_dir": str(src), "export_dir": str(work / "runs" / f"{pid}-{tag}"), "policy_name": "reconcile"})
            pol = make_policy("reconcile", 17); traj = [json.loads(json.dumps(obs))]
            while True:
                obs, r, term, trunc, info = env.step(pol.act(copy.deepcopy(obs)))
                traj.append([json.loads(json.dumps(obs)), r, term, trunc])
                if term or trunc: return traj
        replay_ok = run_once("a") == run_once("b")
        # 2-3. oracle on both sides with identical inspection_steps, scored by the core evaluator
        t0 = json.loads((src / "private" / "truth.json").read_text(encoding="utf-8")); t1 = json.loads((dst / "private" / "truth.json").read_text(encoding="utf-8"))
        o0, o1 = oracle(t0), oracle(t1)
        back = lambda f: {**f, "affected_ids": sorted(inverse.get(x, x) for x in f["affected_ids"]), "evidence_ids": sorted(inverse.get(x, x) for x in f["evidence_ids"])}
        norm0 = sorted(json.dumps({**f, "affected_ids": sorted(f["affected_ids"]), "evidence_ids": sorted(f["evidence_ids"])}, sort_keys=True) for f in o0)
        mapped_ok = sorted(json.dumps(back(f), sort_keys=True) for f in o1) == norm0
        s0 = evaluate({"episode_id": t0["episode_id"], "findings": o0}, t0, 12).score
        s1 = evaluate({"episode_id": t1["episode_id"], "findings": o1}, t1, 12).score
        score_ok = s0 == s1
        # 4. every transformed evidence reference is still served by the public tools
        session = open_session(dst / "public")
        def reach(ref):
            kind, _, rest = ref.partition("/")
            act = {"action": "get_document", "args": {"doc_id": rest}} if kind in ("invoices", "payments") else {"action": "get_record", "args": {"table": kind, "id": rest}}
            return session.inspect(act).get("ok") is True
        refs_ok = all(reach(r) for i in t1["issues"] for g in i["evidence_sets"] for r in g)
        # independent semantics: reviewer calculation on the transformed copy maps back to the original answer
        a0 = audit(src / "public"); a1 = audit(dst / "public")
        calc_ok = sorted(json.dumps(back(f), sort_keys=True) for f in a1["findings"]) == \
                  sorted(json.dumps({**f, "affected_ids": sorted(f["affected_ids"]), "evidence_ids": sorted(f["evidence_ids"])}, sort_keys=True) for f in a0["findings"])
        conds = {"1_exact_replay_original": replay_ok, "2_oracle_maps_back": mapped_ok, "3_scores_equal_same_steps": score_ok,
                 "4_evidence_available_no_new_channel": refs_ok, "reviewer_calc_invariant": calc_ok}
        rows.append({"id": pid, "block": "pairs", "base": did, "transform": PAIR_KIND[pid], "conditions": conds, "score": {k: s0[k] for k in ("tp", "fp", "fn", "reward")},
                     "status": "passed" if all(conds.values()) else "failed",
                     "adapter_transport": "not_checked (internal runner snapshot_dir used as the local analog of episode_dir)"})
    return rows
