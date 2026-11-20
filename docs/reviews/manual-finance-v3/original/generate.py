"""Generate new profile-1.1 examples with the real generator (main 461a11e). Only public/ is copied for the manual step."""
import json, shutil, sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(repo))
from auditlab.generator import generate_episode
Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}
def cfg(seed, **over):
    base = {"config_version": "1.1", "company_size": {"ledger_rows_target": 0, "documents_target": 0},
            "base_currency": "KZT", "currencies": ["KZT", "USD"], "timezone": "Asia/Almaty", "target_period": "2026-11",
            "issue_counts": dict(Z), "interaction": None, "clean_distractors": []}
    base.update(over); return seed, base
CASES = {
    "C1-clean-refund": cfg(7101, clean_distractors=["refund", "split_payment"]),
    "C2-partial-allocation": cfg(7102, issue_counts={**Z, "payment_allocation": 1}, settlement_timing="after"),
    "C3a-boundary-before": cfg(7103, settlement_timing="before"),
    "C3b-boundary-at": cfg(7103, issue_counts={**Z, "payment_allocation": 1}, settlement_timing="at"),
    "C4-interaction-a": cfg(7104, issue_counts={**Z, "payment_allocation": 1, "fx_conversion": 1}, interaction="allocation_fx_same_payment"),
    "C5-interaction-b": cfg(7105, issue_counts={**Z, "period_cutoff": 1, "fx_conversion": 1}, interaction="cutoff_fx_same_invoice"),
    "C6a-jpy-base-kwd": cfg(7106, base_currency="JPY", currencies=["JPY", "KWD"], issue_counts={**Z, "fx_conversion": 1, "duplicate_invoice": 1}),
    "C6b-kwd-base-jpy": cfg(7107, base_currency="KWD", currencies=["KWD", "JPY"], issue_counts={**Z, "fx_conversion": 1, "period_cutoff": 1}),
    "C7a-dst-berlin-oct": cfg(7108, base_currency="EUR", currencies=["EUR", "USD"], timezone="Europe/Berlin", target_period="2026-10",
                              issue_counts={**Z, "payment_allocation": 1, "fx_conversion": 1, "period_cutoff": 1}, settlement_timing="at"),
    "C7b-dst-newyork-mar": cfg(7109, base_currency="USD", currencies=["USD", "EUR"], timezone="America/New_York", target_period="2026-03",
                               issue_counts={**Z, "fx_conversion": 1, "period_cutoff": 1}, clean_distractors=["refund"]),
}
out = Path(sys.argv[2])
for name, (seed, config) in CASES.items():
    ep = out / "episodes" / name
    try:
        generate_episode(seed, config, ep)
    except Exception as error:
        print(name, "REJECTED", error); continue
    shutil.copytree(ep / "public", out / "public_only" / name / "public")
    m = json.loads((ep / "public" / "manifest.json").read_text(encoding="utf-8"))
    print(name, "ok", m["episode_id"][:10], "rows", m["ledger_rows"], "docs", m["document_count"], m["period_start"], m["period_end"])
(out / "configs.json").write_text(json.dumps(CASES, indent=1), encoding="utf-8")
