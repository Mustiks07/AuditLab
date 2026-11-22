"""External check of ID renaming and record reordering against the published jury interface.

    python -m review.jury_compatibility_v3.check_ids --episodes datasets/open --output artifacts/jury-v3/ids

For every open episode and every transformer profile (external_transform.py, written from the page only):

1. ``links``     — own reader: every public reference resolves inside the transformed copy
                   (ledger → invoices/payments, invoice_refs, settlement_index rows,
                   allocation_instructions, journal groups) and every truth reference exists.
2. ``evidence``  — every truth evidence key is served by ``get_record`` through ``judge_adapter``.
3. ``oracle``    — the truth-derived (oracle) answer scores TP/FP/FN/return identically through
                   ``judge_adapter.score_episode`` and maps back to the original answer.
4. ``audit``     — the result of auditing public data is preserved:
                   a) residuals computed by the documented settlement index method (own code);
                   b) the built-in ``reconcile`` policy (a public-tools auditor) gives the same score.
5. ``replay``    — the original episode replays: two resets with the same actions give identical
                   observations (pass condition 1 of P01–P20).

The checker never imports auditlab/release/transforms.py or the curriculum transformer. It uses
``judge_adapter`` exactly like an organiser would; ``reconcile`` is used only as an auditor.
Statuses: passed / failed / not_checked. Exit 0 when the run completed (failures are findings).
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from review.jury_compatibility_v3 import external_transform as ext  # noqa: E402

PROFILES = [
    {"name": "reorder", "profile": "reorder", "style": "grammar", "truth_mode": "composite", "reorder": True},
    {"name": "discover-grammar", "profile": "discover", "style": "grammar", "truth_mode": "composite", "reorder": False},
    {"name": "discover-grammar+reorder", "profile": "discover", "style": "grammar", "truth_mode": "composite", "reorder": True},
    {"name": "discover-neutral+reorder", "profile": "discover", "style": "neutral", "truth_mode": "composite", "reorder": True},
    {"name": "discover-numeric+reorder", "profile": "discover", "style": "numeric", "truth_mode": "composite", "reorder": True},
    {"name": "literal+reorder", "profile": "literal", "style": "grammar", "truth_mode": "composite", "reorder": True},
    {"name": "literal-opaque-truth", "profile": "literal", "style": "grammar", "truth_mode": "opaque", "reorder": False},
]


GENERATED = {  # seed 11, judge_adapter.generate_episode; reproducible byte for byte
    "gen-split-refund": {"config_version": "1.1", "issue_counts": {"payment_allocation": 1},
                         "clean_distractors": ["split_payment", "refund"]},
    "gen-archive-duplicate": {"config_version": "1.1", "issue_counts": {"duplicate_invoice": 1},
                              "clean_distractors": ["archive_copy"]},
    "gen-allocation-fx": {"config_version": "1.1", "currencies": ["KZT", "USD"], "interaction": "allocation_fx_same_payment",
                          "issue_counts": {"payment_allocation": 1, "fx_conversion": 1}},
    "gen-cutoff-fx": {"config_version": "1.1", "currencies": ["KZT", "USD"], "interaction": "cutoff_fx_same_invoice",
                      "issue_counts": {"period_cutoff": 1, "fx_conversion": 1}},
}


# ---------------------------------------------------------------------- own public reader
def load(public: Path) -> dict:
    _, ledger = ext.read_csv(public / "ledger.csv")
    _, fx = ext.read_csv(public / "fx.csv")
    return {"ledger": ledger, "invoices": ext.read_jsonl(public / "invoices.jsonl"),
            "payments": ext.read_jsonl(public / "payments.jsonl"), "fx": fx,
            "policy": json.loads((public / "policy.json").read_text(encoding="utf-8")),
            "manifest": json.loads((public / "manifest.json").read_text(encoding="utf-8"))}


def keys(data: dict) -> set[str]:
    out = {f"ledger/{r['row_id']}" for r in data["ledger"]}
    out |= {f"invoices/{r['doc_id']}" for r in data["invoices"]} | {f"payments/{r['doc_id']}" for r in data["payments"]}
    out |= {f"fx/{r['date']}/{r['source_currency']}/{r['target_currency']}" for r in data["fx"]}
    out |= {f"policy/{name}" for name in data["policy"]}
    out |= {f"invoice/{r['invoice_id']}" for r in data["invoices"]} | {f"payment/{r['payment_id']}" for r in data["payments"]}
    return out


def link_problems(data: dict, truth: dict) -> list[str]:
    invoices = {r["invoice_id"] for r in data["invoices"]}
    payments = {r["payment_id"] for r in data["payments"]}
    rows = {r["row_id"] for r in data["ledger"]}
    problems = []
    for r in data["ledger"]:
        if r.get("invoice_id") not in ("", "null", None) and r["invoice_id"] not in invoices:
            problems.append(f"ledger/{r['row_id']}.invoice_id → missing invoice {r['invoice_id']}")
        if r.get("payment_id") not in ("", "null", None) and r["payment_id"] not in payments:
            problems.append(f"ledger/{r['row_id']}.payment_id → missing payment {r['payment_id']}")
    for p in data["payments"]:
        for ref in p.get("invoice_refs") or []:
            if ref not in invoices:
                problems.append(f"payments/{p['doc_id']}.invoice_refs → missing invoice {ref}")
        for row in (p.get("settlement_index") or {}).get("unapplied_row_ids") or []:
            if row not in rows:
                problems.append(f"payments/{p['doc_id']}.settlement_index.unapplied_row_ids → missing ledger row {row}")
        for item in p.get("allocation_instructions") or []:
            if item.get("invoice_id") not in invoices:
                problems.append(f"payments/{p['doc_id']}.allocation_instructions → missing invoice {item.get('invoice_id')}")
    known = keys(data)
    for issue in truth.get("issues", []):
        for ref in issue["affected_ids"] + [r for s in issue["evidence_sets"] for r in s]:
            if ref not in known:
                problems.append(f"truth {issue['issue_id']} → unknown reference {ref}")
    return problems


def journal_shape(data: dict) -> Counter:
    groups = Counter(r.get("journal_id") for r in data["ledger"])
    return Counter(groups.values())


def index_residuals(data: dict) -> list[tuple]:
    """Documented method (policy/allocation): sum UNAPPLIED credits − debits over settlement_index rows."""
    by_row = {r["row_id"]: r for r in data["ledger"]}
    out = []
    for p in data["payments"]:
        index = p.get("settlement_index")
        if not index:
            continue
        rows = [by_row.get(r) for r in index.get("unapplied_row_ids") or []]
        if any(r is None for r in rows):
            out.append((p["amount_minor"], p["currency"], "index_unresolved"))
            continue
        balance = sum(int(r["credit_minor"]) - int(r["debit_minor"]) for r in rows)
        out.append((p["amount_minor"], p["currency"], balance))
    return sorted(out, key=str)


# ---------------------------------------------------------------------- jury interface
def oracle(truth: dict) -> dict:
    return {"episode_id": truth["episode_id"], "findings": [
        {"type": i["type"], "affected_ids": list(i["affected_ids"]), "claimed_amount_minor": i["expected_amount_minor"],
         "currency": i["currency"], "evidence_ids": list(i["evidence_sets"][0]), "explanation": "oracle"}
        for i in truth.get("issues", [])]}


def score(adapter, episode: Path, findings: dict) -> dict:
    result = adapter.score_episode(str(episode), findings, 10)
    return {k: result.get(k) for k in ("tp", "fp", "fn", "return")}


def evidence_served(adapter, episode: Path, truth: dict) -> list[str]:
    refs = sorted({r for i in truth.get("issues", []) for s in i["evidence_sets"] for r in s})
    missing, env, used = [], None, 100
    for ref in refs:
        if used >= 99:
            env = adapter.make_env()
            env.reset(0, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
            used = 0
        table, _, ident = ref.partition("/")
        observation = env.step({"name": "get_record", "args": {"table": table, "id": ident}})[0]
        used += 1
        if observation.get("error") is not None or observation.get("result") is None:
            missing.append(f"{ref}: {observation.get('error', {}).get('code')}")
    return missing


def replay_identical(adapter, episode: Path) -> bool:
    script = [{"name": "list_records", "args": {"table": t, "cursor": None, "limit": 50}} for t in ("ledger", "payments", "fx")]
    script.append({"name": "calculator", "args": {"expression": "50000-30000"}})
    def run():
        env = adapter.make_env()
        first, _ = env.reset(1, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
        return json.dumps([first] + [env.step(a)[:4] for a in script], sort_keys=True, default=str)
    return run() == run()


def reconcile_score(snapshot: Path, work: Path) -> dict:
    from auditlab.policies import make_policy
    from auditlab.runner import make_env
    env, policy = make_env("train"), make_policy("reconcile", 0)
    observation, _ = env.reset(0, {"snapshot_dir": str(snapshot), "export_dir": str(work), "policy_name": "reconcile"})
    while True:
        observation, _, terminated, truncated, info = env.step(policy.act(observation))
        if terminated or truncated:
            s = info.get("score") or {}
            return {k: s.get(k) for k in ("tp", "fp", "fn", "reward")}


# ---------------------------------------------------------------------- run
def check_episode(adapter, episode: Path, spec: dict, work: Path, seed: int, original: dict) -> dict:
    target = work / spec["name"] / episode.name
    meta = ext.transform(episode, target, profile=spec["profile"], seed=seed, style=spec["style"],
                         truth_mode=spec["truth_mode"], reorder=spec["reorder"])
    data, truth = load(target / "public"), json.loads((target / "private/truth.json").read_text(encoding="utf-8"))
    conditions = {}
    problems = link_problems(data, truth)
    shape_ok = journal_shape(data) == original["journal_shape"]
    conditions["links"] = {"status": "passed" if not problems and shape_ok else "failed",
                           "problems": problems[:8], "problem_count": len(problems), "journal_shape_preserved": shape_ok}
    try:
        missing = evidence_served(adapter, target, truth)
        conditions["evidence"] = {"status": "passed" if not missing else "failed", "unavailable": missing[:8]}
    except Exception as error:  # noqa: BLE001 — loading failure is a finding, not a crash
        conditions["evidence"] = {"status": "failed", "error": f"{type(error).__name__}: {error}"[:300]}
    try:
        transformed = score(adapter, target, oracle(truth))
        back = [(f["type"], sorted(ext.inverse_reference(a, meta["inverse"]) for a in f["affected_ids"]), f["claimed_amount_minor"],
                 f["currency"], sorted(ext.inverse_reference(e, meta["inverse"]) for e in f["evidence_ids"]))
                for f in oracle(truth)["findings"]]
        same_answer = sorted(back, key=str) == original["oracle_answer"]
        conditions["oracle"] = {"status": "passed" if transformed == original["oracle_score"] and same_answer else "failed",
                                "original": original["oracle_score"], "transformed": transformed,
                                "answer_maps_back": same_answer}
    except Exception as error:  # noqa: BLE001
        conditions["oracle"] = {"status": "failed", "error": f"{type(error).__name__}: {error}"[:300]}
    residuals = index_residuals(data)
    try:
        auditor = reconcile_score(target, work / "runs" / spec["name"] / episode.name)
    except Exception as error:  # noqa: BLE001
        auditor = {"error": f"{type(error).__name__}: {error}"[:300]}
    audit_ok = residuals == original["index_residuals"] and auditor == original["reconcile"]
    conditions["audit"] = {"status": "passed" if audit_ok else "failed", "index_residuals_preserved": residuals == original["index_residuals"],
                           "reconcile_original": original["reconcile"], "reconcile_transformed": auditor}
    status = "passed" if all(c["status"] == "passed" for c in conditions.values()) else "failed"
    return {"episode": episode.name, "profile": spec["name"], "renamed_ids": meta["renamed"], "status": status, "conditions": conditions}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=Path, default=ROOT / "datasets/open")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="episodes per run (0 = all)")
    parser.add_argument("--generate", action="store_true",
                        help="also generate four profile-1.1 episodes (interactions, distractors) via judge_adapter")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    import judge_adapter as adapter
    episodes = sorted(p.parent.parent for p in args.episodes.glob("*/*/private/truth.json"))
    if args.limit:
        episodes = episodes[:args.limit]
    if args.generate:
        generated = args.output / "generated"
        for name, config in GENERATED.items():
            if not (generated / name / "private/truth.json").exists():
                adapter.generate_episode(11, config, str(generated / name))
            episodes.append(generated / name)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    rows, replays = [], {}
    with tempfile.TemporaryDirectory(prefix="jury-v3-ids-") as temporary:
        work = Path(temporary)
        for n, episode in enumerate(episodes):
            truth = json.loads((episode / "private/truth.json").read_text(encoding="utf-8"))
            data = load(episode / "public")
            original = {"journal_shape": journal_shape(data), "index_residuals": index_residuals(data),
                        "oracle_score": score(adapter, episode, oracle(truth)),
                        "oracle_answer": sorted([(f["type"], sorted(f["affected_ids"]), f["claimed_amount_minor"], f["currency"],
                                                  sorted(f["evidence_ids"])) for f in oracle(truth)["findings"]], key=str),
                        "reconcile": reconcile_score(episode, work / "runs" / "original" / episode.name)}
            replays[episode.name] = replay_identical(adapter, episode)
            for spec in PROFILES:
                rows.append(check_episode(adapter, episode, spec, work, 7000 + n, original))
                print(f"{episode.parent.name}/{episode.name[:22]} {spec['name']}: {rows[-1]['status']}", flush=True)
    summary = {}
    for spec in PROFILES:
        mine = [r for r in rows if r["profile"] == spec["name"]]
        summary[spec["name"]] = {"passed": sum(r["status"] == "passed" for r in mine), "total": len(mine),
                                 "by_condition": {c: sum(r["conditions"][c]["status"] == "passed" for r in mine)
                                                  for c in ("links", "evidence", "oracle", "audit")}}
    report = {"suite": "jury-compatibility-v3/id-transform", "not_an_official_score": True, "commit": sha,
              "python": platform.python_version(), "episodes": len(episodes),
              "finished_utc": datetime.now(timezone.utc).isoformat(),
              "definitions": {"url": "https://genesis-exe.com/docs/fintech-checks.txt",
                              "sha256": "2a597ebfb94daec700ba9b1b09d62c032366e788c6e992486d444e063be8d870"},
              "replay_identical": {"passed": sum(replays.values()), "total": len(replays)},
              "summary": summary, "rows": rows}
    (args.output / "id-transform.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"summary": summary, "replay_identical": report["replay_identical"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
