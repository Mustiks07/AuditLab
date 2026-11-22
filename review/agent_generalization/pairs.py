"""P9 matched_clean_control: is the clean member of a pair a fair control for the error member?

A fair control has the same commercial history as the error variant's source book, no finding of
the reviewer's calculator, and no simple public signal that tells the two members apart (ledger
rows, document count, table sizes, account set, period). Reads public files and the error
variant's private source book only; truth is not used.
"""
from __future__ import annotations

import csv, io, json
from pathlib import Path

from review.agent_generalization.canonical import chains
from review.genesis_acceptance.independent_calc import audit


def _commercial(p: Path) -> list[str]:
    return sorted(c["digest"] for c in chains(p).values() if c["canon"]["invoices"] or c["canon"]["payments"])


def surface(public: Path) -> dict:
    """Simple public features an auditor could use as a shortcut."""
    m = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    ledger = list(csv.DictReader(io.StringIO((public / "ledger.csv").read_text(encoding="utf-8"))))
    count = lambda n: sum(1 for l in (public / n).read_text(encoding="utf-8").splitlines() if l.strip())
    return {"manifest_ledger_rows": m["ledger_rows"], "ledger_rows": len(ledger), "document_count": m["document_count"],
            "invoices": count("invoices.jsonl"), "payments": count("payments.jsonl"),
            "fx_rows": len(list(csv.DictReader(io.StringIO((public / "fx.csv").read_text(encoding="utf-8"))))),
            "accounts": sorted({r["account"] for r in ledger}), "journals": len({r["journal_id"] for r in ledger}),
            "period": [m["period_start"], m["period_end"]], "currencies": sorted(m["currency_exponents"])}


def matched_clean_control(error_ep: Path, clean_ep: Path) -> dict:
    sb = error_ep / "private" / "source_book"
    clean_answer = audit(clean_ep / "public")
    same_history = _commercial(clean_ep / "public") == _commercial(sb) if sb.is_dir() else None
    se, sc = surface(error_ep / "public"), surface(clean_ep / "public")
    differing = sorted(k for k in se if se[k] != sc[k])
    # journals differ by construction for row-adding/removing defects; they are not a shortcut by themselves
    signals = [k for k in differing if k not in ("journals",)]
    checks = {"clean_has_no_finding": not clean_answer["findings"] and not clean_answer["notes"],
              "same_commercial_history_as_source_book": same_history,
              "no_simple_public_signal": not signals}
    status = "not_checked" if same_history is None else ("passed" if all(checks.values()) else "failed")
    return {"status": status, "checks": checks, "differing_features": {k: [se[k], sc[k]] for k in differing},
            "clean_findings": len(clean_answer["findings"]), "clean_notes": clean_answer["notes"][:3]}
