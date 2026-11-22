"""Self-check for an organiser's transformed copy: do all public and truth references still resolve?

    python -m review.jury_compatibility_v3.check_links EPISODE_DIR [EPISODE_DIR ...]

Reads only the episode files (standard library, no AuditLab import). Checks ledger → invoices/payments,
payments.invoice_refs, payments.settlement_index.unapplied_row_ids, payments.allocation_instructions and
every truth affected_id / evidence_id against the public keys (``ledger/<row_id>``, ``invoices/<doc_id>``,
``payments/<doc_id>``, ``fx/<date>/<SRC>/<TGT>``, ``policy/<rule>``, ``invoice/<invoice_id>``,
``payment/<payment_id>``). Exit 0 — consistent, 1 — broken references (listed), 2 — unreadable input.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from review.jury_compatibility_v3.check_ids import link_problems, load  # noqa: E402


def main(argv=None) -> int:
    paths = [Path(p) for p in (sys.argv[1:] if argv is None else argv)]
    if not paths:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    worst = 0
    for episode in paths:
        try:
            data = load(episode / "public")
            truth_path = episode / "private" / "truth.json"
            truth = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.exists() else {"issues": []}
        except (OSError, ValueError, KeyError) as error:
            print(json.dumps({"episode": str(episode), "status": "unreadable", "error": f"{type(error).__name__}: {error}"}))
            worst = max(worst, 2)
            continue
        problems = link_problems(data, truth)
        print(json.dumps({"episode": str(episode), "status": "consistent" if not problems else "broken_references",
                          "problems": problems}, ensure_ascii=False))
        worst = max(worst, 1 if problems else 0)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
