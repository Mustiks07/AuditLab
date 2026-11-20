"""Executable internal 20 x 4 + 20-pair rehearsal. No competition-score claims.

This is trusted orchestration. Ordinary policies use Docker only. A fixed selection is
written before generation or policy execution. Every planned attempt remains in reports.
"""
from __future__ import annotations

from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time
import uuid

from .transforms import comparable, read_json, transform_public, transform_truth, write_json

VERSION = "internal-rehearsal-1.1"
POLICIES = ("random", "reconcile", "flag_all", "oracle")
ISSUE_TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
PUBLIC_FILES = ("manifest.json", "policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv")


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_hashes(directory):
    return {p.relative_to(directory).as_posix(): _hash(p) for p in sorted(Path(directory).rglob("*")) if p.is_file()}


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def selection_manifest(config_version="1.1"):
    """Frozen protocol, deliberately distinct from shipped train/dev seeds and periods.

    Once used for development, these are internal regression cases, not fresh holdout data.
    The full manifest hash is always published; no hidden-seed security claim is made.
    """
    rows = []
    sizes = ((240, 76), (400, 100), (800, 140), (1200, 180), (2000, 200))
    zones = ("Asia/Almaty", "UTC", "America/New_York", "Europe/Berlin", "Asia/Kathmandu")
    for index in range(20):
        counts = {key: 0 for key in ISSUE_TYPES}
        interaction = None
        if 5 <= index < 10:
            interaction = "allocation_fx_same_payment" if index % 2 else "cutoff_fx_same_invoice"
            counts["fx_conversion"] = 1
            counts["payment_allocation" if index % 2 else "period_cutoff"] = 1
        elif 10 <= index < 18:
            counts[ISSUE_TYPES[(index - 10) % 4]] = 1 + (index >= 14)
        elif index == 18:
            counts = dict.fromkeys(ISSUE_TYPES, 1)
        elif index == 19:
            counts = dict.fromkeys(ISSUE_TYPES, 3)
        currency = ("USD", "JPY", "KWD")[index % 3]
        zone = zones[index % len(zones)]
        period = ("2027-02", "2027-05", "2026-03", "2026-10", "2027-08")[index % 5]
        # Two explicit DST transitions with FX, whose offsets differ within the period.
        if index in (12, 16):
            zone, period = "America/New_York", "2026-03" if index == 12 else "2026-11"
        ledger, documents = sizes[index % len(sizes)]
        config = {"config_version": config_version,
                  "company_size": {"ledger_rows_target": ledger, "documents_target": documents},
                  "base_currency": "KZT", "currencies": ["KZT", currency], "timezone": zone,
                  "target_period": period, "issue_counts": counts, "interaction": interaction,
                  "clean_distractors": ["split_payment", "equal_amount_different_supplier", "refund", "archive_copy"]}
        rows.append({"id": f"internal-{index+1:02d}", "seed": 807113 + index * 104729,
                     "category": "clean" if index < 5 else "interaction" if index < 10 else "single_or_mixed",
                     "config": config, "pair_seed": 190081 + index * 7919})
    return {"version": VERSION, "purpose": "internal regression rehearsal, not organizer hidden evaluation",
            "selection_rule": "Fixed before this run; never tune policies on this result and call it fresh holdout.",
            "policy_seed": 314159, "policies": list(POLICIES), "episodes": rows}


def pending_plan():
    reason = "Запланировано; запуск ещё не выполнялся."
    return {"version": VERSION, "status": "in_progress", "datasets": [],
            "episodes": [{"id": f"{policy}/internal-{i:02d}", "episode": f"internal-{i:02d}",
                          "policy": policy, "category": "clean" if i <= 5 else "interaction" if i <= 10 else "single_or_mixed",
                          "status": "not_checked", "detail": reason}
                         for policy in POLICIES for i in range(1, 21)],
            "pairs": [{"id": f"pair-{i:02d}", "episode": f"internal-{i:02d}",
                       "status": "not_checked", "detail": reason} for i in range(1, 21)],
            "summary": {}, "prerequisites": [], "measurements": {"peak_memory_bytes": None}}


