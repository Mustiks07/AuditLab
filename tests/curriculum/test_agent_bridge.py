import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.generator import generate_episode
from auditlab.release.transforms import read_json, write_json
from auditlab.research.curriculum.agent_bridge import ClaudeAttemptRunner, _claude_public_digest
from auditlab.research.curriculum.reduce import snapshot_sha256


class _Transport:
    isolated = True

    def __init__(self, reply):
        self.reply = reply
        self.messages = []
        self.closed = False

    def send(self, message):
        self.messages.append(message)
        return self.reply

    def close(self):
        self.closed = True


class ClaudeBridgeTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.snapshot = self.root / "snapshot"
        self.output = self.root / "result"
        generate_episode(2037, {"config_version": "1.1", "issue_counts": {"payment_allocation": 1}}, self.snapshot)
        self.issue, = read_json(self.snapshot / "private/truth.json")["issues"]
        self.config = {"protocol_id": "genesis-official-60s", "max_attempt_cost_micros": 200,
                       "max_provider_calls": 1, "max_wall_seconds": 12,
                       "agent": {"variant": "tools"}, "provider": {"provider": "anthropic"}, "docker": {}}
        self.observation = {"has_more": False, "remaining_steps": 100}

    def _runner(self, transport, *, test_double=False, check_cap=False):
        def execute(spec, raw, create_bridge, *, env_factory):
            self.assertEqual(env_factory(), {"time_limit_s": 7})
            self.assertEqual(spec["budget_usd"], 0.0002)
            self.assertEqual(spec["repetition"], 2)
            self.assertEqual(spec["seed"], 41002)
            attempt_id = spec["attempt_id"]
            bridge = create_bridge()
            try:
                reply = bridge.send({"type": "observation", "observation": self.observation, "seconds_left": 1000})
                if check_cap:
                    stopped = bridge.send({"type": "observation", "observation": self.observation, "seconds_left": 1000})
                    self.assertEqual(stopped["kind"], "budget_stop")
            finally:
                bridge.close()
            self.assertEqual(len(transport.messages), 1)
            self.assertEqual(transport.messages[0]["observation"], self.observation)
            self.assertLessEqual(transport.messages[0]["seconds_left"], 7)
            self.assertNotIn("episode_ref", transport.messages[0])
            success = reply["type"] == "action"
            evaluation = {"env_status": "submitted" if success else "truncated_timeout", "matches": [],
                          "unmatched": {}, "official": None, "internal_score": {"fn": 1} if success else None}
            status = "completed" if success else "timeout"
            record = {"attempt_id": attempt_id, "status": status, "status_detail": None, "test_double": test_double,
                      "isolated_bridge": True, "episode": {"episode_sha256": _claude_public_digest(self.snapshot)},
                      "final_answer": {"findings": []} if success else None,
                      "totals": {"provider_calls": int(success), "unpriced_calls": 0, "environment_actions": 1,
                                 "cost_usd": 0.000006 if success else 0.0, "tokens": {"input": 9, "output": 2}}}
            for directory, name, value in (("attempts", "attempt.json", record),
                                           ("trusted", "evaluation.json", evaluation)):
                target = raw / directory / attempt_id / name
                target.parent.mkdir(parents=True)
                write_json(target, value)
            return record

        return ClaudeAttemptRunner(self.config, "a" * 40, execute, lambda: transport, dict)

    def test_maps_real_executor_shape_and_preserves_trusted_refs(self):
        transport = _Transport({"type": "action", "telemetry": {"provider_calls": [
            {"status": "ok", "cost_usd": 0.0000062}]}})
        result = self._runner(transport, check_cap=True)(self.snapshot, self.output, 2, 7)
        self.assertTrue(transport.closed)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["cost_micros"], 7)
        self.assertEqual(result["provider_calls"], 1)
        self.assertEqual(result["snapshot_sha256"], snapshot_sha256(self.snapshot))
        self.assertEqual(result["failures"], [{"reason": "missed_issue", "type": self.issue["type"],
                                             "affected_ids": self.issue["affected_ids"],
                                             "issue_id": self.issue["issue_id"]}])
        self.assertEqual(read_json(Path(result["raw_attempt_ref"]))["status"], "completed")
        self.assertEqual(read_json(Path(result["raw_evaluation_ref"]))["internal_score"], {"fn": 1})
        self.assertEqual(read_json(self.output / "attempt.json"), result)

    def test_timeout_with_missing_telemetry_has_unknown_cost_and_no_financial_result(self):
        transport = _Transport({"type": "failure", "kind": "timeout", "telemetry": None})
        result = self._runner(transport)(self.snapshot, self.output, 2, 7)
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(result["cost_micros"])
        self.assertIsNone(result["failures"])
        self.assertIsNone(result["score"])

    def test_test_double_and_unisolated_bridge_do_not_produce_measured_failures(self):
        transport = _Transport({"type": "action", "telemetry": {"provider_calls": [{"status": "ok", "cost_usd": 0}]}})
        test_double = self._runner(transport, test_double=True)(self.snapshot, self.output, 2, 7)
        self.assertEqual(test_double["status"], "not_checked")
        self.assertTrue(test_double["test_double"])
        self.assertIsNone(test_double["failures"])

        transport = _Transport({})
        transport.isolated = False
        unisolated = self._runner(transport)(self.snapshot, self.root / "unisolated", 2, 7)
        self.assertTrue(transport.closed)
        self.assertEqual(transport.messages, [])
        self.assertEqual(unisolated["status"], "provider_error")
        self.assertIsNone(unisolated["failures"])


if __name__ == "__main__":
    unittest.main()
