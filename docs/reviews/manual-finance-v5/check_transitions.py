"""DAT-01, own replay (not tools/check_source_books.py): every difference between private/source_book
and the final public/ must be exactly one source_transition entry, and nothing else may differ.

usage: python check_transitions.py WORKDIR
"""
import csv, json, sys
from pathlib import Path

work = Path(sys.argv[1])
KEYS = {"ledger": ("ledger.csv", "row_id"), "invoices": ("invoices.jsonl", "doc_id"), "payments": ("payments.jsonl", "doc_id")}


def table(folder, name):
    fname, key = KEYS[name]
    if fname.endswith(".csv"):
        rows = list(csv.DictReader((folder / fname).open(encoding="utf-8", newline="")))
        rows = [{k: (int(v) if k in ("debit_minor", "credit_minor") else (None if v == "null" else v)) for k, v in r.items()} for r in rows]
    else:
        rows = [json.loads(l) for l in (folder / fname).read_text(encoding="utf-8").splitlines() if l.strip()]
    return {r[key]: r for r in rows}


def norm(r):
    return None if r is None else {k: (None if v == "null" else v) for k, v in r.items()}


summary = {}
for case in sorted((work / "sealed").iterdir()):
    priv = case / "private"; src = priv / "source_book"; pub = work / "public" / case.name / "public"
    trans = json.loads((priv / "source_transition.json").read_text(encoding="utf-8"))
    entries = trans if isinstance(trans, list) else trans.get("changes", trans.get("entries", []))
    expected = {(e["table"], e["id"]): (norm(e["before"]), norm(e["after"])) for e in entries}
    actual = {}
    for name in KEYS:
        a, b = table(src, name), table(pub, name)
        for i in set(a) | set(b):
            if norm(a.get(i)) != norm(b.get(i)):
                actual[(name, i)] = (norm(a.get(i)), norm(b.get(i)))
    # v1 compared bytes and flagged G03, where only the fx.csv row order differs (my check error).
    # v2: fx.csv as a multiset of lines (order is not financial content), policy.json as parsed JSON.
    same_other = (sorted((src / "fx.csv").read_text(encoding="utf-8").splitlines()) == sorted((pub / "fx.csv").read_text(encoding="utf-8").splitlines())
                  and json.loads((src / "policy.json").read_text(encoding="utf-8")) == json.loads((pub / "policy.json").read_text(encoding="utf-8")))
    bytes_same = all((src / f).read_bytes() == (pub / f).read_bytes() for f in ("fx.csv", "policy.json"))
    unexplained = sorted(k for k in actual if k not in expected)
    wrong = sorted(k for k in actual if k in expected and expected[k] != actual[k])
    phantom = sorted(k for k in expected if k not in actual)
    summary[case.name] = {"changed_objects": len(actual), "transition_entries": len(expected), "unexplained": [list(x) for x in unexplained],
                          "mismatched": [list(x) for x in wrong], "phantom": [list(x) for x in phantom], "fx_policy_identical": same_other, "fx_policy_bytes_identical": bytes_same}
    flag = "OK" if not (unexplained or wrong or phantom) and same_other else "PROBLEM"
    print(f"{case.name:32} {flag} changed={len(actual)} transition={len(expected)} unexplained={len(unexplained)} mismatched={len(wrong)} phantom={len(phantom)} fx/policy same={same_other} bytes_same={bytes_same}")
json.dump(summary, sys.stdout if False else open(work / "transitions.json", "w", encoding="utf-8"), indent=1)