def inspect_image(image: str | None) -> dict:
    if not image:
        return {"status": "not_checked", "detail": "Не указан локальный Docker image; небезопасной подмены нет."}
    try:
        process = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=20)
        if process.returncode:
            return {"status": "failed", "detail": f"Локальный image недоступен: {image}; ничего не скачивалось."}
        metadata = json.loads(process.stdout)[0]
        return {"status": "passed", "image": image, "image_id": metadata["Id"], "image_size_bytes": metadata["Size"]}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"status": "failed", "detail": f"Docker prerequisite: {type(error).__name__}: {error}"}


_VERIFIER_SCRIPT = '''import json,sys,tempfile,contextlib,io
from pathlib import Path
from auditlab.verifier import verify_report
payload=json.load(sys.stdin)
with tempfile.TemporaryDirectory() as temporary:
 public=Path(temporary)/"public"; public.mkdir()
 for name,contents in payload.items(): (public/name).write_text(contents,encoding="utf-8",newline="")
 answer=verify_report(public,engine="duckdb")
 counters=io.StringIO()
 with contextlib.redirect_stdout(counters): exec(sys.argv[1], {})
 print(json.dumps({"answer":answer,"resources":json.loads(counters.getvalue())},ensure_ascii=True))
'''


def independent_answer(public: Path, destination: Path, verifier_image: str | None, cpuset: str | None):
    """Record public-only answer before caller loads truth. No truth is sent to the image."""
    destination.mkdir(parents=True, exist_ok=False)
    copied = destination / "public"
    shutil.copytree(public, copied)
    resources = None
    if verifier_image:
        from auditlab.runner.isolation import _CGROUP_PROBE, _parse_cgroup_measurements, _verify_cgroup_scope, launch_label_args
        name = "auditlab-verifier-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "-i", "--pull=never", "--name", name,
                   "--network", "none", "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "--pids-limit", "128",
                   "--cpus", "2", "--memory", "1536m", "--memory-swap", "1536m", "--user", "65534:65534",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"]
        if cpuset:
            command += ["--cpuset-cpus", cpuset]
        command += launch_label_args() + [verifier_image, "python", "-c", _VERIFIER_SCRIPT, _CGROUP_PROBE]
        payload = json.dumps({name: (copied / name).read_text(encoding="utf-8") for name in PUBLIC_FILES})
        if len(payload.encode()) > 8 * 1024 * 1024:
            raise ValueError("public snapshot exceeds verifier transport limit")
        try:
            process = subprocess.run(command, input=payload, capture_output=True, text=True, encoding="utf-8", timeout=60)
            if process.returncode:
                raise RuntimeError(f"isolated verifier exit {process.returncode}: {process.stderr[-2000:]}")
            if len(process.stdout.encode()) > 4 * 1024 * 1024:
                raise ValueError("verifier report exceeds transport limit")
            reply = json.loads(process.stdout)
            answer = reply["answer"]
            resources = _verify_cgroup_scope(_parse_cgroup_measurements(reply["resources"]), reply["resources"],
                {"memory": "1536m", "cpus": "2", "cpuset": cpuset, "pids": 128})
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
        isolation = "docker_public_input_only"
    else:
        from auditlab.verifier import verify_report
        answer = verify_report(copied, engine="duckdb")
        isolation = "host_public_copy_not_os_isolated"
    write_json(destination / "answer.json", answer)
    return answer, {"answer_sha256": _hash(destination / "answer.json"), "backend": isolation,
                    "input_sha256": file_hashes(copied), "resources": resources}


def _by_class(final, truth):
    issues = {item["issue_id"]: item for item in truth["issues"]}
    graded = final.get("graded") or {}
    matched_ids = {pair[1] for pair in graded.get("matches", [])}
    matched_findings = {int(pair[0]) for pair in graded.get("matches", [])}
    body = final.get("submission") or {}
    findings = body.get("findings", []) if isinstance(body, dict) else []
    results = {kind: {"tp": 0, "fp": 0, "fn": 0} for kind in ISSUE_TYPES}
    for issue_id, issue in issues.items():
        results[issue["type"]]["tp" if issue_id in matched_ids else "fn"] += 1
    if isinstance(findings, list):
        for index, finding in enumerate(findings):
            if isinstance(finding, dict) and index not in matched_findings and finding.get("type") in results:
                results[finding["type"]]["fp"] += 1
    return results


