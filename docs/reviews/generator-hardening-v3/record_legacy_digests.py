"""Record tree SHA-256 of profiles 1.0-1.2 (run at the base d745376 and at the head; outputs must match).

python docs/reviews/generator-hardening-v3/record_legacy_digests.py > digests.json
"""
import hashlib, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from auditlab.generator import generate_episode
CASES = [
 (3, {}), (817, {"settlement_timing": "after", "clean_distractors": []}),
 (11, {"config_version": "1.1", "issue_counts": {"duplicate_invoice": 1, "payment_allocation": 1, "fx_conversion": 1, "period_cutoff": 1}}),
 (12, {"config_version": "1.1", "currencies": ["KZT", "JPY"], "interaction": "cutoff_fx_same_invoice", "issue_counts": {"period_cutoff": 1, "fx_conversion": 1}, "company_size": {"ledger_rows_target": 600, "documents_target": 90}}),
 (13, {"config_version": "1.1", "allocation_mode": "omitted", "issue_counts": {"payment_allocation": 2}, "clean_distractors": ["split_payment", "next_day_settlement"]}),
 (21, {"config_version": "1.2", "issue_counts": {"payment_allocation": 1, "fx_conversion": 1}, "interaction": "allocation_fx_same_payment"}),
 (22, {"config_version": "1.2", "financial_case": {"kind": "allocation", "amount_minor": 1234567, "settlement_offset_minutes": 0}, "issue_counts": {"payment_allocation": 1}}),
 (23, {"config_version": "1.2", "financial_case": {"kind": "invoice_cutoff", "amount_minor": 777700}, "issue_counts": {}, "timezone": "Asia/Tokyo", "target_period": "2025-02"}),
]
def digest(path):
    d = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        d.update(str(f.relative_to(path).as_posix()).encode() + b"\0" + f.read_bytes() + b"\0")
    return d.hexdigest()
out = []
with tempfile.TemporaryDirectory() as tmp:
    for i, (seed, cfg) in enumerate(CASES):
        t = Path(tmp) / str(i); generate_episode(seed, cfg, t)
        out.append({"seed": seed, "config": cfg, "tree_sha256": digest(t)})
print(json.dumps(out, indent=1))
