"""Literal jury-style pair probe (reviewer tool, not product code).

Emulates ONE plausible reading of the published pair transport (P06-P20): the reviewer knows only
the section-03 fields, renames every value of row_id, doc_id, invoice_id, payment_id, entity_id
(and invoice_refs) consistently, reverses record order, and rewrites truth affected_ids /
evidence_ids by exact whole-string lookup ("opaque" references). Fields it does not know are left
untouched. This is uncertainty U2 of docs/reviews/jury-compatibility-v3/ID_RULES.md, not the
jury's actual tool. A team that uses composite keys or extra reference fields can legitimately
fail here while passing a value-based transformer.

Checks: the transformed copy loads with reset(closed); oracle findings built from the transformed
truth score exactly as on the original (TP/FP/FN/return at inspection_steps=10).

    python3.11 -I literal_pair_probe.py REPO_DIR OUT_JSON ['[extra configs]']
"""
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2])
EXTRA = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []
sys.path.insert(0, str(repo))
os.chdir(repo)
res = {"repo": repo.name, "episodes": [], "notes": []}
import judge_adapter as ja  # noqa: E402

SPACES = ("row_id", "doc_id", "invoice_id", "payment_id", "entity_id")


def find(d, name):
    hits = sorted(Path(d).rglob(name), key=lambda p: ("public" not in p.parts, len(p.parts), str(p)))
    return hits[0] if hits else None


def issues_of(t):
    return t.get("issues", []) if isinstance(t, dict) else (t if isinstance(t, list) else [])


def oracle(issues, eid):
    F = []
    for it in issues:
        sets = (it.get("acceptable_evidence_sets") or it.get("allowed_evidence_sets") or it.get("evidence_sets") or [])
        ev = list(sets[0]) if sets and isinstance(sets[0], list) else list(sets)
        F.append({"type": it.get("type"), "affected_ids": list(it.get("affected_ids", [])),
                  "claimed_amount_minor": it.get("expected_amount_minor"), "currency": it.get("currency"),
                  "evidence_ids": ev[:10], "explanation": "oracle"})
    return {"episode_id": eid, "findings": F}


def transform(src, dst):
    shutil.copytree(src, dst)
    maps = {s: {} for s in SPACES}

    def new(space, old):
        if old in (None, ""):
            return old
        m = maps[space]
        if old not in m:
            m[old] = f"{space[0].upper()}x{len(m):06d}"
        return m[old]
    ledger = find(dst, "ledger.csv")
    rows = list(csv.DictReader(io.StringIO(ledger.read_text(encoding="utf-8"))))
    fields = list(rows[0].keys()) if rows else []
    for r in rows:
        r["row_id"] = new("row_id", r.get("row_id"))
        for f in ("entity_id", "invoice_id", "payment_id"):
            if r.get(f) not in (None, "", "null"):
                r[f] = new(f, r[f])
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n"); w.writeheader()
    for r in reversed(rows):
        w.writerow(r)
    ledger.write_text(buf.getvalue(), encoding="utf-8")
    for name, keys in (("invoices.jsonl", ("doc_id", "invoice_id", "entity_id")),
                       ("payments.jsonl", ("doc_id", "payment_id", "entity_id"))):
        p = find(dst, name)
        docs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        for d in docs:
            for k in keys:
                if d.get(k) is not None:
                    d[k] = new(k, d[k])
            if isinstance(d.get("invoice_refs"), list):
                refs = []
                for x in d["invoice_refs"]:
                    if isinstance(x, dict):  # some teams store {"invoice_id": ..., "amount_minor": ...}
                        x = {**x, **({"invoice_id": new("invoice_id", x["invoice_id"])} if x.get("invoice_id") else {})}
                    elif x is not None:
                        x = new("invoice_id", x)
                    refs.append(x)
                d["invoice_refs"] = refs
        p.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in reversed(docs)), encoding="utf-8")
    lookup = {}
    for s in SPACES:
        lookup.update(maps[s])
    tp = find(dst, "truth.json")
    t = json.loads(tp.read_text(encoding="utf-8"))
    for it in issues_of(t):
        it["affected_ids"] = [lookup.get(x, x) for x in it.get("affected_ids", [])]
        for key in ("acceptable_evidence_sets", "allowed_evidence_sets", "evidence_sets"):
            if isinstance(it.get(key), list):
                it[key] = [[lookup.get(x, x) for x in s] if isinstance(s, list) else lookup.get(s, s) for s in it[key]]
    tp.write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")
    unchanged = sum(1 for it in issues_of(t) for x in it.get("affected_ids", []) if x not in lookup.values())
    return unchanged


work = Path(tempfile.mkdtemp(prefix="pairs-"))
for seed in (11, 12, 13, 14, 15):
    entry = {"seed": seed}
    try:
        src = None
        for cfg in (*EXTRA, {}):
            d = work / f"src-{seed}-{len(json.dumps(cfg))}"
            try:
                ja.generate_episode(seed, cfg, str(d))
            except Exception as exc:
                entry.setdefault("gen_errors", []).append(repr(exc)[:150]); continue
            src = d
            if issues_of(json.loads(find(d, "truth.json").read_text())):
                break
        if src is None:
            entry["status"] = "not_checked"; res["episodes"].append(entry); continue
        eid = json.loads(find(src, "manifest.json").read_text()).get("episode_id")
        t0 = issues_of(json.loads(find(src, "truth.json").read_text()))
        entry["issues"] = len(t0)
        a = ja.score_episode(str(src), oracle(t0, eid), 10)
        dst = work / f"dst-{seed}"
        entry["affected_ids_not_renamed"] = transform(src, dst)
        t1 = issues_of(json.loads(find(dst, "truth.json").read_text()))
        env = ja.make_env()
        obs, info = env.reset(seed, {"mode": "closed", "episode_dir": str(dst), "team_config": {}})
        o, *_ = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 3}})
        entry["reset_ok"] = o.get("error") is None
        b = ja.score_episode(str(dst), oracle(t1, eid), 10)
        keys = ("tp", "fp", "fn", "return")
        entry["original"] = {k: a.get(k) for k in keys}
        entry["transformed"] = {k: b.get(k) for k in keys}
        entry["status"] = "passed" if entry["original"] == entry["transformed"] and entry["reset_ok"] and a.get("fp") == 0 else "failed"
    except Exception as exc:
        entry["status"] = "failed"
        entry["error"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()[:300]
    res["episodes"].append(entry)
st = [e["status"] for e in res["episodes"]]
res["summary"] = {k: st.count(k) for k in ("passed", "failed", "not_checked")}
out.write_text(json.dumps(res, indent=1, default=str))
print(json.dumps(res["summary"]))
