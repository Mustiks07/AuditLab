"""Expand unique ID prefixes to full public IDs (string lookup only) and write the frozen manual answer."""
import csv, json, re
from pathlib import Path
src = json.loads(Path("manual_before_truth.src.json").read_text(encoding="utf-8"))
for name, case in src["cases"].items():
    p = Path("public_only") / name / "public"
    ids = set()
    for r in csv.DictReader((p / "ledger.csv").open(encoding="utf-8")):
        ids |= {r["row_id"], r["invoice_id"], r["payment_id"], r["journal_id"]}
    for t in ("invoices", "payments"):
        for line in (p / f"{t}.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(line); ids |= {d["doc_id"], d.get("invoice_id") or "", d.get("payment_id") or ""}
    def full(ref):
        kind, _, key = ref.partition("/")
        if kind in ("policy", "fx"): return ref
        hits = [i for i in ids if i.startswith(key)]
        assert len(hits) == 1, (name, ref, hits)
        return f"{kind}/{hits[0]}"
    for f in case["findings"]:
        f["affected_ids"] = sorted(full(a) for a in f["affected_ids"])
        f["evidence_ids"] = [full(e) for e in f["evidence_ids"]]
Path("manual_before_truth.json").write_text(json.dumps(src, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
