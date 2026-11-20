"""Generate review-v4 cases with generator 1.4 (main a301b16). Copies public/ only for the answer step.
The twin's ID mapping is written next to the sealed episode and is not read before the answer is frozen."""
import json, shutil, sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve(); sys.path[:0] = [str(repo), "."]
from auditlab.generator import generate_episode, GENERATOR_VERSION
from auditlab.release.transforms import transform_public
from scan import cfg, Z
ALL = ["split_payment", "equal_amount_different_supplier", "refund", "archive_copy"]
FULL = {"ledger_rows_target": 2000, "documents_target": 200}
CASES = {
  "N1-full-kzt-usd": (7201, cfg(company_size=FULL, clean_distractors=ALL, interaction="allocation_fx_same_payment",
                               issue_counts={"duplicate_invoice": 2, "payment_allocation": 3, "fx_conversion": 3, "period_cutoff": 2})),
  "N2-full-jpy-kwd-usd-berlin": (7202, cfg(company_size=FULL, clean_distractors=ALL, base_currency="JPY", currencies=["JPY", "KWD", "USD"],
                               timezone="Europe/Berlin", target_period="2026-10", interaction="cutoff_fx_same_invoice",
                               issue_counts={"duplicate_invoice": 1, "payment_allocation": 2, "fx_conversion": 3, "period_cutoff": 2})),
  "N3-half-kzt-jpy-clean": (2, cfg(currencies=["KZT", "JPY"], issue_counts={**Z, "fx_conversion": 1}, clean_distractors=["refund", "split_payment"])),
  "N4-half-kzt-jpy-error": (6, cfg(currencies=["KZT", "JPY"], issue_counts={**Z, "fx_conversion": 1}, clean_distractors=["refund", "split_payment"])),
  "N5-half-jpy-usd-error": (22, cfg(base_currency="JPY", currencies=["JPY", "USD"], issue_counts={**Z, "fx_conversion": 1}, clean_distractors=["refund", "split_payment"])),
  "N6-half-kzt-kwd-both": (192, cfg(currencies=["KZT", "KWD"], issue_counts={**Z, "fx_conversion": 1}, clean_distractors=["refund", "split_payment"])),
}
out = Path(sys.argv[2]); print("generator", GENERATOR_VERSION)
for name, (seed, config) in CASES.items():
    ep = out / "episodes" / name
    generate_episode(seed, config, ep)
    shutil.copytree(ep / "public", out / "public_only" / name / "public")
    m = json.loads((ep / "public" / "manifest.json").read_text(encoding="utf-8"))
    print(name, m["episode_id"][:10], "rows", m["ledger_rows"], "docs", m["document_count"], m["timezone"], m["period_start"], m["period_end"])
# N7: economic twin of N1 (rename all public IDs, permute rows); public-only transform, mapping sealed
twin = out / "episodes" / "N7-twin-of-N1"; (twin).mkdir(parents=True)
mapping = transform_public(out / "episodes" / "N1-full-kzt-usd" / "public", twin / "public", 7207)
(twin / "SEALED-mapping.json").write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
shutil.copytree(twin / "public", out / "public_only" / "N7-twin-of-N1" / "public")
m = json.loads((twin / "public" / "manifest.json").read_text(encoding="utf-8"))
print("N7-twin-of-N1", m["episode_id"][:10], "rows", m["ledger_rows"], "docs", m["document_count"])
(out / "configs.json").write_text(json.dumps({**CASES, "N7-twin-of-N1": {"transform_public": {"source": "N1-full-kzt-usd", "seed": 7207}}}, indent=1), encoding="utf-8")
