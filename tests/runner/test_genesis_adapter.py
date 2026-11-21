"""The official judge adapter (judge_adapter.py) against the Genesis contract checks C01-C20.

Episodes are loaded through episode_dir exactly as the judge does. Small fixtures come from
fixtures/foundation (no generator needed); tests that need the generator (pagination over more than
100 rows, generate_episode itself, a generated pair) skip where the pinned tzdata is missing
(plain Windows) and run in the AuditLab runtime image (docs/GENESIS_ADAPTER.md).

Results here are AuditLab's own checks of the published definitions, not the organizers' score.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import judge_adapter  # noqa: E402
from auditlab.evaluator.oracle import oracle_submission  # noqa: E402
from auditlab.release.transforms import transform_public, transform_truth  # noqa: E402
from auditlab.runner.genesis import AdapterError, _translate  # noqa: E402

FIXTURES = ROOT / "fixtures" / "foundation"
SEED = 987654321  # must never appear in anything a policy receives
LOOK = {"name": "list_records", "args": {"table": "payments", "cursor": None, "limit": 1}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(file.relative_to(path)).encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


def copy_episode(name: str, target: Path) -> Path:
    shutil.copytree(FIXTURES / name, target)
    return target


def generated_episode(target: Path, seed: int = 7, config: dict | None = None) -> Path:
    config = config or {"config_version": "1.1", "issue_counts": {"duplicate_invoice": 1, "payment_allocation": 1,
                                                                  "fx_conversion": 1, "period_cutoff": 1}}
    try:
        judge_adapter.generate_episode(seed, config, str(target))
    except ValueError as error:
        if "timezone_data_required" in str(error):
            raise unittest.SkipTest("generator needs the pinned tzdata (run in the runtime image)")
        raise
    return target


def official_submit(truth: dict) -> dict:
    return {"name": "submit", "args": {"findings": oracle_submission(truth)}}


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = Clock()

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **options):
        options.setdefault("evaluator", "inprocess")  # the subprocess default is covered separately
        options.setdefault("clock", self.clock)
        return judge_adapter.make_env(**options)

    def start(self, name="S02", mode="closed", **options):
        self._count = getattr(self, "_count", 0) + 1
        episode = copy_episode(name, self.tmp / f"{name}-{mode}-{self._count}")
        env = self.env(**options)
        observation, info = env.reset(SEED, {"mode": mode, "episode_dir": str(episode), "team_config": {}})
        return env, episode, observation, info

    def truth(self, episode: Path) -> dict:
        return json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))

    def assertOfficialObservation(self, observation, remaining):
        self.assertEqual(set(observation), {"result", "error", "remaining_steps"})
        self.assertEqual(observation["remaining_steps"], remaining)
        json.dumps(observation, allow_nan=False)


class ResetAndFirstObservation(AdapterCase):
    def test_c01_c02_c03_reset_pair_and_first_observation(self):
        env, episode, observation, info = self.start()
        json.dumps([observation, info], allow_nan=False)
        self.assertEqual(set(observation), {"task", "manifest", "policy", "remaining_steps"})
        self.assertEqual(observation["remaining_steps"], 100)
        self.assertIsInstance(observation["task"], str)
        self.assertIsInstance(observation["policy"], dict)
        manifest = observation["manifest"]
        for key in ("schema_version", "episode_id", "base_currency", "period_start", "period_end", "timezone",
                    "ledger_rows", "document_count", "rounding"):
            self.assertIn(key, manifest)
        public = episode / "public"
        self.assertEqual(manifest["ledger_rows"], len((public / "ledger.csv").read_text().splitlines()) - 1)
        docs = sum(len((public / f).read_text().splitlines()) for f in ("invoices.jsonl", "payments.jsonl"))
        self.assertEqual(manifest["document_count"], docs)
        self.assertEqual(info, {"episode_id": "S02", "max_steps": 100, "mode": "closed"})
        text = json.dumps([observation, info])
        for secret in (str(SEED), "X1", "evidence_sets", str(episode), "private", "truth"):
            self.assertNotIn(secret, text)

    def test_episode_dir_is_loaded_as_is(self):
        episode = copy_episode("S02", self.tmp / "given")
        before = tree_digest(episode)

        def no_generation(*_):
            raise AssertionError("the adapter must not regenerate a given episode")

        env = self.env(generator=no_generation)
        observation, _ = env.reset(SEED, {"mode": "closed", "episode_dir": str(episode), "team_config": {"x": 1}})
        self.assertEqual(observation["manifest"]["episode_id"], "S02")
        env.step(official_submit(self.truth(episode)))
        self.assertEqual(tree_digest(episode), before)  # nothing written into the judge's directory

    def test_repeated_reset_starts_the_same_episode_again(self):
        env, episode, first, _ = self.start()
        env.step(LOOK)
        env.step(LOOK)
        again, _ = env.reset(SEED, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
        self.assertEqual(again, first)
        self.assertEqual(env.step(LOOK)[0]["remaining_steps"], 99)

    def test_bad_configuration_is_a_judge_side_error(self):
        env = self.env()
        for config in ([], {"mode": "eval"}, {"mode": "closed", "episode_dir": str(self.tmp / "missing")},
                       {"mode": "train", "team_config": {"snapshot_dir": "/x"}}):
            with self.subTest(config=config), self.assertRaises(AdapterError):
                env.reset(SEED, config)


class StepContract(AdapterCase):
    def test_c04_one_list_records(self):
        env, *_ = self.start()
        observation, reward, terminated, truncated, info = env.step(
            {"name": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 50}})
        self.assertOfficialObservation(observation, 99)
        self.assertIs(type(terminated), bool)
        self.assertIs(type(truncated), bool)
        self.assertEqual((reward, terminated, truncated), (0.0, False, False))
        self.assertEqual(set(observation["result"]), {"records", "next_cursor"})
        self.assertIsNone(observation["error"])
        json.dumps(info, allow_nan=False)

    def test_c07_c08_record_and_document(self):
        env, *_ = self.start()
        record = env.step({"name": "get_record", "args": {"table": "ledger", "id": "R4"}})[0]["result"]
        self.assertEqual((record["row_id"], record["account"], record["credit_minor"]), ("R4", "UNAPPLIED", 5000000))
        document = env.step({"name": "get_document", "args": {"doc_id": "D9"}})[0]["result"]
        self.assertEqual((document["payment_id"], document["amount_minor"]), ("P9", 5000000))
        for table, key in (("invoices", "D4"), ("payments", "D9"), ("fx", None)):
            if key:
                self.assertIsNone(env.step({"name": "get_record", "args": {"table": table, "id": key}})[0]["error"])

    def test_c09_c10_calculator(self):
        env, *_ = self.start()
        self.assertEqual(env.step({"name": "calculator", "args": {"expression": "0.1+0.2"}})[0]["result"], "0.3")
        for expression in ("open('/etc/passwd').read()", "__import__('os').system('id')", "../private/truth.json"):
            observation = env.step({"name": "calculator", "args": {"expression": expression}})[0]
            self.assertIsNone(observation["result"])
            self.assertEqual(observation["error"]["code"], "invalid_expression")
        self.assertEqual(observation["remaining_steps"], 96)

    def test_c11_c12_c13_c14_errors_cost_one_step_each(self):
        env, *_ = self.start()
        cases = [
            ({"name": "get_record", "args": {"table": "ledger", "id": "R404"}}, "not_found"),
            ({"name": "get_document", "args": {"doc_id": "R404"}}, "not_found"),
            ({"name": "get_record", "args": {"table": "truth", "id": "X1"}}, "unknown_table"),
            ({"name": "drop_tables", "args": {}}, "unknown_action"),
            ({"name": "get_record"}, "invalid_request"),
            ({"name": "list_records", "args": {"table": "ledger", "cursor": "bad", "limit": 5}}, "invalid_cursor"),
            ({"action": "list_records", "args": {}}, "malformed_action"),  # internal envelope is not official
            ("list_records", "malformed_action"),
            ({"name": 5, "args": {}}, "malformed_action"),
            ({"name": "calculator", "args": {"expression": float("nan")}}, "invalid_args"),
        ]
        remaining = 100
        for action, code in cases:
            observation = env.step(action)[0]
            remaining -= 1
            with self.subTest(action=action):
                self.assertOfficialObservation(observation, remaining)
                self.assertIsNone(observation["result"])
                self.assertEqual(observation["error"]["code"], code)
                self.assertLessEqual(len(observation["error"]["message"]), 200)
        observation = env.step({"name": "get_record", "args": {"table": "ledger", "id": "R1"}})[0]
        self.assertEqual(observation["result"]["row_id"], "R1")  # access not spoiled by the errors
        self.assertEqual(observation["remaining_steps"], remaining - 1)

    def test_c15_submit_is_final(self):
        env, episode, *_ = self.start()
        observation, reward, terminated, truncated, info = env.step(official_submit(self.truth(episode)))
        self.assertEqual(observation, {"result": {"accepted": True}, "error": None, "remaining_steps": 99})
        self.assertEqual((reward, terminated, truncated), (0.0, True, False))
        before = env.trusted_result()
        self.assertEqual(before["score"], {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0,
                                           "return": 1.0})
        later = env.step({"name": "submit", "args": {"findings": {"episode_id": "S02", "findings": []}}})
        self.assertEqual(later[0]["error"]["code"], "episode_finished")
        self.assertEqual((later[1], later[2], later[3]), (0.0, True, False))
        self.assertEqual(env.trusted_result(), before)

    def test_c16_submit_as_the_hundredth_action(self):
        env, episode, *_ = self.start()
        for index in range(99):
            observation, _, terminated, truncated, _ = env.step(LOOK)
            self.assertFalse(terminated or truncated, index)
        self.assertEqual(observation["remaining_steps"], 1)
        observation, _, terminated, truncated, _ = env.step(official_submit(self.truth(episode)))
        self.assertEqual((observation["result"], terminated, truncated), ({"accepted": True}, True, False))
        self.assertEqual(observation["remaining_steps"], 0)
        result = env.trusted_result()
        self.assertEqual((result["inspection_steps"], result["score"]["tp"]), (99, 1))
        self.assertAlmostEqual(result["score"]["return"], 1 - 0.002 * 99, delta=1e-9)

    def test_c17_hundred_inspections_truncate_with_empty_findings(self):
        env, *_ = self.start()
        for _ in range(99):
            env.step({"name": "unknown", "args": {}})
        observation, reward, terminated, truncated, _ = env.step(LOOK)
        self.assertEqual((terminated, truncated, observation["remaining_steps"]), (False, True, 0))
        result = env.trusted_result()
        self.assertEqual(result["status"], "truncated_budget")
        self.assertEqual((result["score"]["tp"], result["score"]["fp"], result["score"]["fn"]), (0, 0, 1))
        late = env.step({"name": "submit", "args": {"findings": {"episode_id": "S02", "findings": []}}})
        self.assertEqual(late[0]["error"]["code"], "episode_finished")
        self.assertEqual(env.trusted_result(), result)

    def test_c18_deadline_counts_from_entry_into_reset(self):
        clock = self.clock

        def slow_opener(public_dir):  # 59.5 s spent loading inside reset
            from auditlab.environment import open_session
            clock.now += 59.5
            return open_session(public_dir)

        env, episode, *_ = self.start(session_opener=slow_opener)
        self.assertAlmostEqual(env.seconds_left(), 0.5)
        clock.now += 0.5  # policy thinking time counts too
        observation, _, terminated, truncated, _ = env.step(official_submit(self.truth(episode)))
        self.assertEqual((terminated, truncated, observation["error"]["code"]), (False, True, "time_limit"))
        result = env.trusted_result()
        self.assertEqual((result["status"], result["score"]["tp"], result["score"]["fn"]), ("truncated_timeout", 0, 1))

    def test_judge_can_stop_a_policy_that_never_answers(self):
        env, *_ = self.start()
        env.step(LOOK)
        self.clock.now += 61
        observation, _, terminated, truncated, _ = env.expire()
        self.assertEqual((terminated, truncated, observation["error"]["code"]), (False, True, "time_limit"))
        self.assertEqual(env.trusted_result()["inspection_steps"], 1)

    def test_judge_can_abort_a_crashed_policy(self):
        env, *_ = self.start()
        env.step(LOOK)
        observation, _, terminated, truncated, _ = env.abort("provider_error: upstream 529")
        self.assertEqual((terminated, truncated, observation["error"]["code"]), (False, True, "aborted"))
        self.assertEqual(env.trusted_result()["status"], "policy_failure")  # not a financial answer
        self.clock.now += 61
        env2, *_ = self.start()
        self.clock.now += 61
        env2.abort("late")
        self.assertEqual(env2.trusted_result()["status"], "truncated_timeout")  # past the deadline it is a timeout

    def test_c19_closed_mode_hides_reward_and_diagnostics(self):
        env, episode, *_ = self.start()
        seen = [env.step(LOOK) for _ in range(3)]
        seen.append(env.step(official_submit(self.truth(episode))))
        for observation, reward, terminated, truncated, info in seen:
            self.assertEqual(reward, 0.0)
            self.assertIs(type(reward), float)
            self.assertNotIn("score", info)
            text = json.dumps([observation, info])
            for secret in ("tp", "reward", "X1", "matches", "0.994"):
                self.assertNotIn(f'"{secret}"', text)
        self.assertAlmostEqual(env.trusted_result()["score"]["return"], 1 - 0.002 * 3, delta=1e-9)

    def test_train_mode_returns_the_terminal_reward(self):
        env, episode, *_ = self.start(mode="train")
        env.step(LOOK)
        _, reward, terminated, _, info = env.step(official_submit(self.truth(episode)))
        self.assertTrue(terminated)
        self.assertAlmostEqual(reward, 0.998, delta=1e-9)
        self.assertEqual(info["score"]["reward"], reward)

    def test_invalid_submit_through_the_environment(self):
        bodies = [
            {"name": "submit", "args": {"episode_id": "S02", "findings": []}},  # internal shape, not official
            {"name": "submit", "args": {"findings": {"episode_id": "S02", "findings": [{}] * 31}}},
            {"name": "submit"},
        ]
        for body in bodies:
            env, *_ = self.start()
            observation, reward, terminated, truncated, _ = env.step(body)
            with self.subTest(body=str(body)[:60]):
                self.assertEqual((observation["result"], observation["error"]["code"]), (None, "invalid_submission"))
                self.assertEqual((reward, terminated, truncated), (0.0, True, False))
                self.assertEqual(env.trusted_result()["score"], {"tp": None, "fp": None, "fn": None, "precision": None,
                                                                 "recall": None, "f1": None, "return": -1.0})

    def test_environment_and_score_episode_agree(self):
        env, episode, *_ = self.start()
        truth = self.truth(episode)
        submitted = oracle_submission(truth)
        submitted["findings"].append(dict(submitted["findings"][0], explanation="repeat"))
        for _ in range(4):
            env.step(LOOK)
        env.step({"name": "submit", "args": {"findings": submitted}})
        self.assertEqual(env.trusted_result()["score"], judge_adapter.score_episode(str(episode), submitted, 4))

    def test_subprocess_evaluator_is_the_default(self):
        episode = copy_episode("S02", self.tmp / "sub")
        env = judge_adapter.make_env()
        try:
            env.reset(SEED, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
            env.step(official_submit(self.truth(episode)))
            self.assertEqual(env.trusted_result()["score"]["tp"], 1)
            self.assertEqual(env.trusted_result()["evaluation"]["rules"], "genesis-2026-11-20")
        finally:
            env.close()


class Translation(unittest.TestCase):
    """The adapter translates; it never picks or alters a policy's choice."""

    def test_one_to_one_translation(self):
        cases = [
            (LOOK, {"action": "list_records", "args": LOOK["args"]}),
            ({"name": "calculator", "args": {"expression": "1+1"}}, {"action": "calculator", "args": {"expression": "1+1"}}),
            ({"name": "anything", "args": [1]}, {"action": "anything", "args": [1]}),
            ({"name": "get_record"}, {"action": "get_record"}),
            ({"name": "submit", "args": {"findings": {"episode_id": "E", "findings": []}}},
             {"action": "submit", "args": {"episode_id": "E", "findings": []}}),
        ]
        for official, internal in cases:
            self.assertEqual(_translate(official), (internal, False))
        self.assertEqual(_translate({"name": "x", "args": {}, "extra": 1})[1], True)


