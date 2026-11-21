"""Run the frozen curriculum protocol in the trusted controller."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import re
from time import monotonic
from uuid import uuid4

from auditlab.contracts import MAX_EXPLANATION
from auditlab.evaluator.schema import RULES_GENESIS
from auditlab.generator import generate_episode
from auditlab.policies import make_policy
from auditlab.release.rehearsal import canonical_hash, file_hashes
from auditlab.release.transforms import read_json, write_json
from auditlab.runner import make_env
from auditlab.runner.episode import run_episode

from .export import CurriculumExample, FamilyMember, export_curriculum, extend_family_plan, plan_splits
from .families import answers_match, boundary_variants
from .reduce import FailureTarget, ReductionBudget, ReductionCandidate, reduce, snapshot_sha256
from .search import Attempt, Candidate, SearchConfig, Validation, search


def local_runner(name: str, source_sha: str, protocol_id: str):
    """Run a real bundled policy using the closed runner and Genesis scoring rules."""
    def investigate(snapshot: Path, output: Path, repetition: int, seconds: float) -> dict:
        output.mkdir(parents=True, exist_ok=False)
        run_id = f"local-{uuid4().hex}"
        env = make_env("closed", rules=RULES_GENESIS, evaluator="inprocess", trusted_dir=output / "runner")
        start = monotonic()
        policy_seed = 41000 + repetition
        _, _, _, _, _ = run_episode(env, make_policy(name, policy_seed), policy_seed, {
            "snapshot_dir": snapshot, "run_id": run_id, "policy_name": name,
            "time_limit_s": min(60.0, seconds),
        })
        final = env.final
        receipt = {
            "schema_version": "1.0", "artifact_id": run_id, "attempt_id": run_id,
            "source_sha": source_sha, "protocol_id": protocol_id, "agent": name, "kind": "built_in_policy",
            "policy_seed": policy_seed, "repetition": repetition, "status": final["status"],
            "snapshot_sha256": snapshot_sha256(snapshot), "elapsed_seconds": monotonic() - start,
            "cost_micros": 0, "launches": 1, "failures": [], "reason": None, "score": None,
            "artifact_ref": str(output / "attempt.json"),
            "actions": final["inspection_steps"] + (1 if final["terminated"] else 0),
        }
        graded = final["graded"]
        if final["status"] == "submitted" and final["completed"] and graded is not None:
            truth = read_json(snapshot / "private/truth.json")
            matched = {issue_id for _, issue_id in graded["matches"]}
            failures = [{"reason": "missed_issue", "type": issue["type"], "affected_ids": issue["affected_ids"],
                         "issue_id": issue["issue_id"]} for issue in truth["issues"] if issue["issue_id"] not in matched]
            findings = final["submission"]["findings"]
            failures.extend({"reason": "false_positive", "type": findings[int(index)]["type"],
                             "affected_ids": findings[int(index)]["affected_ids"], "diagnostic": reason}
                            for index, reason in graded["unmatched"].items() if int(index) < len(findings))
            receipt.update(failures=failures, score=graded["score"])
        else:
            receipt.update(status="infra_error" if final["infra"] else final["status"],
                           reason=", ".join(final["infra"]) or final["status"])
        write_json(output / "attempt.json", receipt)
        return receipt
    return investigate


def _candidate_receipt(candidate, snapshot, source_sha, protocol_id):
    provenance = read_json(snapshot / "private/provenance.json")
    return {"schema_version": "1.0", "artifact_id": candidate.candidate_id,
            "source_sha": source_sha, "protocol_id": protocol_id, "candidate": asdict(candidate),
            "generator_version": provenance["generator_version"], "profile": candidate.config["config_version"],
            "parent": None, "mutations": provenance["mutations"], "snapshot_sha256": snapshot_sha256(snapshot),
            "files_sha256": file_hashes(snapshot)}


def _search_attempt(receipt, output, digest, seen):
    status = receipt.get("status")
    cost, launches = receipt.get("cost_micros"), receipt.get("launches", 1)
    reference = str(output / "attempt.json")
    if status == "submitted" and isinstance(receipt.get("failures"), list):
        identifier = receipt.get("attempt_id")
        if (not isinstance(identifier, str) or not identifier or identifier in seen
                or digest != receipt.get("snapshot_sha256") or not Path(reference).is_file()):
            return Attempt("invalid_response", cost_micros=cost, artifact_ref=reference,
                           reason="stale or unbound submission receipt", launches=launches)
        seen.add(identifier)
        return Attempt("completed", any(item["reason"] == "missed_issue" for item in receipt["failures"]),
                       cost, reference, launches=launches)
    category = {"timeout": "timeout", "budget_stop": "budget_stopped", "action_budget_exhausted": "budget_stopped",
                "invalid_submission": "invalid_response", "invalid_response": "invalid_response"}
    return Attempt(category.get(status, "provider_error"), cost_micros=cost, artifact_ref=reference,
                   reason=receipt.get("reason") or status or "unknown_runner_status", launches=launches)


def _sizes(snapshot):
    manifest = read_json(snapshot / "public/manifest.json")
    return [manifest["ledger_rows"], manifest["document_count"]]


def _selection_cost(rows):
    attempts = [attempt for row in rows for attempt in row["attempts"]]
    return {"attempts": len(attempts), "runs": sum(a.get("launches") or 0 for a in attempts),
            "cost_micros": None if any(a.get("cost_micros") is None for a in attempts)
            else sum(a["cost_micros"] for a in attempts)}


def _teacher(answer):
    arithmetic = answer["arithmetic"]
    findings = []
    for item in answer["findings"]:
        ids = [reference.split("/", 1)[1] for reference in item["affected_ids"]]
        relevant = [line for line in arithmetic if any(identifier in line for identifier in ids)]
        explanation = "\n".join(relevant) or "Independent financial calculation backed by the cited public records."
        findings.append({**item, "explanation": explanation[:MAX_EXPLANATION]})
    return {"submission": {"episode_id": answer["episode_id"], "findings": findings}, "arithmetic": arithmetic}


def run_experiment(plan, source_sha, validate, investigate, output, *, agent_metadata):
    """Run a bounded experiment. Only its frozen train members reach search or reduction."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    protocol = plan["protocol_id"]
    roster = [Candidate(**{**item, "config": {**plan["generator"], **item["config"]}}) for item in plan["candidates"]]
    candidate_ids = {c.candidate_id: c.split for c in roster}
    calibration = plan.get("clean_calibration", [])
    if (len(candidate_ids) != len(roster) or any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", key)
            for key in candidate_ids) or any(candidate_ids.get(key) not in {"train", "dev"}
            for key in plan.get("paired_examples", [])) or any(candidate_ids.get(key) != "train" for key in calibration)):
        raise ValueError("Expected portable candidate IDs, train/dev pairs and train-only calibration")
    if agent_metadata.get("kind") != "built_in_policy" and any(
        plan[phase].get("max_attempt_cost_micros") is None or plan[phase].get("cost_budget_micros") is None
        for phase in ("search", "reduction")
    ):
        raise ValueError("External runs require explicit attempt and phase cost budgets")
    snapshots, provenance = {}, {}
    for candidate in roster:
        snapshot = output / "candidates" / candidate.candidate_id / "snapshot"
        try:
            generate_episode(candidate.seed, candidate.config, snapshot)
            snapshots[candidate.candidate_id] = snapshot
            provenance[candidate.candidate_id] = _candidate_receipt(candidate, snapshot, source_sha, protocol)
        except (ValueError, RuntimeError, OSError) as error:
            write_json(snapshot.parent / "candidate.json", {"schema_version": "1.0",
                       "artifact_id": candidate.candidate_id, "source_sha": source_sha, "protocol_id": protocol,
                       "candidate": asdict(candidate), "status": "generation_failed", "reason": str(error)})
            raise
        write_json(snapshot.parent / "candidate.json", provenance[candidate.candidate_id])
    family_plan = plan_splits(FamilyMember(c.candidate_id, c.family_id, c.split, snapshots[c.candidate_id])
                              for c in roster)
    training = [c for c in roster if c.split == "train"]
    experiment = {"schema_version": "1.0", "artifact_id": f"experiment-{family_plan.sha256[:24]}",
                  "source_sha": source_sha, "protocol_id": protocol, "status": "in_progress",
                  "plan_sha256": canonical_hash(plan), "partition": family_plan.manifest(), "agent": agent_metadata,
                  "budget": {"search_per_arm": plan["search"], "reduction": plan["reduction"],
                             "episode_protocol_id": plan["episode_protocol_id"],
                             "actions_per_episode": 100, "seconds_per_episode": 60},
                  "criteria": {"weakness": "at least one missed reference issue in a valid submission",
                               "failure_target": "first repeated missed issue in adaptive then uniform order"}}
    write_json(output / "experiment.json", experiment)
    attempt_ids = set()

    def check(candidate, directory):
        member = family_plan.assert_searchable(candidate.candidate_id)
        validation = validate(member.snapshot, directory)
        return Validation(validation["status"], validation["reason"], str(directory / "validation.json"))

    def run(candidate, repetition, directory):
        member = family_plan.assert_searchable(candidate.candidate_id)
        digest = snapshot_sha256(member.snapshot)
        receipt = investigate(member.snapshot, directory, repetition, 60.0)
        if snapshot_sha256(member.snapshot) != digest:
            return Attempt("provider_error", cost_micros=receipt.get("cost_micros"),
                           reason="candidate changed during investigation", launches=receipt.get("launches"))
        return _search_attempt(receipt, directory, digest, attempt_ids)

    selection = search(SearchConfig(tuple(training), source_sha=source_sha, protocol_id=protocol, **plan["search"]),
                       run, check, output / "search")
    if selection["status"] != "completed":
        experiment.update(status="not_checked", reason=selection["status"], search="search/search.json")
        write_json(output / "experiment.json", experiment)
        return experiment

    accepted = {}
    offered = {}
    for arm in selection["arms"].values():
        for row in arm["candidates"]:
            offered.setdefault(row["candidate_id"], []).append(row)
            if row["status"] == "accepted":
                accepted.setdefault(row["candidate_id"], []).append(row)
    examples = {}
    for key, rows in accepted.items():
        path = Path(rows[0]["validation"]["artifact_ref"])
        examples[key] = CurriculumExample(key, {**provenance[key], "selection": "observed_agent_failure"},
                                          read_json(path), _selection_cost(offered[key]),
                                          _teacher(read_json(path.parent / "answer_before_truth.json")))

    controls = []
    for key in calibration:
        member = family_plan.assert_searchable(key)
        directory = output / "calibration" / key
        receipt = validate(member.snapshot, directory)
        answer = read_json(directory / "answer_before_truth.json") if receipt["status"] == "passed" else None
        passed = answer is not None and not answer["findings"]
        status = "not_checked" if answer is None else ("passed" if passed else "failed")
        controls.append({"candidate_id": key, "status": status, "validation_ref": str(directory / "validation.json")})
        if passed and key not in examples:
            examples[key] = CurriculumExample(key, {**provenance[key], "selection": "predeclared_clean_control"},
                receipt, _selection_cost(offered.get(key, [])), _teacher(answer))

    # Controls are defined before the comparison. A dev control never launches a searched agent.
    members = []
    paired = []
    for key in plan.get("paired_examples", []):
        candidate = next(c for c in roster if c.candidate_id == key and c.split in {"train", "dev"})
        base = output / "pairs" / key
        validation = validate(snapshots[key], base / "baseline-validation")
        if validation["status"] == "passed" and candidate.split == "dev":
            examples[key] = CurriculumExample(key, provenance[key], validation, {"attempts": 0, "cost_micros": 0})
        for variant in boundary_variants(snapshots[key], candidate.seed, candidate.config, base / "snapshots"):
            variant_id = f"{key}-{variant['kind']}"
            snapshot = variant.pop("snapshot")
            directory = base / variant["kind"]
            receipt = validate(snapshot, directory)
            if receipt["status"] == "passed":
                answer = read_json(directory / "answer_before_truth.json")
                passed = answers_match(variant["expected"], answer["findings"])
            else:
                passed = False
            paired.append({**variant, "candidate_id": variant_id, "parent": key, "status": "passed" if passed else "failed",
                           "public_size": _sizes(snapshot), "parent_public_size": _sizes(snapshots[key]),
                           "snapshot_sha256": snapshot_sha256(snapshot), "validation_ref": str(directory / "validation.json")})
            if passed and validation["status"] == "passed" and (candidate.split == "dev" or key in examples):
                members.append(FamilyMember(variant_id, candidate.family_id, candidate.split, snapshot, key))
                examples[variant_id] = CurriculumExample(variant_id, paired[-1], receipt, {"attempts": 0, "cost_micros": 0},
                                                          _teacher(answer) if candidate.split == "train" else None)

    reduction = _reduce_found(accepted, selection, snapshots, source_sha, protocol, plan, validate, investigate, output)
    if reduction is not None and reduction["status"] == "reduced":
        key = reduction["candidate_id"]
        original = family_plan.assert_searchable(key)
        reduced = Path(reduction["selected_snapshot"])
        result = next(p for p in reduction["proposals"] if p["snapshot"] == str(reduced))
        receipt_path = reduced.parent / "validation/validation.json"
        receipt = read_json(receipt_path)
        teacher = _teacher(read_json(receipt_path.parent / "answer_before_truth.json"))
        short_id = f"{key}-reduced"
        members.append(FamilyMember(short_id, original.family_id, "train", reduced, key))
        examples[short_id] = CurriculumExample(short_id, {"parent": key, "reduction": "reduction/reduction.json",
                    "selected_records": result["sizes"]}, receipt, {"attempts": reduction["runs_used"],
                    "cost_micros": reduction["cost"]["cost_micros"]}, teacher)
    family_plan = extend_family_plan(family_plan, members)
    export_curriculum(family_plan, examples.values(), output / "curriculum", artifact_id="curriculum-" + family_plan.sha256[:24],
                      source_sha=source_sha, protocol_id=protocol)
    experiment.update(status="completed", search="search/search.json", curriculum="curriculum/curriculum.json",
                      paired=paired, clean_calibration=controls,
                      reduction="reduction/reduction.json" if reduction is not None else None,
                      reason=(None if reduction is not None else
                              "no_repeated_target_selected" if accepted else "no_target_weakness_observed"))
    write_json(output / "experiment.json", experiment)
    return experiment


