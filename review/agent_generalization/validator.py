"""validate_candidate(snapshot_ref, protocol_config) -> validation  (CONTRACTS.md, Claude Pro 2).

Independent of generator/matcher/verifier calculations: the answer comes from the reviewer's own
calculator (review/genesis_acceptance/independent_calc.py) and is written before truth is read.
Solvability is shown by a real witness trajectory through judge_adapter: only cursor listings
(no prior knowledge of IDs), then a submit, inside 100 actions and 60 s.
"""
from __future__ import annotations

import csv, hashlib, io, json, re, shutil, subprocess, time
from datetime import datetime
from pathlib import Path

from review.genesis_acceptance.independent_calc import audit

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ID = "agent-generalization-1.1"
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$")
RATE = re.compile(r"^\d+(\.\d+)?$")
K = lambda f, a: (f["type"], tuple(sorted(f["affected_ids"])), f[a], f["currency"])
TABLES = ("policy", "fx", "payments", "invoices", "ledger")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _source_sha() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def clean_variant(error_episode: Path, out: Path) -> Path:
    """Matched clean variant of a family: the error snapshot's own source book, with an empty truth."""
    src = error_episode / "private" / "source_book"
    if not src.is_dir():
        raise FileNotFoundError("no private/source_book: matched clean variant unavailable")
    out.mkdir(parents=True)
    shutil.copytree(src, out / "public")
    eid = json.loads((out / "public" / "manifest.json").read_text(encoding="utf-8"))["episode_id"]
    (out / "private").mkdir()
    (out / "private" / "truth.json").write_text(json.dumps({"episode_id": eid, "issues": []}), encoding="utf-8", newline="\n")
    shutil.copytree(src, out / "private" / "source_book")  # its own clean book
    (out / "private" / "source_transition.json").write_text(json.dumps({"changes": []}), encoding="utf-8", newline="\n")
    return out


