"""Trusted second step: compare a fixed verifier answer with truth, classify, shrink counterexamples.

Never imported by the verifier itself. The answer is computed and written (with its hash)
before truth.json is opened. Usage:

    python -m auditlab.verifier.crosscheck --public EPISODE/public --truth EPISODE/private/truth.json \
        --out artifacts/crosscheck-EPISODE [--engine duckdb|sqlite] [--no-reduce]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile

from auditlab.contracts import MAX_EVIDENCE

from .core import finding_key, verify_report
from .loader import load_snapshot


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def public_refs(public_dir: Path) -> set[str]:
    """Every evidence key the public snapshot can serve (SPEC §5 'Ссылки')."""
    refs = set()
    with (public_dir / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        refs |= {"ledger/" + row["row_id"] for row in csv.DictReader(stream)}
    for table in ("invoices", "payments"):
        for line in (public_dir / f"{table}.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                refs.add(f"{table}/" + json.loads(line)["doc_id"])
    with (public_dir / "fx.csv").open(encoding="utf-8", newline="") as stream:
        refs |= {f"fx/{r['date']}/{r['source_currency']}/{r['target_currency']}" for r in csv.DictReader(stream)}
    policy = json.loads((public_dir / "policy.json").read_text(encoding="utf-8"))
    refs |= {"policy/" + key for key in policy}
    return refs


def public_entities(public_dir: Path) -> set[str]:
    snapshot = load_snapshot(public_dir)
    entities = {"invoice/" + row[1] for row in snapshot.tables["invoices"]}
    entities |= {"payment/" + row[1] for row in snapshot.tables["payments"]}
    return entities


def compare(report: dict, truth: dict, public_dir: Path) -> list[dict]:
    """Discrepancies between a fixed verifier report and truth, each with a category and suspects."""
    discrepancies = []
    refs, entities = public_refs(public_dir), public_entities(public_dir)
    notes = report.get("notes", [])
    integrity = [n for n in notes if n["kind"] == "integrity"]

    def suspects(subjects: set[str], default: list[str]) -> list[str]:
        if any(n["kind"] == "ambiguity" and n["subject"] in subjects for n in notes):
            return ["policy_ambiguity"] + default
        return (["generator"] if integrity else []) + default

    if truth.get("episode_id") != report["episode_id"]:
        discrepancies.append({"category": "episode_mismatch", "truth": truth.get("episode_id"),
                              "verifier": report["episode_id"], "suspects": ["truth"]})
    issues = truth.get("issues", [])
    findings = list(report["findings"])
    unmatched = []
    for issue in issues:
        bad_refs = sorted({ref for group in issue["evidence_sets"] for ref in group} - refs)
        if bad_refs:
            discrepancies.append({"category": "evidence_ref_unknown", "issue_id": issue.get("issue_id"),
                                  "refs": bad_refs, "suspects": ["truth"]})
        bad_entities = sorted(set(issue["affected_ids"]) - entities)
        if bad_entities:
            discrepancies.append({"category": "affected_id_unknown", "issue_id": issue.get("issue_id"),
                                  "ids": bad_entities, "suspects": ["truth"]})
        for group in issue["evidence_sets"]:
            if len(group) > MAX_EVIDENCE or not group:
                discrepancies.append({"category": "evidence_set_size", "issue_id": issue.get("issue_id"),
                                      "size": len(group), "suspects": ["truth"]})
        key = finding_key(issue, "expected_amount_minor")
        match = next((f for f in findings if finding_key(f) == key), None)
        if match is None:
            unmatched.append(issue)
            continue
        findings.remove(match)
        # Membership: some truth set must be covered by the verifier's evidence.
        # Sufficiency: a truth set strictly smaller than the verifier's set omits a proof step.
        mine = set(match["evidence_ids"])
        base = {"issue_id": issue.get("issue_id"), "truth": _row(issue, "expected_amount_minor"),
                "truth_sets": issue["evidence_sets"], "verifier_evidence": match["evidence_ids"]}
        if not any(set(group) <= mine for group in issue["evidence_sets"]):
            discrepancies.append({"category": "evidence_set_differs", **base,
                                  "suspects": ["truth", "verifier", "policy_ambiguity"]})
        for group in issue["evidence_sets"]:
            if set(group) < mine:
                discrepancies.append({"category": "evidence_insufficient", **base,
                                      "missing": sorted(mine - set(group)),
                                      "suspects": ["truth", "policy_ambiguity"]})
    for issue in unmatched:
        key = finding_key(issue, "expected_amount_minor")
        near = None
        for category, same in (("amount_mismatch", lambda f: finding_key(f)[:2] == key[:2] and f["currency"] == key[3]),
                               ("currency_mismatch", lambda f: finding_key(f)[:2] == key[:2]),
                               ("affected_ids_mismatch", lambda f: f["type"] == key[0]
                                and finding_key(f)[2:] == key[2:])):
            near = next((f for f in findings if same(f)), None)
            if near is not None:
                break
        subjects = set(issue["affected_ids"])
        if near is None:
            discrepancies.append({"category": "missing_in_verifier", "issue_id": issue.get("issue_id"),
                                  "truth": _row(issue, "expected_amount_minor"),
                                  "suspects": suspects(subjects, ["verifier", "truth"])})
        else:
            findings.remove(near)
            discrepancies.append({"category": category, "issue_id": issue.get("issue_id"),
                                  "truth": _row(issue, "expected_amount_minor"),
                                  "verifier": _row(near, "claimed_amount_minor"),
                                  "suspects": suspects(subjects | set(near["affected_ids"]),
                                                       ["truth", "verifier"])})
    for finding in findings:
        discrepancies.append({"category": "extra_in_verifier", "verifier": _row(finding, "claimed_amount_minor"),
                              "suspects": suspects(set(finding["affected_ids"]),
                                                   ["truth", "verifier", "generator"])})
    return discrepancies


def _row(item: dict, amount: str) -> dict:
    return {"type": item["type"], "affected_ids": sorted(item["affected_ids"]),
            "amount_minor": item[amount], "currency": item["currency"]}


def signature(discrepancies: list[dict]) -> list[tuple]:
    return sorted((d["category"], json.dumps({k: d.get(k) for k in ("truth", "verifier", "refs", "ids")},
                                             sort_keys=True)) for d in discrepancies)


# ---------- counterexample reduction (works on copies only) ----------

def _read_units(public: Path) -> dict:
    with (public / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        header, ledger = reader.fieldnames, list(reader)
    with (public / "fx.csv").open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fx_header, fx = reader.fieldnames, list(reader)
    docs = {t: [l for l in (public / f"{t}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
            for t in ("invoices", "payments")}
    return {"header": header, "ledger": ledger, "fx_header": fx_header, "fx": fx, **docs}


def _write_units(source: Path, target: Path, units: dict):
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    for name, header, rows in (("ledger.csv", units["header"], units["ledger"]),
                               ("fx.csv", units["fx_header"], units["fx"])):
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        (target / name).write_text(buffer.getvalue(), encoding="utf-8", newline="")
    for table in ("invoices", "payments"):
        (target / f"{table}.jsonl").write_text("".join(l + "\n" for l in units[table]), encoding="utf-8",
                                               newline="")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    manifest["ledger_rows"] = len(units["ledger"])
    manifest["document_count"] = len(units["invoices"]) + len(units["payments"])
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8", newline="")


def reduce_counterexample(public_dir: Path, truth: dict, out_dir: Path, engine: str | None = None) -> dict:
    """Greedy delta reduction: drop whole journals, documents and FX rows while the discrepancy
    signature is unchanged. The original snapshot is only read."""
    public_dir = Path(public_dir)
    original = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(public_dir.iterdir()) if p.is_file())).hexdigest()
    target = signature(compare(verify_report(public_dir, engine), truth, public_dir))
    if not target:
        raise ValueError("no discrepancy to reduce")
    units = _read_units(public_dir)
    work = Path(tempfile.mkdtemp(prefix="auditlab-reduce-"))
    runs = 0

    def holds(candidate: dict) -> bool:
        nonlocal runs
        runs += 1
        probe = work / "public"
        _write_units(public_dir, probe, candidate)
        try:
            result = compare(verify_report(probe, engine), truth, probe)
        except Exception:  # an invalid reduction is simply rejected
            return False
        return signature(result) == target

    groups = [("ledger", lambda units, j: [r for r in units["ledger"] if (r.get("journal_id") or r["row_id"]) != j],
               lambda units: sorted({r.get("journal_id") or r["row_id"] for r in units["ledger"]})),
              ("invoices", lambda units, d: [l for l in units["invoices"] if l != d], lambda units: list(units["invoices"])),
              ("payments", lambda units, d: [l for l in units["payments"] if l != d], lambda units: list(units["payments"])),
              ("fx", lambda units, r: [x for x in units["fx"] if x != r], lambda units: list(units["fx"]))]
    changed = True
    while changed:
        changed = False
        for table, drop, items in groups:
            for item in items(units):
                candidate = {**units, table: drop(units, item)}
                if holds(candidate):
                    units, changed = candidate, True
    reduced = Path(out_dir) / "reduced" / "public"
    _write_units(public_dir, reduced, units)
    shutil.rmtree(work, ignore_errors=True)
    (Path(out_dir) / "reduced" / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n",
                                                          encoding="utf-8")
    after = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(public_dir.iterdir()) if p.is_file())).hexdigest()
    if after != original:
        raise RuntimeError("reduction modified the original snapshot")
    return {"reduced_public": str(reduced), "ledger_rows": len(units["ledger"]),
            "documents": len(units["invoices"]) + len(units["payments"]), "fx_rows": len(units["fx"]),
            "verifier_runs": runs, "signature": target,
            "command": f"python -m auditlab.verifier.crosscheck --public {reduced} "
                       f"--truth {reduced.parent / 'truth.json'} --out <new-dir> --no-reduce"}


def crosscheck(public_dir: Path, truth_path: Path, out_dir: Path, engine: str | None = None,
               reduce: bool = True) -> dict:
    public_dir, truth_path, out_dir = Path(public_dir), Path(truth_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    # Step 1: fix the independent answer before truth is touched.
    report = verify_report(public_dir, engine)
    answer = _canonical(report)
    (out_dir / "answer.json").write_bytes(answer)
    answer_sha = hashlib.sha256(answer).hexdigest()
    # Step 2: only now read truth.
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    discrepancies = compare(report, truth, public_dir)
    result = {"episode_id": report["episode_id"], "engine": report["engine"],
              "engine_version": report["engine_version"], "rules_version": report["rules_version"],
              "answer_sha256": answer_sha, "input_sha256": report["input_sha256"],
              "truth_sha256": hashlib.sha256(truth_path.read_bytes()).hexdigest(),
              "status": "agree" if not discrepancies else "disagree",
              "findings": len(report["findings"]), "truth_issues": len(truth.get("issues", [])),
              "discrepancies": discrepancies, "notes": report["notes"]}
    if discrepancies and reduce:
        result["counterexample"] = reduce_counterexample(public_dir, truth, out_dir, engine)
    (out_dir / "crosscheck.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditlab.verifier.crosscheck")
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="new directory (never overwritten)")
    parser.add_argument("--engine", choices=("duckdb", "sqlite"))
    parser.add_argument("--no-reduce", action="store_true")
    args = parser.parse_args(argv)
    result = crosscheck(args.public, args.truth, args.out, args.engine, not args.no_reduce)
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return 0 if result["status"] == "agree" else 1


if __name__ == "__main__":
    raise SystemExit(main())