def run_attempt(snapshot: Path, output: Path, selection: dict, policy_name: str,
                policy_seed: int, policy_image: str | None, cpuset: str | None):
    from auditlab.runner import make_env
    from auditlab.runner.episode import OraclePolicy, run_episode
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    env = make_env("closed", trusted_dir=output / "trusted")
    policy = None
    row = {"id": f"{policy_name}/{selection['id']}", "episode": selection["id"], "policy": policy_name,
           "status": "failed", "kind": "infrastructure_error", "category": selection["category"],
           "mode": "closed", "metrics": None, "inspection_steps": None, "peak_memory_bytes": None}
    try:
        if policy_name == "oracle":
            policy = OraclePolicy(snapshot)
        else:
            from auditlab.runner.isolation import DockerPolicy
            kwargs = {"name": policy_name, "seed": policy_seed, "timeout_s": 60, "cpus": "2", "memory": "1536m"}
            if cpuset:
                kwargs["cpuset"] = cpuset
            policy = DockerPolicy(policy_image, **kwargs)
        run_episode(env, policy, selection["seed"], {"snapshot_dir": str(snapshot),
                    "policy_name": policy_name, "run_id": "attempt", "time_limit_s": 60})
        if env.final is None:
            raise RuntimeError("runner produced no final record")
        final = copy.deepcopy(env.final)
        write_json(output / "final.json", final)
        row.update(outcome=final["status"], persistence=final.get("persistence", []), infra=final.get("infra", []))
        if final.get("persistence") or final.get("infra"):
            raise RuntimeError("runner final record reports infrastructure or persistence failure")
        trusted = read_json(output / "trusted" / "attempt" / "report.json")
        trajectory_path = output / "trusted" / "attempt" / "trajectory.jsonl"
        trajectory = [json.loads(line) for line in trajectory_path.read_text(encoding="utf-8").splitlines()]
        if any(line["reward"] != 0 for line in trajectory):
            raise RuntimeError("closed trajectory leaked a non-neutral reward")
        good_execution = (final["status"] in {"submitted", "invalid_submission", "truncated_budget", "truncated_timeout"}
                          and not trusted["infra"] and trusted["completed"] is True)
        truth = read_json(snapshot / "private" / "truth.json")
        row.update(status="passed" if good_execution else "failed", kind="measured_attempt" if good_execution else
                   "policy_error" if final["status"] == "policy_failure" else "infrastructure_error",
                   outcome=final["status"], terminated=final["terminated"], truncated=final["truncated"],
                   metrics=(final.get("graded") or {}).get("score"), inspection_steps=final["inspection_steps"],
                   by_class=_by_class(final, truth) if good_execution else None,
                   trajectory_sha256=_hash(trajectory_path), trajectory_steps=len(trajectory),
                   versions=trusted["versions"], true_reward=final["reward"], detail="")
        if policy_name == "oracle" and (not good_execution or final["status"] != "submitted" or
                row["metrics"]["fp"] or row["metrics"]["fn"] or row["metrics"]["tp"] != len(truth["issues"])):
            row.update(status="failed", kind="oracle_correctness", detail="Oracle did not match every reference issue exactly once.")
    except Exception as error:
        row.update(status="failed", kind="infrastructure_error", detail=f"{type(error).__name__}: {error}")
    finally:
        try:
            if policy is not None and hasattr(policy, "close"):
                policy.close()
            env.close()
        except Exception as error:
            row.update(status="failed", kind="infrastructure_error", detail=f"Cleanup failed: {type(error).__name__}: {error}")
        resources = copy.deepcopy(getattr(policy, "measurements", None))
        row["resources"] = resources
        if isinstance(resources, dict):
            row["peak_memory_bytes"] = resources.get("peak_memory_bytes")
    row["elapsed_seconds"] = time.monotonic() - started
    write_json(output / "result.json", row)
    return row


def _score_tuple(row):
    score = row.get("metrics") or {}
    return tuple(score.get(key) for key in ("tp", "fp", "fn", "precision", "recall", "f1", "reward"))