class NoLeaks(AdapterCase):
    def walk(self, env):
        """Read every public record and document, then provoke every error the tools have."""
        seen = []
        for table in ("ledger", "invoices", "payments", "fx", "policy"):
            cursor = None
            while True:
                observation = env.step({"name": "list_records", "args": {"table": table, "cursor": cursor, "limit": 50}})
                seen.append(observation)
                result = observation[0]["result"]
                if not result or not result["next_cursor"]:
                    break
                cursor = result["next_cursor"]
        for action in ({"name": "get_document", "args": {"doc_id": "D9"}}, {"name": "get_record", "args": {}},
                       {"name": "calculator", "args": {"expression": "1/0"}}, {"name": "shell", "args": {"cmd": "ls"}},
                       {"name": "get_record", "args": {"table": "../private/truth.json", "id": "x"}},
                       {"name": "get_document", "args": {"doc_id": "../../private/truth.json"}}):
            seen.append(env.step(action))
        return seen

    def check(self, episode, seen):
        truth = self.truth(episode)
        provenance_file = episode / "private" / "provenance.json"
        provenance = json.loads(provenance_file.read_text(encoding="utf-8")) if provenance_file.is_file() else {}
        text = json.dumps(seen, ensure_ascii=False)
        secrets = [str(SEED), str(episode), str(episode.parent), "evidence_sets", "expected_amount_minor",
                   "issue_id", "provenance", "truth.json", "Traceback", "mutations"]
        secrets += [item["issue_id"] for item in truth["issues"]]
        if "seed" in provenance:
            secrets.append(f'"seed": {provenance["seed"]}')
        for secret in secrets:
            self.assertNotIn(secret, text)

    def test_c20_fixture_episodes(self):
        for name in ("S01", "S02"):
            with self.subTest(name=name):
                env, episode, first, info = self.start(name)
                seen = [first, info, *self.walk(env), env.step(official_submit(self.truth(episode)))]
                self.check(episode, seen)

    def test_c20_generated_episode_and_c05_c06_pagination(self):
        episode = generated_episode(self.tmp / "generated")
        env = self.env()
        first, info = env.reset(SEED, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})
        ledger_rows = (episode / "public" / "ledger.csv").read_text(encoding="utf-8").splitlines()[1:]
        self.assertGreater(len(ledger_rows), 100)  # fixture L: at least three pages
        pages, cursor, seen_ids = 0, None, []
        while True:
            observation = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": cursor, "limit": 50}})[0]
            records = observation["result"]["records"]
            self.assertLessEqual(len(records), 50)
            seen_ids += [record["row_id"] for record in records]
            pages += 1
            if pages == 2:  # C06: the same cursor twice gives the same page
                repeat = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": cursor, "limit": 50}})
                self.assertEqual(repeat[0]["result"], observation["result"])
            cursor = observation["result"]["next_cursor"]
            if cursor is None:
                break
        self.assertGreaterEqual(pages, 3)
        self.assertEqual(sorted(seen_ids), sorted(row.split(",", 1)[0] for row in ledger_rows))
        self.assertEqual(len(seen_ids), len(set(seen_ids)))
        self.check(episode, [first, info, *self.walk(env), env.step(official_submit(self.truth(episode)))])


