"""Minimal reproductions of the data-design defects found by the agent-generalization review.

    python docs/research/agent-generalization/repro_defects.py OUT_DIR

Run from the repository root at main 611163e75468f4af95cfa115f033e7a9f8c81145 with the pinned lock.
Uses only the generator's public entry point and the reviewer's canonical histories; truth is not read.
"""
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from auditlab.generator import generate_episode  # noqa: E402
from review.agent_generalization.canonical import chains  # noqa: E402

out = Path(sys.argv[1])
Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}
BASE = {"config_version": "1.1", "company_size": {"ledger_rows_target": 400, "documents_target": 65}, "base_currency": "KZT", "currencies": ["KZT", "USD"],
        "timezone": "Asia/Almaty", "target_period": "2026-11", "interaction": None,
        "clean_distractors": ["split_payment", "equal_amount_different_supplier", "refund", "archive_copy", "next_day_settlement"]}


def tree(p):
    return hashlib.sha256(b"".join(f.read_bytes() for f in sorted(p.rglob("*")) if f.is_file())).hexdigest()


def rows(p):
    return json.loads((p / "manifest.json").read_text())["ledger_rows"]


report = {}
# Defect 1: turning a defect off does not give the same history for period_cutoff (and changes padding for duplicates)
for t in ("payment_allocation", "fx_conversion", "duplicate_invoice", "period_cutoff"):
    for s in (0, 1):
        generate_episode(424242, {**BASE, "issue_counts": {**Z, t: s}}, out / f"toggle-{t}-{s}")
    commercial = lambda p: sorted(c["digest"] for c in chains(p).values() if c["canon"]["invoices"] or c["canon"]["payments"])
    a, b = out / f"toggle-{t}-0" / "private" / "source_book", out / f"toggle-{t}-1" / "private" / "source_book"
    report[f"1_{t}"] = {"source_books_identical": tree(a) == tree(b), "commercial_chains_identical": commercial(a) == commercial(b),
                        "source_book_rows_off_on": [rows(a), rows(b)]}
# Defect 2: a row-adding mutation leaves the source book smaller than the final book (row-count shortcut for clean books)
e = out / "toggle-duplicate_invoice-1"
report["2_duplicate_rows_source_vs_final"] = [rows(e / "private" / "source_book"), rows(e / "public")]
# Defect 3: the boundary settlement pair is one fixed commercial history, shared with the published train set
digests = {}
for seed in (11, 12):
    generate_episode(seed, {**BASE, "issue_counts": {**Z, "payment_allocation": 1}, "settlement_timing": "at"}, out / f"boundary-{seed}")
    digests[seed] = {c["digest"] for c in chains(out / f"boundary-{seed}" / "public").values()} |                     {c["digest"] for c in chains(out / f"boundary-{seed}" / "private" / "source_book").values()}
train = set()
for p in (ROOT / "datasets/open/train").glob("*/public"):
    train |= {c["digest"] for c in chains(p).values()}
shared = digests[11] & digests[12]
report["3_boundary_chain_shared_by_two_seeds"] = len(shared)
report["3_boundary_chains_also_in_published_train"] = len(shared & train)
print(json.dumps(report, indent=1))