def replay_actions(snapshot, source, destination, selection, source_report=None):
    """Compare public transitions and trusted grading; clocks are measured separately."""
    from auditlab.runner import make_env
    source_report = Path(source_report) if source_report is not None else Path(source).with_name("report.json")
    if not source_report.is_file():
        return {"status": "not_checked", "detail": "Original trusted report is required to verify closed-mode reward."}
    original_report = read_json(source_report)
    expected = [json.loads(line) for line in Path(source).read_text(encoding="utf-8").splitlines()]
    env = make_env("closed", trusted_dir=destination)
    try:
        env.reset(selection["seed"], {"snapshot_dir": str(snapshot), "policy_name": "recorded-replay",
                                      "run_id": "replay", "time_limit_s": 60})
        if not expected or any(row["action"] is None for row in expected):
            return {"status": "not_checked", "detail": "No deterministic complete action sequence to replay."}
        mismatches = []
        for row in expected:
            observation, reward, terminated, truncated, _ = env.step(row["action"])
            actual = (observation, reward, terminated, truncated)
            wanted = tuple(row[key] for key in ("observation", "reward", "terminated", "truncated"))
            if actual != wanted:
                mismatches.append(row["step"])
        if env.final is None:
            mismatches.append("not_finalized")
        trusted_mismatches = []
        if env.final is not None:
            actual_report = read_json(Path(destination) / "replay" / "report.json")
            # Closed public rewards are always zero: public replay alone cannot certify grades.
            # Run IDs, policy names and elapsed time intentionally differ in a fresh replay.
            for key in ("episode_id", "mode", "status", "versions", "seed", "inspection_steps",
                        "evaluation", "infra", "true_reward", "completed"):
                if key not in original_report or original_report[key] != actual_report.get(key):
                    trusted_mismatches.append(key)
            if trusted_mismatches:
                mismatches.append("trusted_report")
        return {"status": "failed" if mismatches else "passed", "steps": len(expected),
                "mismatches": mismatches, "trusted_mismatches": trusted_mismatches,
                "source_sha256": _hash(source), "source_report_sha256": _hash(source_report),
                "scope": "Public transitions and deterministic trusted report fields; elapsed time excluded."}
    finally:
        env.close()


def evidence_addressability(public, truth):
    from auditlab.environment import open_session
    session = open_session(public)
    unknown = []
    refs = sorted({ref for issue in truth["issues"] for group in issue["evidence_sets"] for ref in group})
    for ref in refs:
        table, key = ref.split("/", 1)
        action = ({"action": "get_document", "args": {"doc_id": key}} if table in ("invoices", "payments") else
                  {"action": "get_record", "args": {"table": table, "id": key}})
        if not session.inspect(action)["ok"]:
            unknown.append(ref)
    return {"status": "failed" if unknown else "passed", "references_checked": len(refs), "unknown": unknown,
            "scope": "Known-ID addressability, not discovery by an ordinary policy."}


def run_pair(snapshot, original_answer, original_oracle, destination, selection, verifier_image, cpuset,
             replay_source=None):
    from auditlab.verifier.crosscheck import compare
    started = time.monotonic()
    destination.mkdir(parents=True, exist_ok=False)
    row = {"id": f"pair-{int(selection['id'].split('-')[-1]):02d}", "episode": selection["id"],
           "status": "failed", "kind": "pair_correctness"}
    try:
        transformed = destination / "snapshot"
        mapping = transform_public(snapshot / "public", transformed / "public", selection["pair_seed"])
        write_json(destination / "mapping.json", mapping)
        # Compute and durably record independent answer before reading/mapping the closed reference.
        answer, evidence = independent_answer(transformed / "public", destination / "independent", verifier_image, cpuset)
        truth = transform_truth(read_json(snapshot / "private" / "truth.json"), mapping)
        (transformed / "private").mkdir()
        write_json(transformed / "private" / "truth.json", truth)
        differences = compare(answer, truth, transformed / "public")
        write_json(destination / "discrepancies.json", differences)
        oracle = run_attempt(transformed, destination / "oracle", selection, "oracle", 0, None, cpuset)
        addressability = evidence_addressability(transformed / "public", truth)
        replay = replay_actions(snapshot, replay_source, destination / "replay", selection) if replay_source else {
            "status": "not_checked", "detail": "No original trajectory supplied."}
        same_answer = comparable(original_answer["findings"], mapping["references"]) == comparable(answer["findings"])
        same_score = original_oracle["status"] == oracle["status"] == "passed" and _score_tuple(original_oracle) == _score_tuple(oracle)
        passed = same_answer and same_score and not differences and addressability["status"] == "passed"
        if replay_source:
            passed = passed and replay["status"] == "passed"
        row.update(status="passed" if passed else "failed", replay=replay, addressability=addressability,
                   transformed_answer_agrees=same_answer, oracle_score_agrees=same_score,
                   discrepancies=len(differences), independent=evidence, detail="",
                   original_oracle_score=original_oracle.get("metrics"), transformed_oracle_score=oracle.get("metrics"))
    except Exception as error:
        row.update(kind="infrastructure_error", detail=f"{type(error).__name__}: {error}")
    row["elapsed_seconds"] = time.monotonic() - started
    write_json(destination / "result.json", row)
    return row