class Pairs(AdapterCase):
    """Block 4: order and ID transformation of a copy, oracle with mapped IDs, equal step cost."""

    def run_oracle(self, episode: Path, looks: int):
        env = self.env()
        trace = [env.reset(SEED, {"mode": "closed", "episode_dir": str(episode), "team_config": {}})]
        for _ in range(looks):
            trace.append(env.step(LOOK))
        trace.append(env.step(official_submit(self.truth(episode))))
        return trace, env.trusted_result()

    def transformed(self, source: Path, seed: int) -> tuple[Path, dict]:
        target = self.tmp / f"{source.name}-T{seed}"
        mapping = transform_public(source / "public", target / "public", seed)
        (target / "private").mkdir()
        (target / "private" / "truth.json").write_text(
            json.dumps(transform_truth(self.truth(source), mapping)), encoding="utf-8")
        return target, mapping

    def assertPair(self, source: Path):
        original_digest = tree_digest(source)
        trace_a, result_a = self.run_oracle(source, 3)
        trace_b, _ = self.run_oracle(source, 3)
        self.assertEqual(trace_a, trace_b)  # exact repeat of observations, rewards and flags
        target, mapping = self.transformed(source, 11)
        _, result_t = self.run_oracle(target, 3)
        self.assertEqual(tree_digest(source), original_digest)  # the source episode is never rewritten
        self.assertEqual(result_t["score"], result_a["score"])
        self.assertEqual(result_a["score"]["fp"], 0)
        inverse = {new: old for old, new in mapping["references"].items()}
        submitted = oracle_submission(self.truth(target))["findings"]
        original = oracle_submission(self.truth(source))["findings"]
        back = sorted((f["type"], sorted(inverse.get(a, a) for a in f["affected_ids"]), f["claimed_amount_minor"],
                       f["currency"], sorted(inverse.get(e, e) for e in f["evidence_ids"])) for f in submitted)
        self.assertEqual(back, sorted((f["type"], sorted(f["affected_ids"]), f["claimed_amount_minor"], f["currency"],
                                       sorted(f["evidence_ids"])) for f in original))

    def test_fixture_pair(self):
        self.assertPair(copy_episode("S02", self.tmp / "S02"))

    def test_generated_pair(self):
        self.assertPair(generated_episode(self.tmp / "G"))


