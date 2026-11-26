"""Uniform black-box probe of a team's judge_adapter.py (reviewer tool, not product code).

It approximates a subset of the published Genesis checks (C02-C06, C09-C11, C13-C17, C19 and
the score_episode vectors 1, 2, 5, 18, 19, M09) using only the published entry points:
generate_episode, make_env().reset/step, score_episode. It is NOT the jury's harness: fixtures
are the team's own default episodes, pairs (P01-P20), data checks (D01-D20) and the 60 s limit
are not covered. The oracle is reconstructed from the team's truth.json with field-name
heuristics; when that fails the scoring rows are reported as not_checked.

Usage (run in an isolated sandbox, the adapter is untrusted code):
    python3.11 -I adapter_probe.py REPO_DIR OUT_JSON ['[{"team_config": {...}}]']
"""
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2])
EXTRA = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []  # team-documented configs that plant errors
sys.path.insert(0, str(repo))
os.chdir(repo)
results = {"repo": repo.name, "rows": {}, "notes": []}


def row(key, ok, detail=""):
    results["rows"][key] = {"status": "passed" if ok is True else ("failed" if ok is False else "not_checked"),
                            "detail": str(detail)[:300]}


LEAK_WORDS = ("expected_amount", "evidence_sets", "acceptable_evidence", "truth.json", "provenance",
              "issue_id", "\"issues\"")


def leak_scan(obj):
    text = json.dumps(obj, default=str)
    return [w for w in LEAK_WORDS if w in text]


def find(dirpath, name):
    # Prefer the public copy, then the shallowest path (some teams keep source books in private/).
    hits = sorted(Path(dirpath).rglob(name), key=lambda p: ("public" not in p.parts, len(p.parts), str(p)))
    return hits[0] if hits else None


try:
    t0 = time.time()
    import judge_adapter as ja  # noqa: E402
    results["import_s"] = round(time.time() - t0, 2)
except Exception as exc:  # pragma: no cover
    results["fatal"] = f"import judge_adapter failed: {exc!r}"
    out.write_text(json.dumps(results, indent=1))
    sys.exit(0)

work = Path(tempfile.mkdtemp(prefix="probe-"))
episode = None
truth = None
gen_errors = []
for seed in (1, 2, 3, 4, 5, 6, 7, 8):
    for cfg in (*EXTRA, {}, {"team_config": {}}):
        d = work / f"ep-{seed}-{len(cfg)}"
        try:
            ja.generate_episode(seed, cfg, str(d))
        except Exception as exc:
            gen_errors.append(f"seed {seed} cfg {cfg}: {exc!r}"[:200])
            continue
        tf = find(d, "truth.json")
        t = json.loads(tf.read_text()) if tf else None
        issues = t.get("issues", []) if isinstance(t, dict) else (t if isinstance(t, list) else [])
        if episode is None:
            episode, truth = d, issues
        if issues:
            episode, truth = d, issues
            break
    if truth:
        break
results["generate_errors"] = gen_errors[:4]
if episode is None:
    results["fatal"] = "generate_episode failed for default configs"
    out.write_text(json.dumps(results, indent=1))
    sys.exit(0)
results["episode_issues"] = len(truth or [])


