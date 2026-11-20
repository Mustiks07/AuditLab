"""After the frozen answer: compare with truth, score with the evaluator, check public reachability,
then (separately) run ordinary reconcile and the verifier."""
import copy, json, shutil, sys, tempfile
from pathlib import Path
repo = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(repo))
from auditlab.environment import open_session
from auditlab.evaluator import evaluate
from auditlab.release.transforms import transform_truth
from auditlab.runner import make_env
from auditlab.policies import make_policy
from auditlab.verifier import verify_report
ans = json.loads(Path("answer_before_truth.json").read_text(encoding="utf-8"))
key = lambda f, a: (f["type"], tuple(sorted(f["affected_ids"])), f[a], f["currency"])
def truth_of(name):
    if name == "N7-twin-of-N1":
        mapping = json.loads(Path("episodes/N7-twin-of-N1/SEALED-mapping.json").read_text(encoding="utf-8"))
        return transform_truth(json.loads(Path("episodes/N1-full-kzt-usd/private/truth.json").read_text(encoding="utf-8")), mapping)
    return json.loads((Path("episodes") / name / "private" / "truth.json").read_text(encoding="utf-8"))
def fetch(session, ref):
    kind, _, rest = ref.partition("/")
    a = {"action": "get_document", "args": {"doc_id": rest}} if kind in ("invoices", "payments") else {"action": "get_record", "args": {"table": kind, "id": rest}}
    return session.inspect(a).get("ok") is True
report = {}; tmp = Path(tempfile.mkdtemp(prefix="v4cmp-"))
for name, case in ans["cases"].items():
    truth = truth_of(name); pub = Path("episodes") / name / "public"
    if truth["episode_id"] != case["episode_id"]:
        print(name, "EPISODE ID MISMATCH", truth["episode_id"], case["episode_id"])
    mine = {key(f, "claimed_amount_minor"): f for f in case["findings"]}; theirs = {key(i, "expected_amount_minor"): i for i in truth["issues"]}
    rows = []
    for k in sorted(set(mine) | set(theirs)):
        m, t = mine.get(k), theirs.get(k)
        exact = None if not (m and t) else [sorted(g) == sorted(m["evidence_ids"]) for g in t["evidence_sets"]]
        cover = None if not (m and t) else [set(g) <= set(m["evidence_ids"]) for g in t["evidence_sets"]]
        rows.append({"key": [k[0], list(k[1]), k[2], k[3]], "manual": bool(m), "truth": bool(t), "covers": None if cover is None else any(cover),
                     "identical_set": None if exact is None else any(exact), "truth_sets": t["evidence_sets"] if t else None, "manual_evidence": m["evidence_ids"] if m else None})
    sub = {"episode_id": truth["episode_id"], "findings": [{**{k: f[k] for k in ("type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids")}, "explanation": "review v4"} for f in case["findings"]]}
    s = evaluate(sub, truth, 0).score
    session = open_session(pub)
    refs = sorted({r for i in truth["issues"] for g in i["evidence_sets"] for r in g})
    unreachable = [r for r in refs if not fetch(session, r)]
    # separately: ordinary reconcile through the runner (needs a snapshot dir with private/truth.json)
    snap = tmp / name; shutil.copytree(pub, snap / "public"); (snap / "private").mkdir()
    (snap / "private" / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    env = make_env("train"); obs, _ = env.reset(31, {"snapshot_dir": str(snap), "export_dir": str(tmp / f"run-{name}"), "policy_name": "reconcile"})
    pol = make_policy("reconcile", 31); steps = 0
    while True:
        obs, reward, term, trunc, info = env.step(pol.act(copy.deepcopy(obs))); steps += 1
        if term or trunc: break
    rs = json.loads((tmp / f"run-{name}" / "score.json").read_text(encoding="utf-8"))
    vpub = tmp / f"ver-{name}" / "public"; shutil.copytree(pub, vpub); vr = verify_report(vpub)
    vs = evaluate({"episode_id": truth["episode_id"], "findings": vr["findings"]}, truth, 0).score
    ok = all(r["manual"] and r["truth"] and r["covers"] for r in rows)
    report[name] = {"status": "MATCH" if ok else "DIFF", "rows": rows, "answer_score": {k: s[k] for k in ("tp", "fp", "fn", "reward")},
                    "truth_refs": len(refs), "unreachable": unreachable, "reconcile": {"steps": steps, **{k: rs[k] for k in ("tp", "fp", "fn", "reward")}, "truncated": trunc},
                    "verifier": {"engine": vr["engine"], "rules": vr["rules_version"], **{k: vs[k] for k in ("tp", "fp", "fn")}, "notes": vr["notes"]}}
    print(f"{name:28} {report[name]['status']} issues={len(truth['issues'])} answer={s['tp']}/{s['fp']}/{s['fn']} identical_sets={sum(bool(r['identical_set']) for r in rows)}/{len(rows)} refs={len(refs)} unreachable={len(unreachable)} | reconcile {steps} steps {rs['tp']}/{rs['fp']}/{rs['fn']} r={rs['reward']:.3f} | verifier {vs['tp']}/{vs['fp']}/{vs['fn']} notes={len(vr['notes'])}")
    for r in rows:
        if not (r["manual"] and r["truth"] and r["covers"] and r["identical_set"]):
            print("    ", json.dumps(r, ensure_ascii=False)[:700])
Path("comparison_after_truth.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
shutil.rmtree(tmp, ignore_errors=True)
