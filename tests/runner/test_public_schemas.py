"""Issue #14 (API-01, API-05, ISO-02/03): published schemas checked against the real components.

Run: python -m unittest discover -s tests/runner -p "test_public_schemas.py" -v
Everything here drives make_env with the real open_session and the evaluator in its own process,
the real policies and the shipped snapshots. Synthetic JSON is used only as negative examples
derived from real messages.
"""
import ast
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from auditlab.environment import open_session
from auditlab.runner import make_env
from auditlab.runner.episode import OraclePolicy, run_episode
from auditlab.runner.isolation import SubprocessPolicy, scrubbed_env

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas" / "public" / "1.0"
SCHEMA_FILE = SCHEMA_DIR / "auditlab-public.schema.json"
FIXTURES = ROOT / "fixtures" / "foundation"
HERE = Path(__file__).resolve().parent


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATE = _load_by_path("published_validator", ROOT / "schemas" / "validate.py")
V = VALIDATE.Validator.from_path(SCHEMA_FILE)
VALID = json.loads((SCHEMA_DIR / "examples" / "valid.json").read_text(encoding="utf-8"))
INVALID = json.loads((SCHEMA_DIR / "examples" / "invalid.json").read_text(encoding="utf-8"))
RUNNER_ERRORS = {"invalid_request": "Invalid request.", "unknown_action": "Unknown action.",
                 "internal_error": "Internal error.", "invalid_submission": "Submission failed schema validation.",
                 "time_limit": "Time limit exceeded.", "aborted": "Episode aborted.",
                 "malformed_action": "Action must be an object with 'action' and 'args'.",
                 "no_active_episode": "Call reset first.", "episode_finished": "The episode is already finished."}
TABLE_PAGE = {"ledger": "pageLedger", "invoices": "pageInvoices", "payments": "pagePayments",
              "fx": "pageFx", "policy": "pagePolicy"}


def shipped_snapshots():
    return sorted({p.parent.parent for p in ROOT.glob("**/public/manifest.json") if ".git" not in p.parts})


def step_pointer(mode):
    return "#/$defs/stepTrain" if mode == "train" else "#/$defs/stepClosed"


class SchemaAndGolden(unittest.TestCase):
    def assertValid(self, instance, pointer, what=""):
        problems = V.errors(instance, pointer)
        self.assertEqual(problems, [], f"{what} vs {pointer}")

    def test_schema_document_uses_only_supported_keywords_and_local_refs(self):
        self.assertEqual(V.document["$id"], "urn:auditlab:public-schema:1.0")
        for name in ("reset", "action", "submission", "stepTrain", "stepClosed", "initialObservation",
                     "errorObservation", "trajectoryLineTrain", "trajectoryLineClosed", "exportRun"):
            self.assertIn(name, V.document["$defs"])
        with self.assertRaises(VALIDATE.SchemaError):
            VALIDATE.Validator({"$defs": {"x": {"format": "date-time"}}})  # unsupported keywords fail closed

    def test_golden_examples_are_reproduced_by_the_real_components(self):
        golden = _load_by_path("schema_golden", HERE / "schema_golden.py")
        # The committed files were built with the evaluator in its own process; rebuilding with the
        # in-process backend must give the same messages (and keeps the suite within run.py's timeout).
        valid = golden.build_valid(evaluator="inprocess")
        self.assertEqual(json.loads(golden.dump(None, valid)), VALID, "wire format drifted from the golden JSON")
        self.assertEqual(json.loads(golden.dump(None, golden.build_invalid(valid))), INVALID)

    def test_valid_examples_pass_and_each_invalid_one_fails_only_because_of_its_defect(self):
        by_name = {entry["name"]: entry for entry in VALID}
        self.assertGreaterEqual(len(VALID), 400)
        for entry in VALID:
            with self.subTest(valid=entry["name"]):
                self.assertValid(entry["instance"], entry["pointer"])
        self.assertGreaterEqual(len(INVALID), 45)
        for entry in INVALID:
            with self.subTest(invalid=entry["name"]):
                self.assertValid(by_name[entry["base"]]["instance"], entry["pointer"], "unmutated base")
                self.assertNotEqual(V.errors(entry["instance"], entry["pointer"]), [], entry["why"])

    def test_every_reachable_error_code_is_exercised_and_messages_match_the_code(self):
        def codes(value, found):
            if isinstance(value, dict):
                if isinstance(value.get("error"), dict):
                    found[value["error"]["code"]] = value["error"]["message"]
                for child in value.values():
                    codes(child, found)
            elif isinstance(value, list):
                for child in value:
                    codes(child, found)
            return found
        seen = codes([entry["instance"] for entry in VALID], {})
        import auditlab.environment as environment
        session_errors = dict(environment._ERRORS)
        expected = (set(session_errors) | set(RUNNER_ERRORS)) - {"invalid_action"}  # invalid_action: session-only
        self.assertEqual(set(seen), expected)
        for code, message in seen.items():
            self.assertIn(message, (RUNNER_ERRORS.get(code), session_errors.get(code)), code)
        for code, message in {**session_errors, **RUNNER_ERRORS}.items():  # schema lists every code verbatim
            self.assertValid({"code": code, "message": message}, "#/$defs/error", code)


