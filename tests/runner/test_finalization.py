"""Issue #23: hostile nesting and non-JSON input must end in exactly one well-formed finalization.

Run: python -m unittest discover -s tests/runner -p "test_finalization.py" -v

Boundary chosen (SPEC §7-8, case PDF p.3 §05 "Неверная структура ... награда -1"):
* JSON text the host cannot decode (NaN literal, nesting beyond the decoder, huge integers) is a
  transport failure of the policy process -> policy_failure, never scored as a submission;
* in-process values that are not JSON (set, object, NaN, non-string keys, ...) are a damaged packet
  -> malformed_action: no step, no trajectory line, the episode continues;
* valid JSON nested deeper than 64 levels or larger than 1 MiB is a request beyond the published
  limits -> recognised submit: invalid_submission (-1 train, neutral 0 closed); tool: invalid_request.
"""
import copy
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from auditlab.runner import make_env
from auditlab.runner import env as env_module
from auditlab.runner.episode import run_episode
from auditlab.runner.isolation import SubprocessPolicy, scrubbed_env
from auditlab.runner.wire import MAX_ACTION_DEPTH, json_snapshot

ROOT = Path(__file__).resolve().parents[2]
S02 = str(ROOT / "fixtures" / "foundation" / "S02")
GOOD = {"type": "payment_allocation", "affected_ids": ["payment/P9", "invoice/I4"],
        "claimed_amount_minor": 2_000_000, "currency": "KZT",
        "evidence_ids": ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"], "explanation": "x"}
D9 = {"action": "get_document", "args": {"doc_id": "D9"}}
MARK = {"$unrepresentable": "depth_limit"}


def nest(depth):
    """A value whose deepest scalar sits `depth` levels down (a bare scalar has depth 1)."""
    value = 0
    for _ in range(depth - 1):
        value = [value]
    return value


def deep_submit(total_depth):
    """submit whose whole action object is exactly `total_depth` levels deep."""
    return {"action": "submit", "args": {"episode_id": "S02", "findings": nest(total_depth - 2)}}


def deep_tool(total_depth):
    return {"action": "get_document", "args": {"doc_id": nest(total_depth - 2)}}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class CountingEvaluator:
    def __init__(self):
        self.calls = 0
        self.inner = env_module.InProcessEvaluator()

    def __call__(self, private_dir, submission, steps):
        self.calls += 1
        return self.inner(private_dir, submission, steps)


def fresh(mode, **options):
    env = make_env(mode, **options)
    env.reset(0, {"snapshot_dir": S02})
    return env


class DepthBoundary(unittest.TestCase):
    def test_walker_matches_the_published_limit(self):
        self.assertEqual(MAX_ACTION_DEPTH, 64)
        self.assertIsNone(json_snapshot(deep_submit(64))[1])
        self.assertEqual(json_snapshot(deep_submit(65))[1], "depth_limit")
        self.assertEqual(json_snapshot(nest(200_000))[1], "depth_limit")  # iterative: no RecursionError

    def test_issue_23_example_both_modes_direct_api(self):
        for mode, public in (("train", -1.0), ("closed", 0.0)):
            with self.subTest(mode=mode):
                env = fresh(mode)  # real session, evaluator in its own process
                action = json.loads(json.dumps(deep_submit(602)))  # findings of depth 600
                obs, reward, term, trunc, info = env.step(action)
                self.assertEqual(obs, {"ok": False, "error": {"code": "invalid_submission",
                                       "message": "Submission failed schema validation."}, "remaining_steps": 99})
                self.assertEqual((reward, term, trunc, info["status"]), (public, True, False, "invalid_submission"))
                self.assertEqual("score" in info, mode == "train")
                self.assertEqual((env._phase, env.final["status"], env.final["reward"]), ("done", "invalid_submission", -1.0))
                self.assertEqual(env.final["submission"], MARK)
                self.assertEqual([row["action"] for row in env._trajectory], [{"action": "submit", "args": MARK}])
                self.assertIn({"event": "action_rejected", "action": "submit", "reason": "depth_limit"}, env._events)
                frozen, rows = copy.deepcopy(env.final), copy.deepcopy(env._trajectory)
                for again in (action, D9, deep_submit(64)):
                    self.assertEqual(env.step(again)[0]["error"]["code"], "episode_finished")
                self.assertEqual(env.abort("late crash")[4], {"protocol_error": "episode_finished"})
                self.assertEqual(env.expire()[4], {"protocol_error": "episode_finished"})
                self.assertEqual((env.final, env._trajectory), (frozen, rows))
                env.close()

    def test_depth_64_reaches_the_evaluator_and_65_is_rejected_by_the_runner(self):
        for depth, recorded in ((64, deep_submit(64)), (65, {"action": "submit", "args": MARK})):
            with self.subTest(depth=depth):
                counter = CountingEvaluator()
                env = fresh("train", evaluator=counter)
                obs, reward, term, trunc, info = env.step(deep_submit(depth))
                self.assertEqual((obs["error"]["code"], reward, term, trunc), ("invalid_submission", -1.0, True, False))
                self.assertEqual(env._trajectory[-1]["action"], recorded)
                self.assertEqual(counter.calls, 1, "graded exactly once")
                self.assertEqual(any(e.get("event") == "action_rejected" for e in env._events), depth == 65)

    def test_deep_tool_arguments_spend_one_step(self):
        env = fresh("closed")
        obs = env.step(deep_tool(64))[0]  # forwarded: the session answers invalid_args
        self.assertEqual((obs["error"]["code"], obs["remaining_steps"]), ("invalid_args", 99))
        self.assertEqual(env._trajectory[-1]["action"], deep_tool(64))
        obs = env.step(deep_tool(65))[0]  # beyond the limit: never forwarded
        self.assertEqual((obs["error"]["code"], obs["remaining_steps"]), ("invalid_request", 98))
        self.assertEqual(env._trajectory[-1]["action"], {"action": "get_document", "args": MARK})
        obs = env.step({"action": "read_truth", "args": nest(500)})[0]
        self.assertEqual((obs["error"]["code"], obs["remaining_steps"]), ("unknown_action", 97))
        self.assertEqual(env.step(D9)[0]["remaining_steps"], 96)
        self.assertEqual(len(env._trajectory), 4)
        env.close()

    def test_size_limit_is_an_invalid_submission(self):
        env = fresh("train", evaluator="inprocess")
        big = {"action": "submit", "args": {"episode_id": "S02", "findings": [0] * 1_100_000}}
        obs, reward, term, _, _ = env.step(big)
        self.assertEqual((obs["error"]["code"], reward, term), ("invalid_submission", -1.0, True))
        self.assertEqual(env.final["submission"], {"$unrepresentable": "size_limit"})

    def test_budget_and_timeout_finalize_once_with_hostile_last_action(self):
        env = fresh("train", evaluator="inprocess")
        for _ in range(99):
            env.step(D9)
        obs, reward, term, trunc, info = env.step(deep_tool(300))
        self.assertEqual((obs["error"]["code"], obs["remaining_steps"], term, trunc, info["status"]),
                         ("invalid_request", 0, False, True, "truncated_budget"))
        self.assertEqual(env._trajectory[-1]["action"], {"action": "get_document", "args": MARK})
        self.assertEqual(len(env._trajectory), 100)
        for hostile, recorded in ((deep_submit(600), {"action": "submit", "args": MARK}),
                                  ({"action": "submit", "args": {"x": {1, 2}}},
                                   {"action": "submit", "args": {"$unrepresentable": "not_json"}}),
                                  (float("nan"), {"$unrepresentable": "not_json"})):
            with self.subTest(late=str(recorded)):
                clock, counter = Clock(), CountingEvaluator()
                env = fresh("closed", clock=clock, evaluator=counter)
                env.step(D9)
                clock.now += 61
                obs, reward, term, trunc, info = env.step(hostile)
                self.assertEqual((obs["error"]["code"], reward, term, trunc, info), ("time_limit", 0.0, False, True,
                                                                                    {"status": "truncated_timeout"}))
                self.assertEqual(env._trajectory[-1]["action"], recorded)
                self.assertEqual((env.final["inspection_steps"], counter.calls), (1, 1))


class NonJsonInProcess(unittest.TestCase):
    class Weird(str):
        pass

    CASES = {
        "set": {"x": {1, 2}}, "object": {"x": object()}, "nan": {"x": math.nan}, "inf": {"x": math.inf},
        "minus_inf": {"x": -math.inf}, "bytes": {"x": b"D9"}, "tuple": {"x": (1, 2)}, "int_key": {1: "x"},
        "bigint": {"x": 10 ** 5000}, "str_subclass": {"x": Weird("D9")},
    }

    def test_non_json_values_are_damaged_packets_not_submissions(self):
        for name, args in self.CASES.items():
            for kind in ("submit", "get_document"):
                with self.subTest(value=name, action=kind):
                    counter = CountingEvaluator()
                    env = fresh("train", evaluator=counter)
                    env.step(D9)
                    obs, reward, term, trunc, info = env.step({"action": kind, "args": args})
                    self.assertEqual((obs["error"]["code"], obs["remaining_steps"], reward, term, trunc, info),
                                     ("malformed_action", 99, 0.0, False, False, {"protocol_error": "malformed_action"}))
                    self.assertEqual((len(env._trajectory), env._phase, counter.calls), (1, "active", 0))
                    self.assertIn({"event": "malformed_action", "reason": "not_json"}, env._events)
                    obs, reward, term, _, _ = env.step({"action": "submit", "args": {"episode_id": "S02", "findings": [GOOD]}})
                    self.assertEqual((obs["ok"], term, round(reward, 12)), (True, True, 0.998))  # still gradeable
                    self.assertEqual(counter.calls, 1)


class Persistence(unittest.TestCase):
    def test_export_and_trusted_journal_record_the_rejected_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, trusted = Path(tmp) / "run", Path(tmp) / "trusted"
            env = make_env("train", trusted_dir=trusted)
            env.reset(0, {"snapshot_dir": S02, "export_dir": str(run_dir), "run_id": "deep"})
            env.step(deep_submit(602))
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual((run["status"], run["outcome"]), ("completed", "invalid_submission"))
            self.assertEqual(json.loads((run_dir / "submission.json").read_text(encoding="utf-8")), MARK)
            self.assertEqual(json.loads((run_dir / "score.json").read_text(encoding="utf-8"))["reward"], -1.0)
            lines = [json.loads(line) for line in (run_dir / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([line["action"] for line in lines], [{"action": "submit", "args": MARK}])
            report = json.loads((trusted / "deep" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual((report["status"], report["true_reward"], report["completed"]), ("invalid_submission", -1.0, True))
            self.assertIn({"event": "action_rejected", "action": "submit", "reason": "depth_limit"}, report["events"])
            env.close()

    def test_failed_writes_are_recorded_and_never_half_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, trusted = Path(tmp) / "run", Path(tmp) / "trusted"
            env = make_env("train", evaluator="inprocess", trusted_dir=trusted)
            env.reset(0, {"snapshot_dir": S02, "export_dir": str(run_dir), "run_id": "w"})
            with mock.patch.object(env_module.export_mod, "write_public_run", side_effect=OSError("disk full")):
                obs, reward, term, _, _ = env.step(deep_submit(602))
            self.assertEqual((obs["error"]["code"], reward, term, env._phase), ("invalid_submission", -1.0, True, "done"))
            self.assertEqual(env.final["persistence"], ["export: OSError: disk full"])
            self.assertFalse(run_dir.exists(), "a failed export is not presented as a completed run")
            report = json.loads((trusted / "w" / "report.json").read_text(encoding="utf-8"))
            self.assertIn("export_failure", [event["event"] for event in report["events"]])
            env = make_env("closed", evaluator="inprocess", trusted_dir=trusted)
            env.reset(0, {"snapshot_dir": S02, "run_id": "w2"})
            with mock.patch.object(env_module.export_mod, "write_trusted_report", side_effect=OSError("ro")):
                env.step(D9)
                result = env.step({"action": "submit", "args": {"episode_id": "S02", "findings": []}})
            self.assertEqual((result[2], env.final["status"], env.final["persistence"]),
                             (True, "submitted", ["trusted_report: OSError: ro"]))
            self.assertEqual(env.step(D9)[0]["error"]["code"], "episode_finished")


class ExternalJsonl(unittest.TestCase):
    """The real policy channel: a child process prints one JSON line per observation."""

    def run_child(self, mode, payload_code, tmp):
        trusted, run_dir = Path(tmp) / "trusted", Path(tmp) / "run"
        code = ("import sys\nfor line in sys.stdin:\n    " + payload_code + "\n    sys.stdout.flush()\n")
        policy = SubprocessPolicy([sys.executable, "-I", "-c", code], env=scrubbed_env(), timeout_s=30)
        env = make_env(mode, trusted_dir=trusted)
        config = {"snapshot_dir": S02, "run_id": "jsonl"}
        if mode == "train":
            config["export_dir"] = str(run_dir)
        result = run_episode(env, policy, 0, config)
        report = json.loads((trusted / "jsonl" / "report.json").read_text(encoding="utf-8"))
        env.close()
        return env, result, report, run_dir

    def test_deep_json_text_is_an_invalid_submission_in_both_modes(self):
        text = '{"action":"submit","args":{"episode_id":"S02","findings":' + "[" * 600 + "0" + "]" * 600 + "}}"
        for mode in ("train", "closed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                env, result, report, run_dir = self.run_child(mode, f"print({text!r})", tmp)
                self.assertEqual((result[2], result[3], result[4]["status"]), (True, False, "invalid_submission"))
                self.assertEqual(result[1], -1.0 if mode == "train" else 0.0)
                self.assertEqual((report["status"], report["true_reward"], report["completed"]), ("invalid_submission", -1.0, True))
                self.assertIn({"event": "action_rejected", "action": "submit", "reason": "depth_limit"}, report["events"])
                trajectory = [json.loads(line) for line in (Path(tmp) / "trusted" / "jsonl" / "trajectory.jsonl")
                              .read_text(encoding="utf-8").splitlines()]
                self.assertEqual([row["action"] for row in trajectory], [{"action": "submit", "args": MARK}])
                if mode == "train":
                    self.assertEqual(json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"], "completed")
                else:
                    self.assertTrue(all(row["reward"] == 0 for row in trajectory))

    def test_undecodable_json_text_is_a_policy_failure_not_a_submission(self):
        cases = {"nan_literal": 'print(\'{"action":"submit","args":{"episode_id":"S02","findings":NaN}}\')',
                 "beyond_decoder_depth": 'print(\'{"action":"submit","args":{"episode_id":"S02","findings":\' + "[" * 5000 + "]" * 5000 + "}}")',
                 "huge_integer": 'print(\'{"action":"submit","args":{"episode_id":"S02","findings":\' + "9" * 5000 + "}}")'}
        for name, code in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                env, result, report, _ = self.run_child("closed", code, tmp)
                self.assertEqual((result[2], result[3], env.final["status"]), (False, True, "policy_failure"))
                self.assertEqual(report["status"], "policy_failure")
                self.assertFalse(report["completed"])
                self.assertEqual(env.final["submission"], {"episode_id": "S02", "findings": []})


if __name__ == "__main__":
    unittest.main()
