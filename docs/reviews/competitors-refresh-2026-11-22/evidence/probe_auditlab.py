"""Reviewer probe (not product code): AuditLab adapter defaults, amount variety, seed brute force.

Run from the repository root with requirements.lock installed:
    python docs/reviews/competitors-refresh-2026-11-22/evidence/probe_auditlab.py OUT_DIR
"""
import json, sys, time, collections, secrets
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
import judge_adapter as ja
from auditlab.generator.core import random_stream, PREFIXES
from auditlab.generator import profile13

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=False)
report = {"defaults": [], "amounts": {}, "attack": {}}
for seed in range(1, 9):
    d = out / f"default-{seed}"; ja.generate_episode(seed, {}, str(d))
    m = json.loads((d / "public/manifest.json").read_text()); t = json.loads((d / "private/truth.json").read_text())
    report["defaults"].append({"seed": seed, "rows": m["ledger_rows"], "docs": m["document_count"], "tz": m["timezone"],
                               "base": m["base_currency"], "period": m["period_start"][:7], "issues": len(t["issues"])})
for typ in ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"):
    vals = []
    for seed in range(100, 110):
        d = out / f"{typ}-{seed}"; ja.generate_episode(seed, {"issue_counts": {typ: 1}}, str(d))
        t = json.loads((d / "private/truth.json").read_text())
        prov = json.loads((d / "private/provenance.json").read_text())
        muts = [x for x in prov.get("mutations", []) if x.get("type") == typ]
        share = None
        if typ == "payment_allocation" and muts:
            b, a = muts[0].get("before", {}), muts[0].get("after", {})
            if b.get("applied_minor"): share = round(a.get("applied_minor", 0) / b["applied_minor"], 4)
        vals.append({"seed": seed, "amount": t["issues"][0]["expected_amount_minor"], "currency": t["issues"][0]["currency"], "applied_share": share})
    report["amounts"][typ] = vals
# Brute force: unkeyed default config {} vs keyed, identical attacker effort.
def first_invoice_ids(seed, config):
    _, ident = profile13.materials(seed, {**config, "config_version": "1.3"})
    return f"{PREFIXES['invoice']}{random_stream(ident, 'ids/invoice').getrandbits(96):024X}"
for label, cfg in (("unkeyed_default", {}), ("keyed", {"episode_key": secrets.token_hex(16)})):
    secret = 412907
    d = out / f"attack-{label}"; ja.generate_episode(secret, cfg, str(d))
    ids = {json.loads(l)["invoice_id"] for l in (d / "public/invoices.jsonl").read_text().splitlines() if l.strip()}
    t0 = time.time(); found = None
    for s in range(0, 1_000_000):
        if first_invoice_ids(s, {}) in ids: found = s; break
    report["attack"][label] = {"true_seed": secret, "recovered": found, "seconds": round(time.time() - t0, 1),
                               "searched": "0..999999 with attacker-guessed config {}"}
(out / "probe.json").write_text(json.dumps(report, indent=1))
print(json.dumps(report, indent=1))
