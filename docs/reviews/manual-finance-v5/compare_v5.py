"""After the frozen answer (commit 871b479): compare with truth, check public addressability,
then separately run ordinary reconcile, the project verifier, control distortions and DAT-01.

usage: python compare_v5.py REPO WORKDIR OUT_JSON
"""
import copy, json, shutil, sys, tempfile
from pathlib import Path

repo, work, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(sys.argv[3])
here = Path(__file__).resolve().parent
sys.path[:0] = [str(repo), str(here)]
from auditlab.environment import open_session  # noqa: E402
from auditlab.evaluator import evaluate  # noqa: E402
from auditlab.runner import make_env  # noqa: E402
from auditlab.policies import make_policy  # noqa: E402
from auditlab.verifier import verify_report  # noqa: E402
from audit_calc import audit  # noqa: E402

ans = json.loads((here / "answer_before_truth.json").read_text(encoding="utf-8"))
K = lambda f, a: (f["type"], tuple(sorted(f["affected_ids"])), f[a], f["currency"])
sub = lambda eid, fs: {"episode_id": eid, "findings": [{**{k: f[k] for k in ("type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids")}, "explanation": "v5"} for f in fs]}
tmp = Path(tempfile.mkdtemp(prefix="v5cmp-"))


def fetch(session, ref):
    kind, _, rest = ref.partition("/")
    a = {"action": "get_document", "args": {"doc_id": rest}} if kind in ("invoices", "payments") else {"action": "get_record", "args": {"table": kind, "id": rest}}
    return session.inspect(a).get("ok") is True


def check(name, public, truth, findings):
    mine = {K(f, "claimed_amount_minor"): f for f in findings}; theirs = {K(i, "expected_amount_minor"): i for i in truth["issues"]}
    rows = []
    for k in sorted(set(mine) | set(theirs)):
        m, t = mine.get(k), theirs.get(k)
        rows.append({"key": [k[0], list(k[1]), k[2], k[3]], "answer": bool(m), "truth": bool(t),
                     "covers": None if not (m and t) else any(set(g) <= set(m["evidence_ids"]) for g in t["evidence_sets"]),
                     "identical": None if not (m and t) else any(sorted(g) == sorted(m["evidence_ids"]) for g in t["evidence_sets"]),
                     "truth_sets": t["evidence_sets"] if t else None, "answer_evidence": m["evidence_ids"] if m else None})
    s = evaluate(sub(truth["episode_id"], findings), truth, 0).score
    session = open_session(public)
    refs = sorted({r for i in truth["issues"] for g in i["evidence_sets"] for r in g})
    unreachable = [r for r in refs if not fetch(session, r)]
    snap = tmp / name; shutil.copytree(public, snap / "public"); (snap / "private").mkdir()
    (snap / "private" / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    env = make_env("train"); obs, _ = env.reset(41, {"snapshot_dir": str(snap), "export_dir": str(tmp / f"run-{name}"), "policy_name": "reconcile"})
    pol = make_policy("reconcile", 41); steps = 0
    while True:
        obs, reward, term, trunc, info = env.step(pol.act(copy.deepcopy(obs))); steps += 1
        if term or trunc:
            break
    rs = json.loads((tmp / f"run-{name}" / "score.json").read_text(encoding="utf-8"))
    vp = tmp / f"ver-{name}" / "public"; shutil.copytree(public, vp); vr = verify_report(vp)
    vs = evaluate(sub(truth["episode_id"], vr["findings"]), truth, 0).score
    ctl = {}
    if findings:   # intentional test distortions of answer copies (not defects)
        for label, mutate in (("amount+1", lambda f: f.update(claimed_amount_minor=f["claimed_amount_minor"] + 1)),
                              ("currency", lambda f: f.update(currency="XXX"))):
            fs = copy.deepcopy(findings); mutate(fs[0]); c = evaluate(sub(truth["episode_id"], fs), truth, 0).score
            ctl[label] = [c["tp"], c["fp"], c["fn"]]
    extra = copy.deepcopy(findings) + [{"type": "period_cutoff", "affected_ids": ["invoice/NOT-AN-ISSUE"], "claimed_amount_minor": 1, "currency": "XXX", "evidence_ids": ["policy/period"]}]
    c = evaluate(sub(truth["episode_id"], extra), truth, 0).score; ctl["extra_finding"] = [c["tp"], c["fp"], c["fn"]]
    ok = all(r["answer"] and r["truth"] and r["covers"] for r in rows)
    return {"status": "MATCH" if ok else "DIFF", "truth_issues": len(truth["issues"]), "answer_score": [s["tp"], s["fp"], s["fn"]],
            "identical_sets": sum(bool(r["identical"]) for r in rows), "rows": rows, "refs": len(refs), "unreachable": unreachable,
            "reconcile": {"steps": steps, "tp_fp_fn": [rs["tp"], rs["fp"], rs["fn"]], "reward": rs["reward"], "truncated": trunc},
            "verifier": {"engine": vr["engine"], "rules": vr["rules_version"], "tp_fp_fn": [vs["tp"], vs["fp"], vs["fn"]], "notes": vr["notes"]},
            "controls": ctl}


report = {"generator_cases": {}, "fixtures": {}, "dat01_source_books": {}}
for name, case in ans["generator_cases"].items():
    sealed = work / "sealed" / name / "private"
    truth = json.loads((sealed / "truth.json").read_text(encoding="utf-8"))
    r = check(name, work / "public" / name / "public", truth, case["automated"]["findings"]); report["generator_cases"][name] = r
    # DAT-01: my calculator on the complete original book (same public formats) must find nothing
    sb = sealed / "source_book"
    if sb.is_dir():
        clean = audit(sb)
        report["dat01_source_books"][name] = {"findings": len(clean["findings"]), "notes": clean["notes"],
                                              "files": sorted(p.name for p in sb.iterdir())}
    else:
        report["dat01_source_books"][name] = "missing"
    print(f"{name:32} {r['status']} truth={r['truth_issues']:2} ans={r['answer_score']} ident={r['identical_sets']} refs={r['refs']} unreach={len(r['unreachable'])}"
          f" | rec {r['reconcile']['steps']} {r['reconcile']['tp_fp_fn']} | ver {r['verifier']['tp_fp_fn']} n={len(r['verifier']['notes'])} | ctl {r['controls']}"
          f" | source_book findings={report['dat01_source_books'][name] if isinstance(report['dat01_source_books'][name], str) else report['dat01_source_books'][name]['findings']}")
    for row in r["rows"]:
        if not (row["answer"] and row["truth"] and row["covers"] and row["identical"]):
            print("     ", json.dumps(row, ensure_ascii=False)[:600])
for name, case in ans["fixtures"].items():
    truth = case["manual_hand_truth"]
    r = check(name, work / "fixtures" / name / "public", truth, case["automated"]["findings"]); report["fixtures"][name] = r
    print(f"FIXTURE {name:24} {r['status']} hand={r['truth_issues']} ans={r['answer_score']} ident={r['identical_sets']} unreach={len(r['unreachable'])}"
          f" | rec {r['reconcile']['steps']} {r['reconcile']['tp_fp_fn']} | ver {r['verifier']['tp_fp_fn']} notes={r['verifier']['notes']} | ctl {r['controls']}")
    for row in r["rows"]:
        if not (row["answer"] and row["truth"] and row["covers"] and row["identical"]):
            print("     ", json.dumps(row, ensure_ascii=False)[:600])
out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
shutil.rmtree(tmp, ignore_errors=True)
