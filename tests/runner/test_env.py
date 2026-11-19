"""API-01/03/04, ISO-02/03, MAT-04 through the real reset/step facade with the PublicSession test double.
Run: python -m unittest discover -s tests/runner -v"""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from auditlab.evaluator import oracle_submission
from auditlab.runner import make_env
from auditlab.runner.testing import FixturePublicSession

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "foundation"
INSPECT = {"action": "get_document", "args": {"doc_id": "D9"}}
BAD_INSPECT = {"action": "get_document", "args": {"doc_id": "NOPE"}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def truth_of(name):
    return json.loads((FIXTURES / name / "private" / "truth.json").read_text(encoding="utf-8"))


def make(mode="train", name="S02", **options):
    env = make_env(mode, session_opener=FixturePublicSession, evaluator="inprocess", **options)
    obs, info = env.reset(0, {"runner": {"snapshot_dir": str(FIXTURES / name), "policy": "test"}})
    return env, obs, info


def good(name="S02"):
    return {"action": "submit", "args": oracle_submission(truth_of(name))}


def empty(name="S02"):
    return {"action": "submit", "args": {"episode_id": name, "findings": []}}


class Lifecycle(unittest.TestCase):
    def test_step_before_reset_is_rejected_and_free(self):
        env = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess")
        obs, reward, term, trunc, info = env.step(INSPECT)
        self.assertEqual((obs["error"]["code"], reward, term, trunc), ("no_active_episode", 0.0, False, False))

    def test_reset_observation(self):
        env, obs, info = make()
        self.assertEqual(obs["remaining_steps"], 100)
        self.assertEqual(obs["manifest"]["episode_id"], "S02")
        self.assertIn("allocation", obs["policy"])
        self.assertEqual(set(obs["tools"]), {"list_records", "get_record", "get_document", "calculator", "submit"})
        self.assertEqual(info["max_steps"], 100)
        json.dumps([obs, info])

    def test_states_are_independent_and_reset_clears_history(self):
        a, _, _ = make()
        b, _, _ = make()
        for _ in range(5):
            a.step(INSPECT)
        self.assertEqual(b.step(INSPECT)[0]["remaining_steps"], 99)
        a.step(empty())
        obs, _ = a.reset(0, {"runner": {"snapshot_dir": str(FIXTURES / "S02")}})
        self.assertEqual(obs["remaining_steps"], 100)
        self.assertEqual(a.step(INSPECT)[0]["remaining_steps"], 99)
        self.assertEqual(a.step(INSPECT)[3:], (False, {"inspection_steps": 2}))

    def test_failed_reset_leaves_no_steppable_episode(self):
        env, _, _ = make()
        env.step(INSPECT)
        with self.assertRaises(FileNotFoundError):
            env.reset(0, {"runner": {"snapshot_dir": str(FIXTURES / "MISSING")}})
        self.assertEqual(env.step(INSPECT)[0]["error"]["code"], "no_active_episode")

    def test_reset_mid_episode_records_abandoned_attempt(self):
        with tempfile.TemporaryDirectory() as trusted:
            env = make_env("closed", session_opener=FixturePublicSession, evaluator="inprocess", trusted_dir=trusted)
            env.reset(0, {"runner": {"snapshot_dir": str(FIXTURES / "S02"), "run_id": "first"}})
            env.step(INSPECT)
            env.reset(0, {"runner": {"snapshot_dir": str(FIXTURES / "S02"), "run_id": "second"}})
            env.step(empty())
            first = json.loads((Path(trusted) / "first" / "report.json").read_text(encoding="utf-8"))
            second = json.loads((Path(trusted) / "second" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual((first["status"], first["completed"], first["evaluation"]), ("abandoned", False, None))
            self.assertEqual((second["status"], second["completed"]), ("submitted", True))

    def test_rep01_same_actions_same_trajectory(self):
        actions = [INSPECT, BAD_INSPECT, {"action": "calculator", "args": {"expression": "50000-30000"}},
                   {"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 3}}, good()]
        dumps = []
        for _ in range(2):
            env, obs, info = make()
            for action in actions:
                env.step(action)
            dumps.append(json.dumps([obs, info, env._trajectory], sort_keys=True))
            other, _, _ = make(name="S01")  # an unrelated episode in between must not matter
            other.step(empty("S01"))
        self.assertEqual(dumps[0], dumps[1])

    def test_remaining_steps_counts_every_inspection_once(self):
        env, _, _ = make()
        seen = [env.step(a)[0]["remaining_steps"] for a in (INSPECT, BAD_INSPECT, {"action": "nope", "args": {}},
                                                              {"action": "calculator", "args": {"expression": "1/0"}})]
        self.assertEqual(seen, [99, 98, 97, 96])


class Termination(unittest.TestCase):
    def test_submit_on_step_one_clean_and_error(self):
        env, _, _ = make(name="S01")
        obs, reward, term, trunc, info = env.step(empty("S01"))
        self.assertEqual((reward, term, trunc, obs["result"]), (1.0, True, False, {"status": "submitted"}))
        env, _, _ = make()
        self.assertEqual(env.step(good())[1], 1.0)

    def test_submit_on_step_100_after_99_inspections_is_terminated(self):
        env, _, _ = make(name="S01")
        for i in range(99):
            result = env.step(INSPECT if i % 2 else BAD_INSPECT)
            self.assertEqual((result[2], result[3]), (False, False))
        obs, reward, term, trunc, info = env.step(empty("S01"))
        self.assertEqual((term, trunc, obs["remaining_steps"]), (True, False, 0))
        self.assertAlmostEqual(reward, 1 - 0.002 * 99, places=12)
        self.assertEqual(env.final["inspection_steps"], 99)
        self.assertEqual(env.final["status"], "submitted")
        self.assertEqual(info["score"]["tp"], 0)

    def test_99_inspections_then_full_answer_matches_mat04_style_reward(self):
        env, _, _ = make()
        for _ in range(99):
            env.step(INSPECT)
        self.assertAlmostEqual(env.step(good())[1], 2 - 1 - 0.198, places=12)

    def test_hundredth_inspection_is_truncation_with_empty_answer(self):
        for name, expected in (("S01", 0.8), ("S02", -1.0)):
            env, _, _ = make(name=name)
            for _ in range(99):
                env.step(INSPECT)
            obs, reward, term, trunc, info = env.step(INSPECT)
            self.assertEqual((term, trunc, obs["remaining_steps"]), (False, True, 0), name)
            self.assertAlmostEqual(reward, expected, places=12)
            self.assertEqual(env.final["status"], "truncated_budget")
            self.assertEqual(env.final["submission"]["findings"], [])

    def test_hundredth_inspection_failing_is_still_truncation(self):
        env, _, _ = make(name="S01")
        for _ in range(99):
            env.step(INSPECT)
        obs, reward, term, trunc, _ = env.step({"action": "get_record", "args": {"table": "ledger", "id": "ZZ"}})
        self.assertEqual((obs["ok"], term, trunc), (False, False, True))
        self.assertAlmostEqual(reward, 0.8, places=12)

    def test_repeat_after_finish_changes_nothing(self):
        for finisher in (empty("S02"), None):
            env, _, _ = make()
            if finisher:
                env.step(finisher)
            else:
                for _ in range(100):
                    env.step(INSPECT)
            final, lines = copy.deepcopy(env.final), len(env._trajectory)
            for again in (good(), INSPECT, empty()):
                obs, reward, term, trunc, info = env.step(again)
                self.assertEqual((obs["error"]["code"], reward, info), ("episode_finished", 0.0,
                                                                       {"protocol_error": "episode_finished"}))
                self.assertEqual((term, trunc), (final["terminated"], final["truncated"]))
            self.assertEqual(env.final, final)
            self.assertEqual(len(env._trajectory), lines)

    def test_structurally_bad_submit_ends_with_minus_one(self):
        for args in ({"episode_id": "S02"}, {"episode_id": "S02", "findings": "x"},
                     {"episode_id": "WRONG", "findings": []}):
            env, _, _ = make()
            obs, reward, term, trunc, _ = env.step({"action": "submit", "args": args})
            self.assertEqual((obs["error"]["code"], reward, term, trunc), ("invalid_submission", -1.0, True, False))
            self.assertEqual(env.final["status"], "invalid_submission")

    def test_malformed_envelope_costs_nothing_and_does_not_end(self):
        env, _, _ = make()
        for bad in (None, "x", [], {}, {"args": {}}, {"action": 5, "args": {}}, {"action": None}):
            obs, reward, term, trunc, info = env.step(bad)
            self.assertEqual((obs["error"]["code"], obs["remaining_steps"], term, trunc),
                             ("malformed_action", 100, False, False))
        self.assertEqual(env.step(INSPECT)[0]["remaining_steps"], 99)

    def test_recognised_submit_with_broken_body_terminates_minus_one(self):
        for bad in ({"action": "submit"}, {"action": "submit", "args": None}, {"action": "submit", "args": []},
                    {"action": "submit", "args": "S02"}):
            env, _, _ = make()
            obs, reward, term, trunc, _ = env.step(bad)
            self.assertEqual((obs["error"]["code"], reward, term, trunc), ("invalid_submission", -1.0, True, False), bad)

    def test_extra_envelope_field_is_transport_damage(self):
        env, _, _ = make()
        obs = env.step({"action": "get_document", "args": {"doc_id": "D9"}, "debug": True})[0]
        self.assertEqual((obs["error"]["code"], obs["remaining_steps"]), ("malformed_action", 100))

    def test_tool_with_bad_args_costs_a_step(self):
        env, _, _ = make()
        for i, bad in enumerate(({"action": "get_document"}, {"action": "get_document", "args": [1]}), start=1):
            obs, reward, term, trunc, _ = env.step(bad)
            self.assertEqual((obs["error"]["code"], obs["remaining_steps"], term, trunc),
                             ("invalid_request", 100 - i, False, False))

    def test_unknown_tool_costs_one_step_with_bounded_error(self):
        env, _, _ = make()
        obs, reward, term, trunc, info = env.step({"action": "delete_everything", "args": {}})
        self.assertEqual(obs, {"ok": False, "error": {"code": "unknown_action", "message": "Unknown action."},
                               "remaining_steps": 99})
        self.assertEqual((reward, term, trunc), (0.0, False, False))


class Timeout(unittest.TestCase):
    def test_deadline_before_action_truncates_with_accepted_count(self):
        clock = Clock()
        env, _, _ = make(name="S01", clock=clock)
        env.step(INSPECT)
        env.step(BAD_INSPECT)  # failed inspections count
        env.step({"action": "nope", "args": {}})
        clock.now += 61
        obs, reward, term, trunc, info = env.step(empty("S01"))  # the late submit is not accepted
        self.assertEqual((term, trunc, obs["error"]["code"]), (False, True, "time_limit"))
        self.assertAlmostEqual(reward, 1 - 0.006, places=12)
        self.assertEqual((env.final["status"], env.final["inspection_steps"]), ("truncated_timeout", 3))
        self.assertEqual(env.step(empty("S01"))[0]["error"]["code"], "episode_finished")

    def test_time_exactly_at_limit_and_custom_limit(self):
        clock = Clock()
        env = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess", clock=clock)
        env.reset(0, {"runner": {"snapshot_dir": str(FIXTURES / "S02"), "time_limit_s": 5}})
        clock.now += 4.99
        self.assertFalse(env.step(INSPECT)[3])
        clock.now += 0.01
        self.assertTrue(env.step(INSPECT)[3])

    def test_timeout_with_missed_error_scores_minus_one(self):
        clock = Clock()
        env, _, _ = make(clock=clock)
        clock.now += 100
        self.assertEqual(env.step(INSPECT)[1], -1.0)


class ClosedMode(unittest.TestCase):
    def test_closed_channel_is_neutral_and_truth_stays_trusted(self):
        with tempfile.TemporaryDirectory() as trusted, tempfile.TemporaryDirectory() as exports:
            env, _, _ = make("closed", trusted_dir=trusted, export_dir=exports)
            for _ in range(4):
                self.assertEqual(env.step(INSPECT)[1], 0.0)
            obs, reward, term, trunc, info = env.step(good())
            self.assertEqual((reward, term, trunc, info), (0.0, True, False, {"status": "submitted"}))
            self.assertAlmostEqual(env.final["reward"], 0.992, places=12)
            report = json.loads((Path(trusted) / env._run_id / "report.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(report["true_reward"], 0.992, places=12)
            self.assertEqual(report["evaluation"]["score"]["tp"], 1)
            self.assertEqual(list(Path(exports).iterdir()), [], "closed runs are never exported publicly")
            lines = [json.loads(l) for l in (Path(trusted) / env._run_id / "trajectory.jsonl")
                     .read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row["reward"] == 0.0 for row in lines))

    def test_closed_truncation_and_timeout_also_neutral(self):
        env, _, _ = make("closed")
        for _ in range(99):
            env.step(INSPECT)
        obs, reward, term, trunc, info = env.step(INSPECT)
        self.assertEqual((reward, trunc, info), (0.0, True, {"status": "truncated_budget"}))
        self.assertEqual(env.final["reward"], -1.0)


class Leaks(unittest.TestCase):
    def sentinel_snapshot(self, tmp, name="S02"):
        snap = Path(tmp) / "snap"
        shutil.copytree(FIXTURES / name, snap)
        truth = truth_of(name)
        for issue in truth["issues"]:
            issue["issue_id"] = "SENTINEL-ISSUE-7c1d"
            issue["evidence_sets"].append(["SENTINEL-EVIDENCE-7c1d"])
        (snap / "private" / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
        (snap / "private" / "provenance.json").write_text('{"secret": "SENTINEL-PROVENANCE-7c1d"}', encoding="utf-8")
        return snap

    def run_flow(self, env, snap, submission):
        out = []
        obs, info = env.reset(0, {"runner": {"snapshot_dir": str(snap), "policy": "t"}})
        out += [obs, info]
        for action in (INSPECT, BAD_INSPECT, {"action": "nope", "args": {}},
                       {"action": "get_record", "args": {"table": "ledger", "id": "R7"}}, submission):
            out.append(env.step(action))
        return out

    def test_iso02_no_closed_marker_in_any_agent_channel(self):
        for mode in ("train", "closed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                snap = self.sentinel_snapshot(tmp)
                exports = Path(tmp) / "exports"
                env = make_env(mode, session_opener=FixturePublicSession, evaluator="inprocess",
                               export_dir=exports)
                sub = oracle_submission(truth_of("S02"))  # a policy never sees the sentinel set
                for bad in (sub, {"episode_id": "S02", "findings": []}, {"episode_id": "S02"}):
                    blob = json.dumps(self.run_flow(env, snap, {"action": "submit", "args": bad}))
                    self.assertNotIn("SENTINEL", blob)
                blob = "".join(p.read_text(encoding="utf-8") for p in exports.rglob("*")
                               if p.is_file()) if exports.exists() else ""
                self.assertNotIn("SENTINEL", blob)
                self.assertNotIn("private", blob)

    def test_iso03_same_public_snapshot_different_truth_same_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_a = Path(tmp) / "a"
            snap_b = Path(tmp) / "b"
            shutil.copytree(FIXTURES / "S02", snap_a)
            shutil.copytree(FIXTURES / "S02", snap_b)
            truth = truth_of("S01")
            truth["episode_id"] = "S02"
            (snap_b / "private" / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
            for submission in (empty(), good()):
                runs = []
                for snap in (snap_a, snap_b):
                    env = make_env("closed", session_opener=FixturePublicSession, evaluator="inprocess")
                    runs.append(self.run_flow(env, snap, submission))
                self.assertEqual(runs[0], runs[1])
                self.assertEqual(json.dumps(runs[0], sort_keys=True), json.dumps(runs[1], sort_keys=True))

    def test_session_exception_and_extra_fields_do_not_leak(self):
        class Evil:
            def __init__(self, _):
                self.calls = 0

            def inspect(self, action):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("secret path C:/private/truth.json")
                return {"ok": True, "result": 1, "debug": "/etc/passwd", "remaining_steps": 5000}

        env = make_env("closed", session_opener=Evil, evaluator="inprocess")
        env.reset(0, {"runner": {"snapshot_dir": str(FIXTURES / "S02")}})
        first = env.step(INSPECT)[0]
        second = env.step(INSPECT)[0]
        self.assertEqual(first, {"ok": False, "error": {"code": "internal_error", "message": "Internal error."},
                                 "remaining_steps": 99})
        self.assertEqual(second, {"ok": True, "result": 1, "remaining_steps": 98})
        self.assertIn("secret path", json.dumps(env._events))  # kept on the trusted side only


class SubprocessEvaluatorTest(unittest.TestCase):
    def test_real_separate_process_gives_same_reward(self):
        env = make_env("train", session_opener=FixturePublicSession, evaluator="subprocess")
        env.reset(0, {"runner": {"snapshot_dir": str(FIXTURES / "S02")}})
        for _ in range(4):
            env.step(INSPECT)
        obs, reward, term, trunc, info = env.step(good())
        self.assertAlmostEqual(reward, 0.992, places=12)
        self.assertEqual(info["score"]["tp"], 1)

    def test_evaluator_failure_is_infra_not_a_score(self):
        def broken(private_dir, submission, steps):
            raise OSError("disk gone")

        env = make_env("train", session_opener=FixturePublicSession, evaluator=broken)
        env.reset(0, {"runner": {"snapshot_dir": str(FIXTURES / "S02")}})
        obs, reward, term, trunc, info = env.step(good())
        self.assertEqual((reward, trunc, info), (0.0, True, {"status": "infra_error"}))
        self.assertEqual(env.final["status"], "infra_error")


if __name__ == "__main__":
    unittest.main()