class RealSnapshots(unittest.TestCase):
    def check_snapshot(self, snapshot: Path):
        session = open_session(snapshot / "public")
        for table, page_def in TABLE_PAGE.items():
            cursor, seen = None, 0
            while True:
                reply = session.inspect({"action": "list_records", "args": {"table": table, "cursor": cursor, "limit": 50}})
                self.assertTrue(reply["ok"], reply)
                problems = V.errors(reply["result"], f"#/$defs/{page_def}")
                self.assertEqual(problems, [], f"{snapshot.name}/{table}")
                for record in reply["result"]["records"]:
                    if table in ("invoices", "payments"):
                        document = session.inspect({"action": "get_document", "args": {"doc_id": record["doc_id"]}})
                        pointer = {"invoices": "#/$defs/invoice", "payments": "#/$defs/payment"}[table]
                        self.assertEqual(document, {"ok": True, "result": record})
                        self.assertEqual(V.errors(document["result"], pointer), [])
                seen += len(reply["result"]["records"])
                cursor = reply["result"]["next_cursor"]
                if cursor is None:
                    break
            if table == "ledger":
                manifest = json.loads((snapshot / "public" / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(seen, manifest["ledger_rows"])
        if (snapshot / "private" / "truth.json").is_file():  # exports ship public/ only
            env = make_env("closed", evaluator="inprocess")
            observation, info = env.reset(0, {"snapshot_dir": str(snapshot)})
            self.assertEqual(V.errors([observation, info], "#/$defs/reset"), [], snapshot.name)
            env.close()
        return seen

    def test_every_record_of_every_shipped_snapshot(self):
        snapshots = shipped_snapshots()
        self.assertGreaterEqual(len(snapshots), 26)
        for snapshot in snapshots:
            with self.subTest(snapshot=str(snapshot.relative_to(ROOT))):
                self.check_snapshot(snapshot)

    def test_generated_profile_1_1(self):
        try:
            import tzdata  # noqa: F401  (pinned in requirements.lock; the generator needs it)
        except ImportError:
            self.skipTest("не проверено: tzdata из requirements.lock не установлен")
        from auditlab.generator import generate_episode
        from auditlab.release.rehearsal import selection_manifest
        episodes = selection_manifest("1.1")["episodes"]
        chosen = [episodes[0], max(episodes, key=lambda e: e["config"]["company_size"]["ledger_rows_target"]), episodes[-1]]
        with tempfile.TemporaryDirectory() as tmp:
            for item in chosen:
                with self.subTest(episode=item["id"]):
                    target = Path(tmp) / item["id"]
                    generate_episode(item["seed"], item["config"], target)
                    manifest = json.loads((target / "public" / "manifest.json").read_text(encoding="utf-8"))
                    self.assertIs(manifest.get("period_end_exclusive"), True)
                    self.check_snapshot(target)


class RealPolicies(unittest.TestCase):
    def run_checked(self, mode, snapshot, policy):
        # Real session and policies; in-process evaluator (the separate process is covered by the
        # adapter, PolicyInput and Exports tests) so the suite fits run.py's per-suite timeout.
        env = make_env(mode, evaluator="inprocess")
        observation, info = env.reset(19, {"snapshot_dir": str(snapshot)})
        self.assertEqual(V.errors([observation, info], "#/$defs/reset"), [])
        steps = 0
        while True:
            action = policy.act(copy.deepcopy(observation))
            self.assertEqual(V.errors(action, "#/$defs/action"), [], f"policy action {action}")
            result = env.step(action)
            steps += 1
            self.assertEqual(V.errors(list(result), step_pointer(mode)), [], f"step {steps}")
            observation = result[0]
            if result[2] or result[3]:
                break
        for row in env._trajectory:
            self.assertEqual(V.errors(row, "#/$defs/trajectoryLineTrain" if mode == "train"
                                      else "#/$defs/trajectoryLineClosed"), [])
        if env.final["status"] in ("submitted",):
            self.assertEqual(V.errors(env.final["submission"], "#/$defs/submission"), [])
        env.close()
        return env.final

    def test_public_policies_and_oracle_in_both_modes(self):
        from auditlab.policies import make_policy
        snapshots = [FIXTURES / "S01", FIXTURES / "S02", ROOT / "datasets" / "open" / "dev" / "E1ADBA2ED9E09A7CE01C5B3A8D7CBE13E"]
        for mode in ("train", "closed"):
            for snapshot in snapshots:
                for name in ("random", "reconcile", "flag_all", "oracle"):
                    with self.subTest(mode=mode, snapshot=snapshot.name, policy=name):
                        policy = OraclePolicy(snapshot, inspections=2) if name == "oracle" else make_policy(name, 19)
                        final = self.run_checked(mode, snapshot, policy)
                        self.assertIn(final["status"], ("submitted", "truncated_budget"))


class ClosedMode(unittest.TestCase):
    def test_same_public_different_truth_gives_identical_valid_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            twins = []
            for name, truth_from in (("a", "S02"), ("b", "S01")):
                snapshot = Path(tmp) / name
                shutil.copytree(FIXTURES / "S02", snapshot)
                truth = json.loads((FIXTURES / truth_from / "private" / "truth.json").read_text(encoding="utf-8"))
                truth["episode_id"] = "S02"
                (snapshot / "private" / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
                twins.append(snapshot)
            actions = [{"action": "get_document", "args": {"doc_id": "D9"}},
                       {"action": "get_record", "args": {"table": "ledger", "id": "R7"}},
                       {"action": "get_document", "args": {"doc_id": "NOPE"}},
                       {"action": "submit", "args": {"episode_id": "S02", "findings": []}},
                       {"action": "get_document", "args": {"doc_id": "D9"}}]
            transcripts = []
            for snapshot in twins:
                env = make_env("closed")
                messages = [list(env.reset(0, {"snapshot_dir": str(snapshot)}))]
                self.assertEqual(V.errors(messages[0], "#/$defs/reset"), [])
                for action in actions:
                    result = list(env.step(copy.deepcopy(action)))
                    self.assertEqual(V.errors(result, "#/$defs/stepClosed"), [])
                    messages.append(result)
                transcripts.append(json.dumps(messages, sort_keys=True))
                env.close()
            self.assertEqual(transcripts[0], transcripts[1])

    def test_closed_data_injected_at_any_depth_of_real_messages_is_rejected(self):
        samples = [entry for entry in VALID if entry["name"].startswith("S02.")
                   and entry["name"].rsplit(".", 1)[-1] in ("observation", "step", "reset", "manifest")]
        injected = 0
        for entry in samples:
            locations = []

            def collect(value, path):
                if isinstance(value, dict):
                    locations.append(path)
                    for key, child in value.items():
                        collect(child, path + (key,))
                elif isinstance(value, list):
                    for index, child in enumerate(value):
                        collect(child, path + (index,))
            collect(entry["instance"], ())
            for path in locations:
                for key in ("evidence_sets", "expected_amount_minor", "issue_id", "seed", "true_reward"):
                    mutated = copy.deepcopy(entry["instance"])
                    target = mutated
                    for part in path:
                        target = target[part]
                    if key in target:
                        continue
                    target[key] = [["ledger/R4"]]
                    injected += 1
                    self.assertNotEqual(V.errors(mutated, entry["pointer"]), [],
                                        f"{entry['name']} accepted {key} at {path}")
        self.assertGreater(injected, 2000)  # 2675 locations x keys in the current golden S02 messages


FOREIGN_KEYS = ("seed", "truth", "score", "issue_id", "evidence_sets")
FOREIGN = {"seed": 123, "truth": {"issues": [{"issue_id": "X1", "evidence_sets": [["ledger/R4"]]}]},
           "score": {"reward": 1.0}}


def keys_anywhere(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        found.update(value)
        for child in value.values():
            keys_anywhere(child, found)
    elif isinstance(value, list):
        for child in value:
            keys_anywhere(child, found)
    return found


class PolicyInput(unittest.TestCase):
    """PR #22 review: arbitrary keys in the POLICY's own request are valid input, never echoed back."""

    def test_integrator_example_both_modes(self):
        for mode in ("closed", "train"):
            with self.subTest(mode=mode):
                env = make_env(mode, evaluator="inprocess")
                env.reset(0, {"snapshot_dir": str((FIXTURES / "S02").resolve())})
                action = {"action": "calculator", "args": {"expression": "1+1", "seed": 123}}
                result = env.step(action)
                self.assertEqual((result[0]["error"]["code"], result[0]["remaining_steps"]), ("invalid_args", 99))
                self.assertEqual(V.errors(action, "#/$defs/actionEnvelope"), [])
                self.assertNotEqual(V.errors(action, "#/$defs/action"), [])
                self.assertEqual(V.errors(env._trajectory[-1], f"#/$defs/trajectoryLine{mode.title()}"), [])
                self.assertEqual(V.errors(list(result), step_pointer(mode)), [])
                env.close()

    def test_closed_looking_keys_in_requests_and_submissions_are_kept_but_not_echoed(self):
        requests = [{"action": "calculator", "args": {"expression": "1+1", "seed": 123}},
                    {"action": "get_document", "args": {"doc_id": "D9", **FOREIGN}},
                    {"action": "get_record", "args": {"table": "ledger", "id": "R4", "truth": FOREIGN["truth"]}},
                    {"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 5, "score": 1}},
                    {"action": "read_truth", "args": FOREIGN}]
        submissions = [{"episode_id": "S02", "findings": [], **FOREIGN},
                       {"episode_id": "S02", "findings": [{"type": "payment_allocation", "affected_ids": ["payment/P9"],
                                                           "claimed_amount_minor": 1, "currency": "KZT", "evidence_ids": [],
                                                           "explanation": "", "truth": {"issues": []}, "seed": 7}]}]
        for mode in ("train", "closed"):
            for index, body in enumerate(submissions):
                with self.subTest(mode=mode, submission=index), tempfile.TemporaryDirectory() as tmp:
                    env = make_env(mode, trusted_dir=Path(tmp) / "trusted")  # evaluator in its own process
                    config = {"snapshot_dir": str(FIXTURES / "S02"), "run_id": "keys"}
                    if mode == "train":
                        config["export_dir"] = str(Path(tmp) / "run")
                    observation, info = env.reset(0, config)
                    for action in requests + [{"action": "submit", "args": body}]:
                        self.assertEqual(V.errors(action, "#/$defs/actionEnvelope"), [])
                        result = list(env.step(copy.deepcopy(action)))
                        self.assertEqual(V.errors(result, step_pointer(mode)), [], action)
                        echoed = keys_anywhere(result[0]) & set(FOREIGN_KEYS)
                        self.assertEqual(echoed, set(), f"observation echoed {echoed}")
                        if mode == "closed":
                            self.assertEqual(keys_anywhere(result[4]) & set(FOREIGN_KEYS), set())
                    self.assertEqual(result[0]["error"]["code"], "invalid_submission")
                    self.assertEqual((result[1], env.final["reward"]), (-1.0 if mode == "train" else 0.0, -1.0))
                    line_pointer = f"#/$defs/trajectoryLine{mode.title()}"
                    files = [Path(tmp) / "trusted" / "keys" / "trajectory.jsonl"]
                    if mode == "train":
                        files.append(Path(tmp) / "run" / "trajectories.jsonl")
                    for path in files:
                        lines = [VALIDATE.load_json(line) for line in path.read_text(encoding="utf-8").splitlines()]
                        self.assertEqual(len(lines), len(requests) + 1)
                        self.assertEqual(lines[-1]["action"], {"action": "submit", "args": body})  # kept verbatim
                        for line in lines:
                            self.assertEqual(V.errors(line, line_pointer), [], path.name)
                            self.assertEqual(keys_anywhere(line["observation"]) & set(FOREIGN_KEYS), set())
                    env.close()

    def test_issue_23_boundary_messages_validate(self):
        for mode in ("train", "closed"):
            for depth, kept in ((64, True), (65, False), (602, False)):
                with self.subTest(mode=mode, depth=depth):
                    env = make_env(mode, evaluator="inprocess")
                    env.reset(0, {"snapshot_dir": str(FIXTURES / "S02")})
                    nested = 0
                    for _ in range(depth - 3):
                        nested = [nested]
                    action = {"action": "submit", "args": {"episode_id": "S02", "findings": nested}}
                    result = list(env.step(action))
                    self.assertEqual(V.errors(result, step_pointer(mode)), [])
                    line = env._trajectory[-1]
                    self.assertEqual(V.errors(line, f"#/$defs/trajectoryLine{mode.title()}"), [])
                    if kept:
                        self.assertEqual(line["action"], action)
                    else:
                        self.assertEqual(V.errors(line["action"]["args"], "#/$defs/unrepresentableArgs"), [])
                    env.close()


class Exports(unittest.TestCase):
    def test_fresh_train_export_and_closed_trusted_trajectory(self):
        from auditlab.policies import make_policy
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            env = make_env("train")
            run_episode(env, make_policy("reconcile", 19), 19,
                        {"snapshot_dir": str(FIXTURES / "S02"), "export_dir": str(run_dir), "policy_name": "reconcile"})
            self.assertEqual(V.errors(json.loads((run_dir / "run.json").read_text(encoding="utf-8")), "#/$defs/exportRun"), [])
            self.assertEqual(V.errors(json.loads((run_dir / "score.json").read_text(encoding="utf-8")), "#/$defs/score"), [])
            self.assertEqual(V.errors(json.loads((run_dir / "submission.json").read_text(encoding="utf-8")), "#/$defs/submission"), [])
            for line in (run_dir / "trajectories.jsonl").read_text(encoding="utf-8").splitlines():
                self.assertEqual(V.errors(VALIDATE.load_json(line), "#/$defs/trajectoryLineTrain"), [])
            env = make_env("closed", trusted_dir=Path(tmp) / "trusted")
            run_episode(env, make_policy("flag_all", 19), 19, {"snapshot_dir": str(FIXTURES / "S02"), "run_id": "c1"})
            for line in (Path(tmp) / "trusted" / "c1" / "trajectory.jsonl").read_text(encoding="utf-8").splitlines():
                self.assertEqual(V.errors(VALIDATE.load_json(line), "#/$defs/trajectoryLineClosed"), [])


class ExternalAdapter(unittest.TestCase):
    ADAPTER = HERE / "schema_adapter.py"

    def test_adapter_source_imports_only_the_standard_library(self):
        tree = ast.parse(self.ADAPTER.read_text(encoding="utf-8"))
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(names, {"importlib", "json", "sys", "decimal", "pathlib"})
        validator_tree = ast.parse((ROOT / "schemas" / "validate.py").read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(validator_tree) if isinstance(node, ast.Import)
                    for alias in node.names} | {node.module.split(".")[0] for node in ast.walk(validator_tree)
                                                if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imported & {"auditlab"}, imported)

    def test_isolated_adapter_validates_the_live_protocol_in_both_modes(self):
        for mode in ("train", "closed"):
            for label in ("S01", "S02"):
                with self.subTest(mode=mode, label=label), tempfile.TemporaryDirectory() as tmp:
                    report_path = Path(tmp) / "report.json"
                    policy = SubprocessPolicy([sys.executable, "-I", str(self.ADAPTER), str(SCHEMA_DIR), str(report_path)],
                                              env=scrubbed_env(), timeout_s=60)
                    env = make_env(mode)
                    result = run_episode(env, policy, 0, {"snapshot_dir": str(FIXTURES / label)})
                    self.assertEqual((result[2], result[3], env.final["status"]), (True, False, "submitted"))
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    self.assertEqual(report["failures"], [])
                    self.assertEqual(report["imported_auditlab"], [])
                    self.assertEqual(report["golden_mismatches"], [])
                    self.assertEqual((report["golden_valid"], report["golden_invalid"]), (len(VALID), len(INVALID)))
                    self.assertGreaterEqual(report["messages_validated"], 9)
                    self.assertEqual(env.final["inspection_steps"], report["actions_validated"])  # report precedes submit
                    score = env.final["graded"]["score"]
                    self.assertEqual((score["fp"], score["fn"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
