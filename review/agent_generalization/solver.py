"""Independent public-only solver (protocol independent-assurance-2.0, P8).

The witness in validator.py proves only that the evidence keys are reachable within the budget:
its submit is built from truth. This module is the separate claim: an answer built from what the
public adapter returned, committed before submit and before truth, then scored.

`solve(step, first_observation)` sees nothing but the adapter's replies. It lists every public
table by cursor, rebuilds the tables in a scratch directory from the observed records, runs the
reviewer's own calculator on that copy, keeps only evidence keys it observed and returns the
answer. `run_solver` is the trusted controller around it: reset, commit, submit, score, then truth.
"""
from __future__ import annotations

import csv, hashlib, json, tempfile, time
from fractions import Fraction
from pathlib import Path

from review.genesis_acceptance.independent_calc import audit
from review.genesis_acceptance import reference

TABLES = ("policy", "fx", "payments", "invoices", "ledger")
PAGE = 50
MAX_ACTIONS, WALL_SECONDS = 100, 60.0
SUPPORTED = {"payment_allocation", "fx_conversion", "duplicate_invoice", "period_cutoff"}
# calculator notes that mean "situation not modelled", so the solver may not claim a pass
UNMODELLED = ("not modelled", "ambiguous", "several refs", "rates for")


class NotSupported(Exception):
    pass


def _key(table: str, rec: dict) -> str:
    if table == "ledger":
        return "ledger/" + rec["row_id"]
    if table in ("invoices", "payments"):
        return f"{table}/" + rec["doc_id"]
    if table == "fx":
        return f"fx/{rec['date']}/{rec['source_currency']}/{rec['target_currency']}"
    return "policy/" + rec["id"]


def _write_tables(folder: Path, manifest: dict, records: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "policy.json").write_text(json.dumps({r["id"]: r["value"] for r in records["policy"]}), encoding="utf-8")
    for name in ("invoices", "payments"):
        (folder / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records[name]), encoding="utf-8")
    for name in ("ledger", "fx"):
        rows = records[name]
        with (folder / f"{name}.csv").open("w", encoding="utf-8", newline="") as out:
            if not rows:
                out.write({"ledger": "row_id\n", "fx": "date,source_currency,target_currency,rate\n"}[name]); continue
            w = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows({k: ("null" if v is None else v) for k, v in r.items()} for r in rows)


def solve(step, first_observation: dict, *, deadline: float | None = None, max_actions: int = MAX_ACTIONS) -> dict:
    """Answer from adapter observations only. Raises NotSupported when it may not claim a result."""
    manifest = first_observation["manifest"]
    records = {t: [] for t in TABLES}
    observed, actions = set(), 0
    for table in TABLES:
        cursor = None
        while True:
            if actions + 1 >= max_actions:  # keep one action for submit
                raise NotSupported(f"listing does not fit the action budget ({actions} used before {table})")
            if deadline is not None and time.monotonic() > deadline:
                raise NotSupported("listing does not fit the wall-clock budget")
            obs = step({"name": "list_records", "args": {"table": table, "cursor": cursor, "limit": PAGE}})
            actions += 1
            if obs.get("error"):
                raise NotSupported(f"listing {table} failed: {obs['error']}")
            page = obs["result"]
            for rec in page["records"]:
                records[table].append(rec)
                observed.add(_key(table, rec))
            cursor = page["next_cursor"]
            if cursor is None:
                break
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        rebuilt = Path(tmp) / "observed"
        _write_tables(rebuilt, manifest, records)
        try:
            answer = audit(rebuilt)
        except (KeyError, ValueError, IndexError) as error:
            raise NotSupported(f"observed data not handled by the calculator: {type(error).__name__}: {error}")
    unmodelled = [n for n in answer["notes"] if any(m in n for m in UNMODELLED)]
    if unmodelled:
        raise NotSupported("calculator reports an unmodelled situation: " + "; ".join(unmodelled[:3]))
    findings = []
    for f in answer["findings"]:
        if f["type"] not in SUPPORTED:
            raise NotSupported(f"type {f['type']} not supported")
        unseen = [e for e in f["evidence_ids"] if e not in observed]
        if unseen:
            raise NotSupported(f"evidence not observed: {unseen[:3]}")
        if len(f["evidence_ids"]) > reference.MAX_EVIDENCE:
            raise NotSupported(f"{len(f['evidence_ids'])} evidence keys exceed the limit of {reference.MAX_EVIDENCE}")
        findings.append({**{k: f[k] for k in ("type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids")},
                         "explanation": "independent public-only solver"})
    if len(findings) > reference.MAX_FINDINGS:
        raise NotSupported("more findings than the submit limit")
    return {"episode_id": manifest["episode_id"], "findings": findings, "listing_actions": actions,
            "notes": answer["notes"], "arithmetic": answer["arithmetic"], "observed_keys": len(observed)}


def run_solver(adapter, episode: Path, work: Path, *, max_actions: int = MAX_ACTIONS) -> dict:
    """Trusted controller: the solver gets only `step` and the first observation."""
    env = adapter.make_env()
    t0 = time.monotonic()
    first, _ = env.reset(0, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
    step = lambda action: env.step(action)[0]
    try:
        answer = solve(step, first, deadline=t0 + WALL_SECONDS, max_actions=max_actions)
    except NotSupported as why:
        return {"status": "not_checked", "reason": str(why), "claim": "independent public-only solution"}
    work.mkdir(parents=True, exist_ok=True)
    submission = {"episode_id": answer["episode_id"], "findings": answer["findings"]}
    path = work / "solver_answer_before_submit.json"
    path.write_text(json.dumps({**submission, "arithmetic": answer["arithmetic"]}, sort_keys=True, indent=1), encoding="utf-8", newline="\n")
    committed = hashlib.sha256(path.read_bytes()).hexdigest()
    obs, reward, terminated, truncated, info = env.step({"name": "submit", "args": {"findings": submission}})
    elapsed = time.monotonic() - t0
    actions = answer["listing_actions"] + 1
    official = adapter.score_episode(str(episode), submission, answer["listing_actions"])
    # only now does the controller open truth
    truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
    from review.agent_generalization.validator import available_keys
    mine = reference.score(submission["findings"], truth["issues"], answer["listing_actions"], available_keys(episode / "public"))
    exact = mine["valid"] and mine["fp"] == 0 and mine["fn"] == 0
    agree = mine["valid"] and (official.get("tp"), official.get("fp"), official.get("fn")) == (mine["tp"], mine["fp"], mine["fn"]) \
        and Fraction(str(official.get("return"))).limit_denominator(10**6) == mine["return"]
    ok = exact and agree and terminated and not truncated and actions <= MAX_ACTIONS and elapsed < WALL_SECONDS
    return {"status": "passed" if ok else "failed", "claim": "independent public-only solution",
            "answer_sha256_before_submit": committed, "actions": actions, "elapsed_s": round(elapsed, 3),
            "findings": len(submission["findings"]), "official_score": official,
            "reference_score": {k: (str(v) if isinstance(v, Fraction) else v) for k, v in mine.items() if k != "pairs"},
            "official_equals_reference": agree, "terminated": terminated, "truncated": truncated}
