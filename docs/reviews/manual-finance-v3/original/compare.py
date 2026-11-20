"""After the manual answer is frozen: compare with truth, score with the real evaluator, and check
that every truth evidence id is served by the public tools (open_session.inspect)."""
import json, sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(repo))
from auditlab.environment import open_session
from auditlab.evaluator import evaluate
manual = json.loads(Path("manual_before_truth.json").read_text(encoding="utf-8"))
def key(f, amount): return (f["type"], tuple(sorted(f["affected_ids"])), f[amount], f["currency"])
def fetch(session, ref):
    kind, _, rest = ref.partition("/")
    if kind in ("invoices", "payments"):
        action = {"action": "get_document", "args": {"doc_id": rest}}
    elif kind == "fx":
        action = {"action": "get_record", "args": {"table": "fx", "id": rest}}
    else:
        action = {"action": "get_record", "args": {"table": kind, "id": rest}}
    reply = session.inspect(action)
    return reply.get("ok") is True, action
report = {}
for name, case in manual["cases"].items():
    ep = Path("episodes") / name
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    mine = {key(f, "claimed_amount_minor"): f for f in case["findings"]}
    theirs = {key(i, "expected_amount_minor"): i for i in truth["issues"]}
    rows = []
    for k in sorted(set(mine) | set(theirs)):
        m, t = mine.get(k), theirs.get(k)
        cover = None if not (m and t) else [set(g) <= set(m["evidence_ids"]) for g in t["evidence_sets"]]
        rows.append({"key": [k[0], list(k[1]), k[2], k[3]], "manual": bool(m), "truth": bool(t),
                     "covers_some_truth_set": None if cover is None else any(cover),
                     "truth_sets": t["evidence_sets"] if t else None, "manual_evidence": m["evidence_ids"] if m else None})
    sub = {"episode_id": truth["episode_id"], "findings": [{k: v for k, v in f.items() if k != "arithmetic"} | {"explanation": f["arithmetic"][:1000]} for f in case["findings"]]}
    s = evaluate(sub, truth, 0).score
    session = open_session(ep / "public")
    refs = sorted({r for i in truth["issues"] for g in i["evidence_sets"] for r in g})
    unreachable = [r for r in refs if not fetch(session, r)[0]]
    report[name] = {"rows": rows, "score_of_manual_answer": {k: s[k] for k in ("tp", "fp", "fn", "reward")},
                    "truth_refs": len(refs), "unreachable_refs": unreachable}
    status = "MATCH" if all(r["manual"] and r["truth"] and r["covers_some_truth_set"] for r in rows) else "DIFF"
    print(f"{name:22} {status} issues={len(truth['issues'])} manual={len(case['findings'])} score={s['tp']}/{s['fp']}/{s['fn']} reward={s['reward']} refs={len(refs)} unreachable={unreachable}")
    for r in rows:
        if not (r["manual"] and r["truth"] and r["covers_some_truth_set"]):
            print("   ", json.dumps(r, ensure_ascii=False)[:600])
Path("comparison_after_truth.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