def new_env():
    env = ja.make_env()
    obs, info = env.reset(1, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
    return env, obs, info


observations = []
try:
    env, obs, info = new_env()
    observations += [obs, info]
    row("C01_reset_pair_json", isinstance(obs, dict) and json.loads(json.dumps(obs)) == obs and isinstance(info, dict))
    keys_ok = all(k in obs for k in ("task", "manifest", "policy", "remaining_steps")) and obs.get("remaining_steps") == 100
    row("C02_first_observation", keys_ok, sorted(obs))
    man = obs.get("manifest", {})
    need = ("schema_version", "episode_id", "base_currency", "period_start", "period_end", "timezone")
    row("C03_manifest_fields", all(k in man for k in need), [k for k in need if k not in man])
    # C04/C05: paginate ledger
    pages, seen, cursor, steps_used, flags_ok = 0, [], None, 0, True
    first_page = None
    while True:
        o, r, term, trunc, inf = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": cursor, "limit": 50}})
        observations += [o, inf]
        steps_used += 1
        flags_ok &= isinstance(term, bool) and isinstance(trunc, bool)
        res = o.get("result") or {}
        recs = res.get("records", []) if isinstance(res, dict) else []
        if first_page is None:
            first_page = (cursor, recs)
        if len(recs) > 50:
            flags_ok = False
        seen += [json.dumps(x, sort_keys=True) for x in recs]
        pages += 1
        cursor = res.get("next_cursor") if isinstance(res, dict) else None
        if not cursor or pages >= 45:
            break
    row("C04_step_tuple_types", flags_ok and pages > 0, f"pages={pages}")
    dup = len(seen) - len(set(seen))
    results["ledger_rows_seen"] = len(seen)
    row("C05_pagination_complete", pages >= 1 and dup == 0 and len(seen) > 0, f"rows={len(seen)} pages={pages} dup={dup}")
    if pages >= 2:
        # C06: replay second page cursor twice
        o1, *_ = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 50}})
        c2 = (o1.get("result") or {}).get("next_cursor")
        a, *_ = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": c2, "limit": 50}})
        b, *_ = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": c2, "limit": 50}})
        row("C06_stable_cursor", a.get("result") == b.get("result") and a.get("result") is not None)
    else:
        row("C06_stable_cursor", None, "only one ledger page in the default episode")
    before = None
    o, r, *_ = env.step({"name": "calculator", "args": {"expression": "0.1+0.2"}})
    val = o.get("result")
    row("C09_calculator_decimal", str(val) in ("0.3", "0.30") or val == 0.3, val)
    o, r, *_ = env.step({"name": "calculator", "args": {"expression": "__import__('os').listdir('/')"}})
    row("C10_calculator_rejects_code", o.get("error") is not None and o.get("result") in (None, ""), o.get("error"))
    rs = o.get("remaining_steps")
    o, r, *_ = env.step({"name": "get_record", "args": {"table": "ledger", "id": "ZZ-NOT-A-REAL-ID-9q"}})
    row("C11_unknown_id_bounded_error", o.get("error") is not None and o.get("remaining_steps") == rs - 1, (o.get("error"), o.get("remaining_steps"), rs))
    rs = o.get("remaining_steps")
    o, r, *_ = env.step({"name": "drop_tables", "args": {}})
    row("C13_unknown_action_costs_step", o.get("error") is not None and o.get("remaining_steps") == rs - 1, (o.get("error"), o.get("remaining_steps")))
    rewards = []
    eid = man.get("episode_id")
    o, r, term, trunc, inf = env.step({"name": "submit", "args": {"findings": {"episode_id": eid, "findings": []}}})
    rewards.append(r)
    observations += [o, inf]
    row("C15_submit_terminates", term is True and trunc is False, (term, trunc, o.get("result")))
    row("C19_closed_reward_zero", r == 0 and not isinstance(r, bool), r)
    o2, r2, term2, trunc2, inf2 = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 5}})
    row("C15b_after_finish_no_change", r2 == 0 and term2 is not None, (o2.get("error"), r2, term2, trunc2))
    # C16: 99 + submit
    env, obs, info = new_env()
    for _ in range(99):
        o, r, term, trunc, inf = env.step({"name": "calculator", "args": {"expression": "1+1"}})
    o, r, term, trunc, inf = env.step({"name": "submit", "args": {"findings": {"episode_id": eid, "findings": []}}})
    row("C16_submit_as_action_100", term is True and trunc is False, (term, trunc, o.get("error")))
    # C17: 100 exploratory
    env, obs, info = new_env()
    term = trunc = False
    for i in range(100):
        o, r, term, trunc, inf = env.step({"name": "calculator", "args": {"expression": "1+1"}})
        if term or trunc:
            break
    row("C17_100_actions_truncate", trunc is True and i == 99, (i, term, trunc))
    leaks = leak_scan(observations)
    row("C20_no_truth_words_in_policy_channel", not leaks, leaks)
except Exception as exc:
    results["notes"].append("contract probe crashed: " + "".join(traceback.format_exception_only(type(exc), exc)).strip()[:300])

# ---- score_episode vectors with a reconstructed oracle
def oracle_findings(issues):
    fs = []
    for it in issues:
        sets = (it.get("acceptable_evidence_sets") or it.get("allowed_evidence_sets") or it.get("evidence_sets")
                or it.get("evidence") or [])
        ev = list(sets[0]) if sets and isinstance(sets[0], list) else list(sets)
        fs.append({"type": it.get("type"), "affected_ids": list(it.get("affected_ids", [])),
                   "claimed_amount_minor": it.get("expected_amount_minor"), "currency": it.get("currency"),
                   "evidence_ids": ev[:10], "explanation": "oracle"})
    return fs


