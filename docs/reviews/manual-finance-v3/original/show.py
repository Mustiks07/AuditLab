"""Display only: group public ledger rows by journal, print docs and rates. No financial logic."""
import csv, json, sys
from pathlib import Path
for name in sys.argv[1:]:
    p = Path("public_only") / name / "public"
    m = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
    print(f"##### {name} base={m['base_currency']} exps={m['currency_exponents']} tz={m['timezone']} [{m['period_start']} .. {m['period_end']})")
    sid = lambda s: s if s in ("null", "", None) else s[:5]
    rows = list(csv.DictReader((p / "ledger.csv").open(encoding="utf-8")))
    by = {}
    for r in rows: by.setdefault(r["journal_id"], []).append(r)
    for j, rs in sorted(by.items(), key=lambda kv: kv[1][0]["timestamp"]):
        print(f"  {sid(j)} " + " | ".join(f"{r['timestamp']} {r['account']} D{r['debit_minor']} C{r['credit_minor']} {r['currency']} e={sid(r['entity_id'])} i={sid(r['invoice_id'])} p={sid(r['payment_id'])} [{sid(r['row_id'])}]" for r in rs))
    for t in ("invoices", "payments"):
        for line in (p / f"{t}.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            extra = {k: v for k, v in d.items() if k not in ("line_items",)}
            for k in ("doc_id", "invoice_id", "payment_id", "entity_id"):
                if k in extra: extra[k] = sid(extra[k])
            if "invoice_refs" in extra: extra["invoice_refs"] = [sid(x) for x in extra["invoice_refs"]]
            if "settlement_index" in extra: extra["settlement_index"] = [sid(x) for x in extra["settlement_index"]["unapplied_row_ids"]]
            if t == "invoices": extra["lines"] = [li["amount_minor"] for li in d["line_items"]]
            print(f"  {t}: {extra}")
    print("  fx:", [tuple(r.values()) for r in csv.DictReader((p / "fx.csv").open(encoding="utf-8"))])