def summarize(attempts):
    """Full-plan headline scores require complete evidence; partial scores are labelled explicitly.

    Failed policies retain their diagnostic empty-answer grade in the attempt record, but do not
    receive an invented performance score. Measured-only aggregates must not masquerade as a
    complete-plan result. Coverage and failure kinds always use all planned attempts.
    """
    summary = {}
    for policy in POLICIES:
        rows = [row for row in attempts if row["policy"] == policy]
        measured = [row for row in rows if row["status"] == "passed" and row.get("metrics") is not None]
        counts = {key: sum(row["metrics"][key] for row in measured) for key in ("tp", "fp", "fn")}
        tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
        clean = [row for row in rows if row.get("category") == "clean"]
        clean_measured = [row for row in clean if row["status"] == "passed" and row.get("metrics")]
        totals = {kind: {key: sum((row.get("by_class") or {}).get(kind, {}).get(key, 0) for row in measured)
                         for key in ("tp", "fp", "fn")} for kind in ISSUE_TYPES}
        for total in totals.values():
            total["recall"] = total["tp"] / (total["tp"] + total["fn"]) if total["tp"] + total["fn"] else None
        complete = bool(rows) and len(measured) == len(rows)
        measured_micro = {**counts, "precision": tp / (tp + fp) if tp + fp else 0,
                          "recall": tp / (tp + fn) if tp + fn else 0,
                          "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else (1 if measured else None)}
        measured_macro = statistics.mean(row["metrics"]["f1"] for row in measured) if measured else None
        measured_reward = statistics.mean(row["metrics"]["reward"] for row in measured) if measured else None
        summary[policy] = {"planned": len(rows), "measured": len(measured),
                           "score_status": "complete" if complete else "incomplete",
                           "measurement_coverage": len(measured) / len(rows) if rows else None,
                           "failure_kinds": dict(Counter(row.get("kind", "unspecified") for row in rows if row["status"] == "failed")),
                           "measured_only": {"denominator": len(measured), "micro": measured_micro,
                                             "macro_f1": measured_macro, "mean_reward": measured_reward,
                                             "by_class": totals},
                           "failed": sum(row["status"] == "failed" for row in rows),
                           "not_checked": sum(row["status"] == "not_checked" for row in rows),
                           "outcomes": dict(Counter(row.get("outcome", row["status"]) for row in rows)),
                           "micro": measured_micro if complete else None,
                           "macro_f1": measured_macro if complete else None,
                           "mean_reward": measured_reward if complete else None,
                           "mean_inspection_steps": statistics.mean(row["inspection_steps"] for row in measured) if measured else None,
                           "inspection_steps": [row["inspection_steps"] for row in measured],
                           "elapsed_seconds": [row.get("elapsed_seconds") for row in rows],
                           "policy_peak_memory_bytes": max((row["peak_memory_bytes"] for row in rows
                              if row.get("peak_memory_bytes") is not None), default=None),
                           "clean": {"planned": len(clean), "measured": len(clean_measured),
                                     "false_accusation_episodes": sum(row["metrics"]["fp"] > 0 for row in clean_measured),
                                     "rate_over_measured": sum(row["metrics"]["fp"] > 0 for row in clean_measured) / len(clean_measured) if clean_measured else None},
                           "by_class": totals if complete else None}
    return summary


def execute(output: Path, policy_image: str | None, verifier_image: str | None = None,
            cpuset: str | None = None, progress=None):
    from auditlab.generator import generate_episode, GENERATOR_VERSION
    from auditlab.verifier.crosscheck import compare
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = selection_manifest()
    write_json(output / "selection.json", manifest)
    result = pending_plan()
    result.update(selection_sha256=_hash(output / "selection.json"), generator_version=GENERATOR_VERSION,
                  started_at=datetime.now(timezone.utc).isoformat(), policy_image=policy_image,
                  verifier_image=verifier_image, cpu_set=cpuset, policy_cpu_limit=2, policy_memory_limit_bytes=1536 * 1024**2)
    def persist():
        result["summary"] = summarize(result["episodes"])
        write_json(output / "report.json", result)
        if progress:
            progress(result)
    # Image checks do not pull or build anything. Freeze selection before even these checks.
    policy_ready = inspect_image(policy_image)
    verifier_ready = inspect_image(verifier_image) if verifier_image else {"status": "not_checked", "detail": "Verifier uses a host public copy; OS isolation is not certified."}
    result["prerequisites"] = [{"id": "policy_image", **policy_ready}, {"id": "verifier_image", **verifier_ready}]
    persist()
    for selection in manifest["episodes"]:
        label = selection["id"]
        snapshot = output / "snapshots" / label
        entry = {"id": label, "status": "failed", "kind": "data_correctness", "category": selection["category"]}
        original_answer = None
        try:
            generate_episode(selection["seed"], selection["config"], snapshot)
            hashes = file_hashes(snapshot)
            entry["snapshot_sha256"] = hashes
            repeated = output / "repeated-snapshots" / label
            generate_episode(selection["seed"], selection["config"], repeated)
            entry["regeneration_identical"] = hashes == file_hashes(repeated)
            if verifier_image and verifier_ready["status"] != "passed":
                raise RuntimeError("isolated verifier image is unavailable; no fallback")
            original_answer, evidence = independent_answer(snapshot / "public", output / "independent" / label, verifier_image, cpuset)
            truth = read_json(snapshot / "private" / "truth.json")
            differences = compare(original_answer, truth, snapshot / "public")
            write_json(output / "independent" / label / "discrepancies.json", differences)
            actual_counts = dict(Counter(issue["type"] for issue in truth["issues"]))
            expected_counts = {key: count for key, count in selection["config"]["issue_counts"].items() if count}
            addressability = evidence_addressability(snapshot / "public", truth)
            integrity_notes = [note for note in original_answer.get("notes", []) if note.get("kind") == "integrity"]
            entry.update(status="passed" if not differences and actual_counts == expected_counts and
                         not integrity_notes and entry["regeneration_identical"] and addressability["status"] == "passed" else "failed",
                         addressability=addressability,
                         integrity_notes=integrity_notes,
                         independent=evidence, discrepancies=len(differences), actual_issue_counts=actual_counts,
                         expected_issue_counts=expected_counts, detail="")
        except Exception as error:
            entry.update(detail=f"{type(error).__name__}: {error}", kind="infrastructure_error")
        result["datasets"].append(entry)
        persist()
        oracle = None
        for policy in POLICIES:
            position = next(i for i, row in enumerate(result["episodes"]) if row["id"] == f"{policy}/{label}")
            if entry["status"] != "passed":
                row = {**result["episodes"][position], "category": selection["category"], "status": "not_checked",
                       "detail": "Набор не прошёл независимую проверку; политика не запускалась."}
            elif policy != "oracle" and policy_ready["status"] != "passed":
                row = {**result["episodes"][position], "category": selection["category"],
                       "detail": "Локальный sandbox image недоступен; небезопасной подмены нет."}
            else:
                row = run_attempt(snapshot, output / "attempts" / policy / label, selection, policy,
                                  manifest["policy_seed"], policy_image, cpuset)
                if policy == "oracle":
                    oracle = row
            result["episodes"][position] = row
            persist()
        pair_index = int(label.split("-")[-1]) - 1
        if entry["status"] == "passed" and oracle:
            reconcile = next(row for row in result["episodes"] if row["id"] == f"reconcile/{label}")
            replay_policy = "reconcile" if reconcile.get("outcome") == "submitted" else "oracle"
            replay_source = output / "attempts" / replay_policy / label / "trusted" / "attempt" / "trajectory.jsonl"
            result["pairs"][pair_index] = run_pair(snapshot, original_answer, oracle, output / "pairs" / label,
                                                   selection, verifier_image, cpuset, replay_source)
            result["pairs"][pair_index]["replayed_policy"] = replay_policy
            if file_hashes(snapshot) != entry["snapshot_sha256"]:
                entry.update(status="failed", kind="snapshot_mutation", detail="Source snapshot changed during rehearsal.")
        else:
            result["pairs"][pair_index]["detail"] = "Исходный набор/прогон oracle не принят; пара не выполнялась."
        persist()
    checks = result["datasets"] + result["episodes"] + result["pairs"] + result["prerequisites"]
    result["status"] = "failed" if any(row["status"] == "failed" for row in checks) else "incomplete" if any(row["status"] == "not_checked" for row in checks) else "passed"
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    persist()
    return result