def close(a, b):
    return a is not None and abs(a - b) <= 1e-9


try:
    eid = json.loads(find(episode, "manifest.json").read_text()).get("episode_id")
    if truth:
        F = oracle_findings(truth)
        n = len(F)
        s = ja.score_episode(str(episode), {"episode_id": eid, "findings": F}, 10)
        row("R1_oracle_full_credit", s.get("tp") == n and s.get("fp") == 0 and close(s.get("return"), max(-1, min(1, 1 - 0.02))), s)
        s = ja.score_episode(str(episode), {"episode_id": eid, "findings": []}, 10)
        row("R2_silence_on_errors", s.get("tp") == 0 and s.get("fn") == n and close(s.get("return"), max(-1.0, -1 - 0.02)), s)
        s = ja.score_episode(str(episode), {"episode_id": eid, "findings": F + F[:1]}, 10)
        exp_f1 = 2 * n / (2 * n + 1)
        row("R5_duplicate_is_fp", s.get("tp") == n and s.get("fp") == 1 and close(s.get("return"), max(-1, min(1, 2 * exp_f1 - 1 - 0.02))), s)
        bad = json.loads(json.dumps(F[:1])); bad[0]["claimed_amount_minor"] = (bad[0]["claimed_amount_minor"] or 0) + 1
        s = ja.score_episode(str(episode), {"episode_id": eid, "findings": bad}, 10)
        row("R8_amount_plus_one_is_fp", s.get("tp") == 0 and s.get("fp") == 1, s)
        bad = json.loads(json.dumps(F[:1])); bad[0]["evidence_ids"] = bad[0]["evidence_ids"] + ["ZZ-NOT-A-REAL-EVIDENCE"]
        s = ja.score_episode(str(episode), {"episode_id": eid, "findings": bad}, 10)
        row("M04_unknown_extra_evidence_is_fp", s.get("tp") == 0 and s.get("fp") == 1, s)
        bad = json.loads(json.dumps(F[:1])); bad[0]["currency"] = " " + str(bad[0]["currency"]).lower()
        s = ja.score_episode(str(episode), {"episode_id": eid, "findings": bad}, 10)
        row("M06_currency_exact_string", s.get("tp") == 0 and s.get("fp") == 1, s)
        bad = json.loads(json.dumps(F[:1])); bad[0]["claimed_amount_minor"] = True
        s = ja.score_episode(str(episode), {"episode_id": eid, "findings": bad}, 10)
        row("M09_bool_amount_invalid", s.get("return") == -1 and s.get("tp") is None, s)
        bad = json.loads(json.dumps(F[:1])); bad[0]["type"] = "not_a_known_type"
        s = ja.score_episode(str(episode), {"episode_id": eid, "findings": bad}, 10)
        row("X07_unknown_type_is_fp_not_invalid", s.get("tp") == 0 and s.get("fp") == 1, s)
    else:
        for k in ("R1_oracle_full_credit", "R2_silence_on_errors", "R5_duplicate_is_fp"):
            row(k, None, "default episodes had no issues")
    many = [{"type": "x", "affected_ids": ["a"], "claimed_amount_minor": 1, "currency": "KZT", "evidence_ids": [], "explanation": ""}] * 31
    s = ja.score_episode(str(episode), {"episode_id": eid, "findings": many}, 10)
    row("R18_31_findings_invalid", s.get("return") == -1 and s.get("tp") is None, s)
    long = [{"type": "x", "affected_ids": ["a"], "claimed_amount_minor": 1, "currency": "KZT", "evidence_ids": [], "explanation": "a" * 1001}]
    s = ja.score_episode(str(episode), {"episode_id": eid, "findings": long}, 10)
    row("R20_explanation_1001_invalid", s.get("return") == -1 and s.get("tp") is None, s)
except Exception as exc:
    results["notes"].append("score probe crashed: " + "".join(traceback.format_exception_only(type(exc), exc)).strip()[:300])

st = [v["status"] for v in results["rows"].values()]
results["summary"] = {k: st.count(k) for k in ("passed", "failed", "not_checked")}
out.write_text(json.dumps(results, indent=1, default=str))
print(json.dumps(results["summary"]))
