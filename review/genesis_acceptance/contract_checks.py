"""C01-C20 through the published jury interface of judge_adapter.py (fintech-checks.txt, block 1).

Written against the published contract only: make_env(), reset(seed, {"mode", "episode_dir", "team_config"}),
step({"name", "args"}) and score_episode(). Fixtures C/E/L are produced by adapter.generate_episode;
R1/D1/U are chosen by reading the episode's public files on the reviewer side.
"""
from __future__ import annotations

import csv, json, re, time
from decimal import Decimal
from pathlib import Path

SEED = 987654321  # distinctive, so its appearance in any policy-visible channel is detectable
ACTIONS = ("list_records", "get_record", "get_document", "calculator", "submit")


def _json_ok(value):
    try:
        json.dumps(value, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def _public(episode):
    p = Path(episode) / "public"
    ledger = list(csv.DictReader((p / "ledger.csv").open(encoding="utf-8")))
    docs = [json.loads(l) for t in ("invoices", "payments") for l in (p / f"{t}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return p, ledger, docs


def _secrets(episode):
    """Values that must never reach the policy channel."""
    priv = Path(episode) / "private"
    truth = json.loads((priv / "truth.json").read_text(encoding="utf-8"))
    vals = {str(SEED), str(priv), str(priv).replace("\\", "/")}
    for issue in truth["issues"]:
        vals.add(issue["issue_id"])
    return {v for v in vals if v}, truth


def run(adapter, work: Path, team_configs: dict, skip_slow: bool) -> list[dict]:
    ids = [f"C{n:02d}" for n in range(1, 21)]
    if adapter is None:
        return [{"id": i, "block": "contract", "status": "not_checked", "reason": "judge_adapter.py is not importable at this commit; "
                 "internal AuditEnvironment rejects the official envelope (malformed_action)"} for i in ids]
    res = {}
    eps = {}
    for name, cfg in team_configs.items():
        d = work / f"fixture-{name}"
        adapter.generate_episode(SEED, cfg, str(d))
        eps[name] = d
    C, E, L = eps["C"], eps["E"], eps["L"]
    pub_c, ledger_c, docs_c = _public(C)
    R1, D1 = ledger_c[0]["row_id"], docs_c[0]["doc_id"]
    all_ids = {r["row_id"] for r in ledger_c} | {d["doc_id"] for d in docs_c}
    U = next(f"U{n}" for n in range(10 ** 6) if f"U{n}" not in all_ids)   # chosen after reading the data
    secrets_c, _ = _secrets(C)

    def fresh(episode, mode="closed"):
        env = adapter.make_env()
        obs, info = env.reset(SEED, {"mode": mode, "episode_dir": str(episode), "team_config": team_configs.get("C", {})})
        return env, obs, info

    def check(cid, fn):
        try:
            ok, evidence = fn()
            res[cid] = {"status": "passed" if ok else "failed", "evidence": evidence}
        except Exception as error:  # noqa: BLE001
            res[cid] = {"status": "failed", "evidence": f"{type(error).__name__}: {error}"}

    def step(env, name, **args):
        out = env.step({"name": name, "args": args})
        return out

    def c01():
        env, obs, info = fresh(C)
        return _json_ok(obs) and _json_ok(info), {"obs_keys": sorted(obs), "info": info}
    check("C01", c01)

    def c02():
        env, obs, info = fresh(C)
        text = json.dumps(obs) + json.dumps(info)
        ok = {"task", "manifest", "policy", "remaining_steps"} <= set(obs) and obs["remaining_steps"] == 100 and not any(s in text for s in secrets_c)
        return ok, {"keys": sorted(obs), "remaining": obs.get("remaining_steps")}
    check("C02", c02)

    def c03():
        env, obs, info = fresh(C)
        m = obs["manifest"]
        need = {"schema_version", "episode_id", "base_currency", "period_start", "period_end", "timezone"}
        counts = m.get("ledger_rows") == len(ledger_c) and m.get("document_count") == len(docs_c)
        return need <= set(m) and counts and ("rounding" in m), {"manifest": m}
    check("C03", c03)

    def c04():
        env, obs, info = fresh(C)
        out = step(env, "list_records", table="ledger", cursor=None, limit=10)
        ok = len(out) == 5 and isinstance(out[2], bool) and isinstance(out[3], bool) and _json_ok(list(out))
        return ok, {"types": [type(x).__name__ for x in out], "remaining": out[0].get("remaining_steps")}
    check("C04", c04)

    def c05():
        env, obs, info = fresh(L)
        _, ledger_l, _ = _public(L)
        seen, cursor, pages = [], None, 0
        while True:
            o = step(env, "list_records", table="ledger", cursor=cursor, limit=50)[0]
            recs = o["result"]["records"]; pages += 1
            if len(recs) > 50: return False, {"page_size": len(recs)}
            seen += [r["row_id"] for r in recs]
            cursor = o["result"]["next_cursor"]
            if cursor is None or pages > 60: break
        ok = len(seen) == len(set(seen)) and set(seen) == {r["row_id"] for r in ledger_l}
        return ok, {"rows": len(ledger_l), "pages": pages, "seen": len(seen)}
    check("C05", c05)

    def c06():
        env, obs, info = fresh(L)
        first = step(env, "list_records", table="ledger", cursor=None, limit=50)[0]
        cur = first["result"]["next_cursor"]
        a = step(env, "list_records", table="ledger", cursor=cur, limit=50)[0]["result"]["records"]
        b = step(env, "list_records", table="ledger", cursor=cur, limit=50)[0]["result"]["records"]
        return a == b and len(a) > 0, {"page_len": len(a)}
    check("C06", c06)

    def c07():
        env, obs, info = fresh(C)
        o = step(env, "get_record", table="ledger", id=R1)[0]
        rec = o["result"]
        ok = o.get("error") is None and rec.get("row_id") == R1 and str(rec.get("debit_minor")) == ledger_c[0]["debit_minor"]
        return ok and not any(s in json.dumps(o) for s in secrets_c), {"record": rec}
    check("C07", c07)

    def c08():
        env, obs, info = fresh(C)
        o = step(env, "get_document", doc_id=D1)[0]
        return o.get("error") is None and o["result"] == docs_c[0], {"doc_id": D1}
    check("C08", c08)

    def c09():
        env, obs, info = fresh(C)
        o = step(env, "calculator", expression="0.1+0.2")[0]
        return o.get("error") is None and Decimal(str(o["result"])) == Decimal("0.3"), {"result": o.get("result")}
    check("C09", c09)

    def c10():
        env, obs, info = fresh(C)
        outs = [step(env, "calculator", expression=e)[0] for e in ("open('/etc/passwd').read()", "__import__('os').listdir('.')", "1+(2")]
        leaks = [o for o in outs if "root:" in json.dumps(o) or "manifest.json" in json.dumps(o)]
        return all(o.get("error") for o in outs) and not leaks, {"errors": [o.get("error") for o in outs]}
    check("C10", c10)

    def c11():
        env, obs, info = fresh(C)
        o = step(env, "get_record", table="ledger", id=U)[0]
        o2 = step(env, "get_record", table="ledger", id=R1)[0]
        return o.get("error") is not None and o["remaining_steps"] == 99 and o2.get("error") is None, {"error": o.get("error")}
    check("C11", c11)

    def c12():
        env, obs, info = fresh(C)
        o = step(env, "get_document", doc_id=U)[0]
        return o.get("error") is not None and o["remaining_steps"] == 99 and o.get("result") is None, {"error": o.get("error")}
    check("C12", c12)

    def c13():
        env, obs, info = fresh(C)
        o = step(env, "no_such_action")[0]
        return o.get("error") is not None and o["remaining_steps"] == 99, {"error": o.get("error")}
    check("C13", c13)

    def c14():
        env, obs, info = fresh(C)
        a = step(env, "get_record", table="ledger", id=R1)[0]
        b = step(env, "get_record", table="ledger", id=U)[0]
        c = step(env, "get_record", table="ledger", id=R1)[0]
        return (a["remaining_steps"], b["remaining_steps"], c["remaining_steps"]) == (99, 98, 97) and c["result"] == a["result"], \
            {"remaining": [a["remaining_steps"], b["remaining_steps"], c["remaining_steps"]]}
    check("C14", c14)

    def submit(env, findings):
        return step(env, "submit", findings={"episode_id": json.loads((Path(C) / "public/manifest.json").read_text())["episode_id"], "findings": findings})

    def c15():
        env, obs, info = fresh(C)
        o, r, term, trunc, inf = submit(env, [])
        after = env.step({"name": "get_record", "args": {"table": "ledger", "id": R1}})
        return term is True and trunc is False and (after[2] or after[3] or after[0].get("error")), {"after": after[0]}
    check("C15", c15)

    def c16():
        env, obs, info = fresh(C)
        for _ in range(99):
            step(env, "get_record", table="ledger", id=R1)
        o, r, term, trunc, inf = submit(env, [])
        return term is True and trunc is False and o.get("error") is None, {"result": o.get("result")}
    check("C16", c16)

    def c17():
        env, obs, info = fresh(C)
        last = None
        for _ in range(100):
            last = step(env, "get_record", table="ledger", id=R1)
        after = step(env, "get_record", table="ledger", id=R1)
        return last[3] is True and last[2] is False and (after[2] or after[3]), {"last": last[0].get("remaining_steps")}
    check("C17", c17)

    def c18():
        if skip_slow:
            return None, "skipped (--skip-slow)"
        env, obs, info = fresh(C)
        t0 = time.monotonic()
        time.sleep(60.5)
        o, r, term, trunc, inf = step(env, "get_record", table="ledger", id=R1)
        return trunc is True, {"waited_s": round(time.monotonic() - t0, 2), "truncated": trunc}
    if skip_slow:
        res["C18"] = {"status": "not_checked", "evidence": "skipped with --skip-slow (needs 61 s of wall time)"}
    else:
        check("C18", c18)

    def c19():
        env, obs, info = fresh(E)
        secrets_e, truth = _secrets(E)
        rewards, infos = [], []
        for _ in range(3):
            o, r, term, trunc, inf = step(env, "get_record", table="ledger", id=_public(E)[1][0]["row_id"]); rewards.append(r); infos.append(inf)
        findings = [{"type": i["type"], "affected_ids": i["affected_ids"], "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
                     "evidence_ids": i["evidence_sets"][0], "explanation": "oracle"} for i in truth["issues"]]
        o, r, term, trunc, inf = step(env, "submit", findings={"episode_id": truth["episode_id"], "findings": findings}); rewards.append(r); infos.append(inf)
        text = json.dumps(infos) + json.dumps(o)
        ok = all(type(x) in (int, float) and x == 0 for x in rewards) and not any(s in text for s in secrets_e) and "tp" not in json.dumps(infos)
        return ok, {"rewards": rewards}
    check("C19", c19)

    def c20():
        leaks = []
        for ep in (C, E):
            env, obs, info = fresh(ep)
            secrets, _ = _secrets(ep)
            p, ledger, docs = _public(ep)
            outs = [step(env, "list_records", table=t, cursor=None, limit=50) for t in ("ledger", "invoices", "payments", "fx", "policy", "../private")]
            outs += [step(env, "get_document", doc_id="../private/truth.json"), step(env, "calculator", expression="__import__('subprocess')")]
            text = json.dumps([o[0] for o in outs]) + json.dumps([o[4] for o in outs])
            leaks += [s for s in secrets if s in text]
            leaks += [w for w in ("provenance", "expected_amount_minor", "evidence_sets") if w in text]
        return not leaks, {"leaks": leaks, "note": "a clean run does not prove isolation; see code review notes in the report"}
    check("C20", c20)
    return [{"id": i, "block": "contract", **res.get(i, {"status": "not_checked"})} for i in ids]