class GenerateEpisode(AdapterCase):
    def test_wrapper_layout_determinism_and_no_overwrite(self):
        first = generated_episode(self.tmp / "new", seed=3)
        empty = self.tmp / "empty"
        empty.mkdir()
        generated_episode(empty, seed=3)
        self.assertEqual(tree_digest(first), tree_digest(empty))
        self.assertEqual(sorted(p.name for p in empty.iterdir()), ["private", "public"])
        with self.assertRaises(AdapterError):
            judge_adapter.generate_episode(3, {}, str(empty))
        wrapped = self.tmp / "wrapped"
        judge_adapter.generate_episode(3, {"team_config": {"config_version": "1.1", "issue_counts": {
            "duplicate_invoice": 1, "payment_allocation": 1, "fx_conversion": 1, "period_cutoff": 1}}}, str(wrapped))
        self.assertEqual(tree_digest(wrapped), tree_digest(first))

    def test_train_reset_without_episode_dir_uses_the_generator(self):
        env, other = self.env(), self.env()
        self.addCleanup(env.close)
        self.addCleanup(other.close)
        try:
            observation, info = env.reset(5, {"mode": "train", "team_config": {"config_version": "1.1"}})
        except Exception as error:  # noqa: BLE001
            if "timezone_data_required" in str(error):
                self.skipTest("generator needs the pinned tzdata (run in the runtime image)")
            raise
        self.assertEqual(observation["remaining_steps"], 100)
        again, _ = other.reset(5, {"mode": "train", "team_config": {"config_version": "1.1"}})
        self.assertEqual(again, observation)  # same seed and team_config, same episode


if __name__ == "__main__":
    unittest.main()
