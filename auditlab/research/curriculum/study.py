"""Run and measure the profile 1.2 financial-family curriculum study."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
import re
from time import monotonic

from auditlab.generator import generate_episode
from auditlab.policies import make_policy
from auditlab.release.rehearsal import canonical_hash
from auditlab.release.transforms import read_json, write_json

from .export import CurriculumExample, FamilyMember, export_curriculum, extend_family_plan, plan_splits
from .families import answers_match, renamed_snapshot
from .financial_pairs import build_financial_variants, check_financial_variant
from .laboratory import _candidate_receipt, _search_attempt, _selection_cost, _teacher, local_runner
from .objectives import OBJECTIVES, objective_failures
from .proposals import ProposalAxis, search_proposals
from .reduce import FailureTarget, ReductionBudget, ReductionCandidate, reduce, snapshot_sha256
from .search import Candidate, SearchConfig, Validation


class _PerturbedReconcile:
    """Positive controls for objective wiring using an ordinary public investigation."""
    def __init__(self, goal, seed):
        self.policy = make_policy("reconcile", seed)
        self.goal = goal

    def act(self, observation):
        action = self.policy.act(observation)
        if action["action"] == "submit":
            for item in action["args"]["findings"]:
                if self.goal == "wrong_amount":
                    item["claimed_amount_minor"] += 1
                else:
                    item["evidence_ids"] = []
        return action


def diagnostic_runner(goal, source_sha, protocol):
    names = {"missed_issue": "random", "clean_false_positive": "flag_all"}
    if goal in names:
        return local_runner(names[goal], source_sha, protocol), names[goal]
    return local_runner(f"reconcile_control_{goal}", source_sha, protocol,
                        policy_factory=lambda name, seed: _PerturbedReconcile(goal, seed)), f"reconcile_control_{goal}"


class _Catalog:
    def __init__(self, past, output, source_sha, protocol):
        self.plan = past
        self.output = output
        self.source_sha = source_sha
        self.protocol = protocol
        self.paths = {}
        self.entries = {}
        self.receipts = {}

    def generate(self, candidate):
        key = candidate.candidate_id
        if key not in self.paths:
            path = self.output / "cases" / key / "s"
            generate_episode(candidate.seed, candidate.config, path)
            self.paths[key] = path
            self.entries[key] = candidate
            receipt = _candidate_receipt(candidate, path, self.source_sha, self.protocol)
            self.receipts[key] = receipt
            write_json(path.parent / "candidate.json", receipt)
        return self.paths[key]

    def offer(self, candidate, parent):
        key = candidate.candidate_id
        path = self.generate(candidate)
        if key != parent and self.receipts[key]["parent"] is None:
            self.plan = extend_family_plan(self.plan, [FamilyMember(key, candidate.family_id, "train", path, parent)])
            self.receipts[key]["parent"] = parent
            write_json(path.parent / "candidate.json", self.receipts[key])
        return self.plan.assert_searchable(key).snapshot


def _read_validation(row):
    return read_json(Path(row["validation"]["artifact_ref"]))


def _stable_target(row, goal, minimum):
    count = Counter()
    for attempt in row["attempts"]:
        if attempt["status"] != "completed":
            continue
        receipt = read_json(Path(attempt["artifact_ref"]))
        count.update({FailureTarget(item["reason"], item["type"], tuple(item["affected_ids"]), item.get("issue_id"))
                      for item in objective_failures(receipt, goal)})
    eligible = [target for target, seen in count.items() if seen >= minimum]
    return min(eligible, key=lambda target: (-count[target], target.issue_type, target.affected_ids,
                                             target.issue_id or ""), default=None)


def _choose_representative(found, candidate, row, target):
    """Retain family order, but export a verified refinement when search found one."""
    existing = found.get(candidate.family_id)
    if existing is None or (row["proposal"]["method"] == "refinement"
                            and existing[1]["proposal"]["method"] != "refinement"):
        found[candidate.family_id] = (candidate, row, target)


def _dimensions(path):
    manifest = read_json(path / "public/manifest.json")
    return [manifest["ledger_rows"], manifest["document_count"]]


def _family_witnesses(catalog, roots, validate, output):
    results = []
    examples = []
    for candidate in roots:
        case = catalog.paths[candidate.candidate_id]
        folder = output / candidate.candidate_id
        base_dir = folder / "base"
        baseline = validate(case, base_dir)
        if baseline["status"] != "passed":
            results.append({"root": candidate.candidate_id, "status": baseline["status"], "reason": baseline["reason"]})
            continue
        baseline_answer = read_json(base_dir / "answer_before_truth.json")
        examples.append(CurriculumExample(candidate.candidate_id, catalog.receipts[candidate.candidate_id],
                                          baseline, {"attempts": 0, "cost_micros": 0}))
        try:
            variants = build_financial_variants(case, candidate.seed, candidate.config, folder / "variants")
        except ValueError as error:
            results.append({"root": candidate.candidate_id, "status": "not_checked", "reason": str(error)})
            continue
        for variant in variants:
            directory = folder / variant.kind
            receipt = validate(variant.snapshot, directory)
            result = {"root": candidate.candidate_id, "kind": variant.kind, "status": receipt["status"],
                      "reason": receipt["reason"], "solver_actions": receipt.get("checks", {}).get("independent_solver", {}).get("actions"),
                      "baseline_size": _dimensions(case), "variant_size": _dimensions(variant.snapshot)}
            if receipt["status"] == "passed":
                answer = read_json(directory / "answer_before_truth.json")
                result.update(check_financial_variant(case, variant, baseline_answer, answer))
                if result["status"] == "passed":
                    member = FamilyMember(f"{candidate.candidate_id}-{variant.kind}", candidate.family_id,
                                          "dev", variant.snapshot, candidate.candidate_id)
                    catalog.plan = extend_family_plan(catalog.plan, [member])
            results.append(result)
    return results, examples


def _paired_control(case, candidate, target, answer, validate, investigate, output, max_cost):
    snapshot = output / "alias" / "s"
    mapping = renamed_snapshot(case, snapshot, candidate.seed + 42)
    write_json(snapshot.parent / "candidate.json", {"candidate": asdict(candidate), "parent": candidate.candidate_id,
                                                   "transform": mapping["version"]})
    checked = validate(snapshot, output / "validation")
    reported = {"status": checked["status"], "reason": checked["reason"], "kind": "id_order", "attempt": None,
                "snapshot_sha256": snapshot_sha256(snapshot), "relates_financial_answers": False}
    if checked["status"] != "passed":
        return reported
    references = mapping["references"]
    alias_answer = read_json(output / "validation/answer_before_truth.json")
    expected = [{**item, "affected_ids": [references.get(key, key) for key in item["affected_ids"]]}
                for item in answer["findings"]]
    reported["relates_financial_answers"] = answers_match(expected, alias_answer["findings"])
    if not reported["relates_financial_answers"]:
        reported.update(status="failed", reason="independent paired answer did not retain its meaning")
        return reported
    alias_target = FailureTarget(target.reason, target.issue_type,
                                 tuple(references.get(key, key) for key in target.affected_ids))
    attempt = investigate(snapshot, output / "rerun", 901, 60.0)
    reported.update(attempt=attempt, repeated=None, over_budget=False)
    if max_cost is not None and (attempt.get("cost_micros") is None or attempt["cost_micros"] > max_cost):
        reported.update(status="not_checked", reason="paired attempt did not account for reserved spend", over_budget=True)
    elif attempt.get("status") == "submitted" and attempt.get("snapshot_sha256") == reported["snapshot_sha256"]:
        reported["repeated"] = any(alias_target.matches(failure)
                                   for failure in objective_failures(attempt, target.reason))
    return reported


def _study_goal(goal, plan, roots, axes, catalog, validate, investigate, output, blocked):
    protocol = plan["protocol_id"]
    seen = set()
    root_ids = {c.family_id: c.candidate_id for c in roots}

    def check(candidate, directory):
        try:
            case = catalog.offer(candidate, root_ids[candidate.family_id])
        except (ValueError, OSError, RuntimeError) as error:
            directory.mkdir(parents=True, exist_ok=False)
            path = directory / "validation.json"
            write_json(path, {"status": "failed", "reason": str(error), "candidate": asdict(candidate)})
            return Validation("failed", str(error), str(path))
        receipt = validate(case, directory)
        return Validation(receipt["status"], receipt["reason"], str(directory / "validation.json"))

    def run(candidate, repeat, directory):
        case = catalog.plan.assert_searchable(candidate.candidate_id).snapshot
        digest = snapshot_sha256(case)
        receipt = investigate(case, directory, repeat, 60.0)
        return _search_attempt(receipt, directory, digest, seen,
                               is_weak=lambda item: bool(objective_failures(item, goal)))

    budget = SearchConfig(tuple(roots), source_sha=catalog.source_sha, protocol_id=protocol, **plan["search"])
    search = search_proposals(budget, run, check, output / "search", axes=axes,
                              holdout_families=blocked[0], holdout_seeds=blocked[1])
    summary = {"objective": goal, "status": search["status"], "space_sha256": search["plan_sha256"], "arms": {},
               "reduction": None, "search": str(output / "search/search.json")}
    selections = {}
    if search["status"] != "completed":
        return summary, selections
    for name, arm in search["arms"].items():
        found = {}
        offered = Counter()
        submitted, clean, accusations, audit_seconds = 0, 0, 0, 0.0
        operations = Counter()
        for row in arm["candidates"]:
            candidate = Candidate(**row["proposal"]["candidate"])
            offered[candidate.family_id] += 1
            for attempt in row["attempts"]:
                ref = attempt.get("artifact_ref")
                if ref and Path(ref).is_file():
                    audit_seconds += read_json(Path(ref)).get("elapsed_seconds", 0)
                if attempt["status"] != "completed":
                    operations[attempt["status"]] += 1
                    continue
                submitted += 1
                receipt = read_json(Path(attempt["artifact_ref"]))
                score = receipt["score"]
                if score["tp"] + score["fn"] == 0:
                    clean += 1
                    accusations += int(score["fp"] > 0)
            if row["status"] != "accepted":
                continue
            target = _stable_target(row, goal, budget.min_weak_repetitions)
            if target is not None:
                _choose_representative(found, candidate, row, target)
        selections[name] = list(found.values())
        summary["arms"][name] = {"offered": arm["offered"], "families_offered": len(offered),
                                 "strata": dict(Counter(r["stratum"] for r in arm["candidates"])),
                                 "refinements": sum(r["proposal"]["method"] == "refinement" for r in arm["candidates"]),
                                 "families_stable": len(found), "stable_goals": dict(Counter(t.issue_type for _, _, t in found.values())),
                                 "agent_runs": arm["agent_runs"], "submitted": submitted, "operational": dict(operations),
                                 "cost_micros": arm["cost_micros"], "validation_seconds": arm["validation_seconds"],
                                 "audit_seconds": audit_seconds,
                                 "clean_submissions": clean, "clean_with_accusation": accusations, "controls": []}
        write_json(output / "assessment.json", summary)
    return summary, selections


def _reduce_selection(item, goal, settings, catalog, validate, investigate, output):
    candidate, _, target = item

    def fresh(case, directory, seconds):
        receipt = investigate(case, directory, int(directory.name.rsplit("-", 1)[-1]), min(60.0, seconds))
        return {**receipt, "failures": objective_failures(receipt, goal)}

    return reduce(ReductionCandidate(catalog.paths[candidate.candidate_id], target, catalog.source_sha, catalog.protocol,
                                     ReductionBudget(**settings)), fresh, validate, output)


def run_study(plan, source_sha, validate, output, *, external=None, exclusions=()):
    """Use the same declared domain and bounds per arm; keep all held-out lineage reserved."""
    output = Path(output).resolve()
    protocol = plan["protocol_id"]
    candidates = [Candidate(**{**root, "config": {**plan["generator"], **root["config"]}}) for root in plan["roots"]]
    ids = [c.candidate_id for c in candidates]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", key) for key in ids):
        raise ValueError("Candidate IDs must be unique and portable")
    if (not plan["objectives"] or not set(plan["objectives"]) <= set(OBJECTIVES)
            or len(set(plan["objectives"])) != len(plan["objectives"])):
        raise ValueError("Study objectives must be supported and unique")
    reserved = [row for row in candidates if row.split in ("dev", "test")]
    roots = [row for row in candidates if row.split == "train"]
    SearchConfig(tuple(roots), source_sha=source_sha, protocol_id=protocol, **plan["search"])
    ReductionBudget(**plan["reduction"])
    if type(plan["examples_per_goal"]) is not int or plan["examples_per_goal"] < 0:
        raise ValueError("The number of training families per goal must be a nonnegative integer")
    if len({root.family_id for root in roots}) != len(roots):
        raise ValueError("Each train family requires exactly one configuration root")
    blocked = ({row.family_id for row in reserved} | {row["family_id"] for row in exclusions},
               {row.seed for row in reserved} | {row["seed"] for row in exclusions})
    if any(c.family_id in blocked[0] or c.seed in blocked[1] for c in roots):
        raise ValueError("A proposed train root overlaps a reserved family or seed")
    output.mkdir(parents=True, exist_ok=False)
    catalog = _Catalog(None, output, source_sha, protocol)
    members = [FamilyMember(c.candidate_id, c.family_id, c.split, catalog.generate(c)) for c in candidates]
    members.extend(FamilyMember(f"reserved-{number:04d}", row["family_id"], "test", Path(row["snapshot"]))
                   for number, row in enumerate(exclusions) if row.get("snapshot"))
    catalog.plan = plan_splits(members)
    axes = {key: tuple(ProposalAxis(tuple(axis["path"]), tuple(axis["values"])) for axis in specs)
            for key, specs in plan["axes"].items()}
    report = {"schema_version": "1.0", "artifact_id": "financial-families-v2", "source_sha": source_sha,
              "protocol_id": protocol, "plan_sha256": canonical_hash(plan), "partition_sha256": catalog.plan.sha256,
              "runner": external.metadata() if external else {"kind": "local_policies_and_positive_controls"},
              "heldout_registry_count": len(exclusions), "goals": [], "status": "in_progress", "exports": {},
              "pair_validation": None}
    write_json(output / "study.json", report)
    curriculum = {"adaptive": {}, "uniform": {}}
    for goal in plan["objectives"]:
        start = monotonic()
        investigate, name = (external, "configured_api") if external else diagnostic_runner(goal, source_sha, protocol)
        folder = output / goal
        result, selected = _study_goal(goal, plan, roots, axes, catalog, validate, investigate, folder, blocked)
        result["agent"] = name
        report["goals"].append(result)
        if result["status"] != "completed":
            report["status"] = result["status"]
            write_json(output / "study.json", report)
            return report
        max_cost = plan["search"].get("max_attempt_cost_micros")
        prepared = {}
        qualified = []
        for arm, items in selected.items():
            examples = []
            used_families = {example.provenance["candidate"]["family_id"] for example in curriculum[arm].values()}
            for candidate, row, target in items:
                path = Path(row["validation"]["artifact_ref"])
                answer = read_json(path.parent / "answer_before_truth.json")
                alias = _paired_control(catalog.paths[candidate.candidate_id], candidate, target, answer, validate,
                                        investigate, folder / "pairs" / arm / candidate.candidate_id, max_cost)
                result["arms"][arm]["controls"].append({"candidate_id": candidate.candidate_id, **alias})
                if alias.get("over_budget"):
                    report["status"] = "stopped_cost_contract"
                    write_json(output / "study.json", report)
                    return report
                if alias["status"] != "passed":
                    continue
                qualified.append((candidate, row, target))
                if candidate.family_id in used_families:
                    continue
                key = candidate.candidate_id
                examples.append(CurriculumExample(key, {**catalog.receipts[key], "objective": goal,
                                "failure_target": asdict(target), "control": alias}, _read_validation(row),
                                _selection_cost([row]), _teacher(answer)))
            prepared[arm] = examples
        count = min(plan["examples_per_goal"], *(len(items) for items in prepared.values()))
        for arm, examples in prepared.items():
            for example in examples[:count]:
                curriculum[arm].setdefault(example.candidate_id, example)
        target = next(iter(qualified), None)
        if target is not None:
            report_reduction = _reduce_selection(target, goal, plan["reduction"], catalog, validate, investigate,
                                                 folder / "reduction")
            result["reduction"] = {key: report_reduction[key] for key in (
                "status", "reason", "target", "runs_used", "cost", "selected_sizes", "selected_snapshot_sha256")}
            if report_reduction["status"] == "reduced":
                candidate = target[0]
                member = FamilyMember(f"{candidate.candidate_id}-{goal}-reduced", candidate.family_id, "train",
                                      Path(report_reduction["selected_snapshot"]), candidate.candidate_id)
                catalog.plan = extend_family_plan(catalog.plan, [member])
                result["reduction"]["family_id"] = candidate.family_id
            if max_cost is not None and (report_reduction["cost"]["cost_micros"] is None or
                report_reduction["reason"] in {"unreported_attempt_cost", "attempt_cost_exceeded_reservation"}):
                report["status"] = "stopped_cost_contract"
                write_json(output / "study.json", report)
                return report
        result.update(elapsed_seconds=monotonic() - start, matched_examples_per_arm=count)
        write_json(output / "study.json", report)

    report["pair_validation"], dev = _family_witnesses(catalog, [c for c in reserved if c.split == "dev"],
                                                       validate, output / "financial-controls")
    for arm, examples in curriculum.items():
        path = export_curriculum(catalog.plan, [*examples.values(), *dev], output / arm, artifact_id=f"financial-v2-{arm}",
                                 source_sha=source_sha, protocol_id=protocol)
        manifest = read_json(path)
        report["exports"][arm] = {"manifest": path.relative_to(output).as_posix(),
                                   "examples": dict(Counter(f"{row['split']}/{row['status']}" for row in manifest["examples"])),
                                   "train_families": len({row["family_id"] for row in manifest["examples"]
                                                          if row["split"] == "train" and row["status"] == "included"})}
    report["status"] = "completed"
    write_json(output / "study.json", report)
    return report
