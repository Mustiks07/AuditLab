"""validate_candidate(snapshot_ref, protocol_config) -> validation  (CONTRACTS.md, Claude Pro 2).

Independent of generator/matcher/verifier calculations: the answer comes from the reviewer's own
calculator (review/genesis_acceptance/independent_calc.py) and is written before truth is read.

Two different claims, kept apart since protocol independent-assurance-2.0:
* `witness_trajectory` (P7, evidence_reachable_within_budget): cursor listings discover every
  cited key inside 100 actions and 60 s. Its submit is built FROM TRUTH, so it is not a solution.
* `independent_solver` (P8, solver.py): an answer built only from the adapter's replies, committed
  before submit and truth, then scored. Unsupported situations are not_checked, never passed.
The passport (P10/P11) binds the result to the exact files and checks regeneration from provenance.
"""
from __future__ import annotations

import csv, hashlib, io, json, re, shutil, subprocess, time
from datetime import datetime
from pathlib import Path

from review.genesis_acceptance.independent_calc import audit

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ID = "independent-assurance-2.0"
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
    return {"status": "passed" if ok else "failed", "claim": "evidence_reachable_within_budget",
            "is_independent_solution": False, "submit_built_from": "truth", "listing_actions": actions, "submit_action": actions + 1, "elapsed_s": round(elapsed, 3),
            "evidence_discovered": discovered, "missing": sorted(needed - seen)[:5], "score": score}


def available_keys(public: Path) -> set[str]:
    """Citation keys that exist in the public files (built here from the files, not by the evaluator)."""
    keys = {"ledger/" + r["row_id"] for r in csv.DictReader(io.StringIO((public / "ledger.csv").read_text(encoding="utf-8")))}
    for t in ("invoices", "payments"):
        keys |= {f"{t}/" + json.loads(l)["doc_id"] for l in (public / f"{t}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    keys |= {f"fx/{r['date']}/{r['source_currency']}/{r['target_currency']}" for r in csv.DictReader(io.StringIO((public / "fx.csv").read_text(encoding="utf-8")))}
    keys |= {"policy/" + k for k in json.loads((public / "policy.json").read_text(encoding="utf-8"))}
    return keys


def declared_amount(m: dict):
    """Amount implied by a provenance mutation record, or None when the record declares none."""
    a, b = m.get("after", {}), m.get("before", {})
    if m["type"] == "payment_allocation":
        return a.get("remaining_minor")
    if m["type"] == "duplicate_invoice" and "recognized_minor" in a and "recognized_minor" in b:
        return a["recognized_minor"] - b["recognized_minor"]
    if m["type"] == "fx_conversion" and "amount_minor" in a and "amount_minor" in b:
        return abs(a["amount_minor"] - b["amount_minor"])
    return None


def mutation_declared(priv: Path, answer: dict) -> dict:
    """P4: the realised change (my answer) matches the mutation declared in provenance."""
    prov_path = priv / "provenance.json"
    if not prov_path.is_file():
        return {"status": "not_checked", "reason": "no private/provenance.json"}
    muts = json.loads(prov_path.read_text(encoding="utf-8")).get("mutations", [])
    mine = {(f["type"], tuple(sorted(f["affected_ids"]))): f for f in answer["findings"]}
    rows, ok = [], len(muts) == len(answer["findings"])
    for m in muts:
        key = (m["type"], tuple(sorted(m["affected_ids"])))
        f, amount = mine.get(key), declared_amount(m)
        good = f is not None and (amount is None or f["claimed_amount_minor"] == amount)
        ok &= good
        rows.append({"type": m["type"], "declared_amount": amount, "realised_amount": f and f["claimed_amount_minor"],
                     "amount_declared": amount is not None, "matches": good})
    return {"status": "passed" if ok else "failed", "declared": len(muts), "realised": len(answer["findings"]), "rows": rows}


def tree_hashes(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): _sha(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def reproducible(episode: Path, scratch: Path) -> dict:
    """P10: regenerate from provenance seed + config (generator as a black box) and compare bytes."""
    prov_path = episode / "private" / "provenance.json"
    if not prov_path.is_file():
        return {"status": "not_checked", "reason": "no provenance"}
    prov = json.loads(prov_path.read_text(encoding="utf-8"))
    if "transform" in prov or "seed" not in prov or "config" not in prov:
        return {"status": "not_checked", "reason": "derived snapshot (transform or reduction): no direct generator call"}
    from auditlab.generator import generate_episode
    if scratch.exists():
        shutil.rmtree(scratch)
    try:
        generate_episode(prov["seed"], prov["config"], scratch)
    except Exception as error:  # the generator refused its own recorded configuration
        return {"status": "failed", "reason": f"regeneration failed: {type(error).__name__}: {error}"}
    a = {k: v for k, v in tree_hashes(episode).items() if k.startswith(("public/", "private/"))}
    b = tree_hashes(scratch)
    diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    note = None
    if "private/provenance.json" in diff:
        # the provenance label of the generator version is not data; every other field must be equal
        pa, pb = prov, json.loads((scratch / "private" / "provenance.json").read_text(encoding="utf-8"))
        va, vb = pa.pop("generator_version", None), pb.pop("generator_version", None)
        if pa == pb:
            diff.remove("private/provenance.json")
            note = f"recorded generator_version {va}, regenerated with {vb}; all data files and other provenance fields identical"
    shutil.rmtree(scratch, ignore_errors=True)
    return {"status": "passed" if not diff else "failed", "files": len(a), "differing": diff[:10], "note": note}


def passport_binding(episode: Path) -> dict:
    return {"public": tree_hashes(episode / "public"), "private": tree_hashes(episode / "private")}


def verify_passport(passport: dict, episode: Path) -> dict:
    """P11: the passport belongs to exactly these files."""
    now = passport_binding(episode)
    diff = {side: sorted(k for k in set(now[side]) | set(passport["binding"][side]) if now[side].get(k) != passport["binding"][side].get(k))
            for side in ("public", "private")}
    return {"status": "passed" if not diff["public"] and not diff["private"] else "failed", "differing": diff}


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
    checks["mutation_declared"] = mutation_declared(priv, answer)
    checks["answer_matches_truth"] = {"status": "passed" if mine.keys() == theirs.keys() and covers else "failed",
                                      "only_answer": [list(k) for k in mine.keys() - theirs.keys()], "only_truth": [list(k) for k in theirs.keys() - mine.keys()],
                                      "answer_covers_an_evidence_set": covers}
    if adapter is None:
        checks["evidence_available"] = checks["witness_trajectory"] = checks["independent_solver"] = {"status": "not_checked", "reason": "judge_adapter not supplied"}
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
        from review.agent_generalization.solver import run_solver
        checks["independent_solver"] = run_solver(adapter, episode, work)
    pc = protocol_config or {}
    checks["reproducible_generation"] = reproducible(episode, work / "regenerated") if pc.get("reproducibility", True) \
        else {"status": "not_checked", "reason": "disabled by protocol_config"}
    statuses = [c["status"] for c in checks.values()]
    overall = "failed" if "failed" in statuses else ("not_checked" if "not_checked" in statuses else "passed")
    manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    return {"schema_version": "1.1", "artifact_id": f"validation-{manifest['episode_id']}", "source_sha": _source_sha(), "protocol_id": PROTOCOL_ID,
            "episode_id": manifest["episode_id"], "status": overall, "checks": checks, "answer_sha256_before_truth": answer_sha,
            "truth_issues": len(truth["issues"]), "answer_findings": len(answer["findings"]), "binding": passport_binding(episode)}