def formats(public: Path) -> list[str]:
    bad = []
    for r in csv.DictReader(io.StringIO((public / "ledger.csv").read_text(encoding="utf-8"))):
        if not TS.match(r["timestamp"]): bad.append(f"ledger timestamp {r['row_id']}")
        if not re.fullmatch(r"-?\d+", r["debit_minor"]) or not re.fullmatch(r"-?\d+", r["credit_minor"]): bad.append(f"ledger money {r['row_id']}")
        if any(r[k] == "" for k in ("invoice_id", "payment_id", "entity_id")): bad.append(f"ledger empty link {r['row_id']}")
    for t, keys in (("invoices", ("issued_at", "due_at")), ("payments", ("timestamp",))):
        for line in (public / f"{t}.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            bad += [f"{t} {k} {d.get('doc_id')}" for k in keys if not TS.match(str(d.get(k)))]
            bad += [f"{t} money {d.get('doc_id')}" for k in ("total_minor", "amount_minor") if k in d and type(d[k]) is not int]
    for r in csv.DictReader(io.StringIO((public / "fx.csv").read_text(encoding="utf-8"))):
        if not RATE.match(r["rate"]): bad.append(f"fx rate {r['date']}")
    return bad[:20]


def transition(priv: Path, public: Path) -> dict:
    src = priv / "source_book"
    if not src.is_dir():
        return {"status": "not_checked", "reason": "no private/source_book"}

    def table(folder, name):
        fname, key = {"ledger": ("ledger.csv", "row_id"), "invoices": ("invoices.jsonl", "doc_id"), "payments": ("payments.jsonl", "doc_id")}[name]
        if fname.endswith(".csv"):
            rows = [{k: (int(v) if k in ("debit_minor", "credit_minor") else (None if v == "null" else v)) for k, v in r.items()}
                    for r in csv.DictReader(io.StringIO((folder / fname).read_text(encoding="utf-8")))]
        else:
            rows = [json.loads(l) for l in (folder / fname).read_text(encoding="utf-8").splitlines() if l.strip()]
        return {r[key]: r for r in rows}
    norm = lambda r: None if r is None else {k: (None if v == "null" else v) for k, v in r.items()}
    tr = json.loads((priv / "source_transition.json").read_text(encoding="utf-8"))
    expected = {(e["table"], e["id"]): (norm(e["before"]), norm(e["after"])) for e in (tr.get("changes", []) if isinstance(tr, dict) else tr)}
    actual = {}
    for name in ("ledger", "invoices", "payments"):
        a, b = table(src, name), table(public, name)
        for i in set(a) | set(b):
            if norm(a.get(i)) != norm(b.get(i)):
                actual[(name, i)] = (norm(a.get(i)), norm(b.get(i)))
    fx_same = sorted((src / "fx.csv").read_text(encoding="utf-8").splitlines()) == sorted((public / "fx.csv").read_text(encoding="utf-8").splitlines())
    pol_same = json.loads((src / "policy.json").read_text(encoding="utf-8")) == json.loads((public / "policy.json").read_text(encoding="utf-8"))
    ok = actual.keys() == expected.keys() and all(expected[k] == actual[k] for k in actual) and fx_same and pol_same
    return {"status": "passed" if ok else "failed", "changed": len(actual), "entries": len(expected),
            "unexplained": sorted(map(list, set(actual) - set(expected)))[:5], "phantom": sorted(map(list, set(expected) - set(actual)))[:5],
            "fx_same": fx_same, "policy_same": pol_same}


def witness(adapter, episode: Path, truth: dict) -> dict:
    """Discovery by cursor listings only, then an oracle submit, through the jury adapter."""
    env = adapter.make_env()
    t0 = time.monotonic()
    obs, info = env.reset(0, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
    seen, actions = set(), 0
    for table in TABLES:
        cursor = None
        while True:
            o, r, term, trunc, inf = env.step({"name": "list_records", "args": {"table": table, "cursor": cursor, "limit": 50}})
            actions += 1
            if o.get("error") or term or trunc:
                return {"status": "failed", "reason": f"listing {table} failed: {o.get('error')}", "actions": actions}
            for rec in o["result"]["records"]:
                if table == "ledger": seen.add("ledger/" + rec["row_id"])
                elif table in ("invoices", "payments"): seen.add(f"{table}/" + rec["doc_id"])
                elif table == "fx": seen.add(f"fx/{rec['date']}/{rec['source_currency']}/{rec['target_currency']}")
                elif table == "policy": seen.add("policy/" + str(rec.get("rule", rec.get("name", rec.get("id", "")))))
            cursor = o["result"]["next_cursor"]
            if cursor is None:
                break
    needed = {ref for i in truth["issues"] for g in i["evidence_sets"][:1] for ref in g}
    discovered = needed <= seen  # every cited key was returned by a listing (policy rules are listed by "id")
    findings = [{"type": i["type"], "affected_ids": i["affected_ids"], "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
                 "evidence_ids": i["evidence_sets"][0], "explanation": "witness"} for i in truth["issues"]]
    o, r, term, trunc, inf = env.step({"name": "submit", "args": {"findings": {"episode_id": truth["episode_id"], "findings": findings}}})
    elapsed = time.monotonic() - t0
    score = adapter.score_episode(str(episode), {"episode_id": truth["episode_id"], "findings": findings}, actions)
    ok = discovered and term and not trunc and actions + 1 <= 100 and elapsed < 60 and score.get("fn") == 0 and score.get("fp") == 0
    return {"status": "passed" if ok else "failed", "listing_actions": actions, "submit_action": actions + 1, "elapsed_s": round(elapsed, 3),
            "evidence_discovered": discovered, "missing": sorted(needed - seen)[:5], "score": score}


def validate_candidate(snapshot_ref, protocol_config: dict | None = None, *, adapter=None, work: Path | None = None) -> dict:
    episode = Path(snapshot_ref)
    public, priv = episode / "public", episode / "private"
    checks, notes = {}, []
    fmt = formats(public)
    checks["formats"] = {"status": "passed" if not fmt else "failed", "problems": fmt}
    # 1. own answer from public only, written before truth is read
    try:
        answer = audit(public)
    except (KeyError, ValueError, IndexError) as error:  # e.g. a ledger row cites a document that is absent from public
        answer = {"episode_id": None, "findings": [], "arithmetic": [],
                  "notes": [f"public data inconsistent for the reviewer calculation: {type(error).__name__}: {error}"]}
    work = Path(work or episode.parent / (episode.name + ".validation"))
    work.mkdir(parents=True, exist_ok=True)
    ans_path = work / "answer_before_truth.json"
    ans_path.write_text(json.dumps(answer, sort_keys=True, indent=1), encoding="utf-8", newline="\n")
    answer_sha = _sha(ans_path)
    checks["no_contradictions"] = {"status": "passed" if not answer["notes"] else "failed", "notes": answer["notes"]}
    # 2. source book and mutation (trusted side)
    if (priv / "source_book").is_dir():
        clean = audit(priv / "source_book")
        checks["source_book_consistent"] = {"status": "passed" if not clean["findings"] and not clean["notes"] else "failed",
                                            "findings": len(clean["findings"]), "notes": clean["notes"]}
    else:
        checks["source_book_consistent"] = {"status": "not_checked", "reason": "no private/source_book"}
    checks["mutation_explained"] = transition(priv, public)
    # 3. only now: truth
    truth = json.loads((priv / "truth.json").read_text(encoding="utf-8"))
    mine = {K(f, "claimed_amount_minor"): f for f in answer["findings"]}
    theirs = {K(i, "expected_amount_minor"): i for i in truth["issues"]}
    covers = all(any(set(g) <= set(mine[k]["evidence_ids"]) for g in theirs[k]["evidence_sets"]) for k in theirs if k in mine)
    checks["answer_matches_truth"] = {"status": "passed" if mine.keys() == theirs.keys() and covers else "failed",
                                      "only_answer": [list(k) for k in mine.keys() - theirs.keys()], "only_truth": [list(k) for k in theirs.keys() - mine.keys()],
                                      "answer_covers_an_evidence_set": covers}
    if adapter is None:
        checks["evidence_available"] = checks["witness_trajectory"] = {"status": "not_checked", "reason": "judge_adapter not supplied"}
    else:
        env = adapter.make_env()
        env.reset(0, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
        unavailable = []
        for ref in sorted({r for i in truth["issues"] for g in i["evidence_sets"] for r in g}):
            kind, _, rest = ref.partition("/")
            act = {"name": "get_document", "args": {"doc_id": rest}} if kind in ("invoices", "payments") else {"name": "get_record", "args": {"table": kind, "id": rest}}
            o = env.step(act)[0]
            if o.get("error"):
                unavailable.append(ref)
            if o.get("remaining_steps", 1) <= 1:
                env.reset(0, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
        checks["evidence_available"] = {"status": "passed" if not unavailable else "failed", "unavailable": unavailable}
        checks["witness_trajectory"] = witness(adapter, episode, truth)
    statuses = [c["status"] for c in checks.values()]
    overall = "failed" if "failed" in statuses else ("not_checked" if "not_checked" in statuses else "passed")
    manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    return {"schema_version": "1.0", "artifact_id": f"validation-{manifest['episode_id']}", "source_sha": _source_sha(), "protocol_id": PROTOCOL_ID,
            "episode_id": manifest["episode_id"], "status": overall, "checks": checks, "answer_sha256_before_truth": answer_sha,
            "truth_issues": len(truth["issues"]), "answer_findings": len(answer["findings"])}