def _reduce_found(accepted, search_result, snapshots, source_sha, protocol, plan, validate, investigate, output):
    # Preserve the discovery evidence and require fresh baseline repeats even when it looks stable.
    candidate_rows = [row for arm in search_result["arms"].values() for row in arm["candidates"]]
    for row in candidate_rows:
        candidate_id = row["candidate_id"]
        if row["status"] != "accepted" or candidate_id not in accepted:
            continue
        attempts = [read_json(Path(attempt["artifact_ref"])) for attempt in row["attempts"]]
        targets = {}
        for attempt in attempts:
            for failure in attempt["failures"]:
                if failure["reason"] == "missed_issue":
                    target = FailureTarget(failure["reason"], failure["type"], tuple(failure["affected_ids"]),
                                           failure["issue_id"])
                    targets[target] = targets.get(target, 0) + 1
        matching = [key for key, count in targets.items() if count >= plan["search"]["min_weak_repetitions"]]
        if not matching:
            continue
        target = matching[0]
        snapshot = snapshots[candidate_id]
        failure = {"schema_version": "1.0", "artifact_id": f"failure-{candidate_id}", "source_sha": source_sha,
                   "protocol_id": protocol, "candidate_id": candidate_id, "target": asdict(target),
                   "attempts": [{"attempt_id": a["attempt_id"], "artifact_ref": a["artifact_ref"]} for a in attempts],
                   "frequency": {"matching": targets[target], "submitted": len(attempts)},
                   "alternatives": ["low-resource policy", "stochastic provider response", "order sensitivity"]}
        write_json(output / "failure.json", failure)

        def fresh(case, directory, seconds):
            return investigate(case, directory, int(directory.name.rsplit("-", 1)[-1]), seconds)

        result = reduce(ReductionCandidate(snapshot, target, source_sha, protocol, ReductionBudget(**plan["reduction"])),
                        fresh, validate, output / "reduction")
        result["candidate_id"] = candidate_id
        write_json(output / "reduction/reduction.json", result)
        return result
    return None
