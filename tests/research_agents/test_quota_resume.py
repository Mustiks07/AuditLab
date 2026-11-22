"""Quota handling and resume of real-model runs (Issue #42, agent-improvement v2).

HTTP 429 is a service outcome: it never becomes an auditor miss, a timeout or a false accusation.
Slots refused by the service are retried finitely after a cooldown with new attempt ids; every
attempt stays in the record; an exhausted (daily) quota stops the run with an explicit reason;
finished slots are never run, and never paid for, again. ScriptedProvider is a labelled TEST DOUBLE.
"""
from __future__ import annotations

import http.server
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auditlab.research.agents import experiment  # noqa: E402
from auditlab.research.agents.openai_chat import _quota_details, http_request  # noqa: E402
from auditlab.research.agents.providers import ProviderError, ProviderRequest, ScriptedProvider, scripted_turn  # noqa: E402
from auditlab.research.agents.reliability import ModelCaller, RetryPolicy, SpendGuard  # noqa: E402

FIXTURES = ROOT / "fixtures" / "foundation"


def google_429(scope: str, delay: str = "17s") -> bytes:
    quota = {"minute": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
             "day": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}[scope]
    return json.dumps([{"error": {"code": 429, "message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED",
                                  "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                               "violations": [{"quotaMetric": "generativelanguage.googleapis.com/x",
                                                               "quotaId": quota}]},
                                              {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}}]).encode()


class QuotaParsing(unittest.TestCase):
    def test_google_details(self):
        self.assertEqual(_quota_details(google_429("minute")), (17.0, "minute"))
        self.assertEqual(_quota_details(google_429("day", "3600s")), (3600.0, "day"))
        self.assertEqual(_quota_details(json.loads(google_429("minute"))[0].__repr__().encode()), (None, None))
        self.assertEqual(_quota_details(b"not json"), (None, None))
        self.assertEqual(_quota_details(b'{"error": {"message": "slow down"}}'), (None, None))

    def test_http_mapping_against_a_local_server(self):
        bodies = {"/minute": (google_429("minute"), {}), "/day": (google_429("day"), {}),
                  "/header": (b'{"error":{"message":"x"}}', {"Retry-After": "9"})}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body, headers = bodies[self.path]
                self.send_response(429)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # local TEST DOUBLE server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            cases = {"/minute": ("rate_limit", 17.0), "/day": ("quota_exhausted", 17.0), "/header": ("rate_limit", 9.0)}
            for path, (kind, retry_after) in cases.items():
                with self.subTest(path=path), self.assertRaises(ProviderError) as caught:
                    http_request("POST", base + path, key="sentinel-key-value", body=b"{}", timeout_s=5)
                self.assertEqual((caught.exception.kind, caught.exception.retry_after), (kind, retry_after))
                self.assertNotIn("sentinel-key-value", str(caught.exception))
            self.assertFalse(ProviderError("quota_exhausted", "x").retryable)
        finally:
            server.shutdown()


class Pacing(unittest.TestCase):
    def test_min_interval_between_calls_and_deadline(self):
        now = [0.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        provider = ScriptedProvider([scripted_turn("calculator", {"expression": "1"})] * 3)
        caller = ModelCaller(provider, RetryPolicy(min_interval_s=4.0), SpendGuard(None, allow_unpriced=True),
                             clock=lambda: now[0], sleep=sleep)
        request = lambda: ProviderRequest("s", [{"role": "user", "text": "x"}], [], 100, 0.0)  # noqa: E731
        caller.call(request(), 100.0)
        now[0] += 1.0
        caller.call(request(), 100.0)
        self.assertEqual(slept, [3.0])  # waited only the rest of the 4 s interval
        with self.assertRaises(ProviderError) as caught:
            caller.call(request(), now[0] + 2.0)  # pacing cannot fit before the deadline
        self.assertEqual(caught.exception.kind, "rate_limit")


class ExperimentService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.episode = self.tmp / "S01"
        shutil.copytree(FIXTURES / "S01", self.episode)
        self.out = self.tmp / "out"
        self.waits = []

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, repetitions=2, **service):
        plan = {"schema_version": "1.0", "artifact_id": "quota-test", "protocol_id": "auditlab-research-600s",
                "purpose": "selection", "repetitions": repetitions, "transport": "in_process",
                "episodes": [{"episode_ref": self.episode.as_posix(), "family_id": "S01", "split": "dev"}],
                "arms": [{"name": "td", "agent": {"kind": "llm", "variant": "tools", "retry": {"max_tries": 1}},
                          "provider": {"provider": "test_double"}}],
                "service": {"cooldown_s": 30.0, **service}}
        path = self.tmp / f"plan-{len(list(self.tmp.glob('plan-*')))}.json"
        path.write_text(json.dumps(plan))
        return experiment.load_plan(path)

    def run_plan(self, plan, sha, script):
        provider = ScriptedProvider(script)
        return experiment.run_experiment(plan, sha, self.out, provider_override=provider, log=lambda _: None,
                                         sleep=self.waits.append)

    def attempts(self):
        return {p.parent.name: json.loads(p.read_text()) for p in (self.out / "attempts").glob("*/attempt.json")}

    @staticmethod
    def submit():
        return scripted_turn("submit", {"episode_id": "S01", "findings": []})

    def test_rate_limit_is_a_service_outcome_retried_with_new_ids(self):
        plan, sha = self.plan()
        limited = ProviderError("rate_limit", "HTTP 429", status=429, retry_after=45.0)
        summary = self.run_plan(plan, sha, [limited, self.submit(), self.submit()])
        attempts = self.attempts()
        self.assertEqual(len(attempts), 3)  # rep0 rate-limited + its retry, rep1
        first = next(a for name, a in attempts.items() if not name.endswith("--retry1") and a["status"] == "rate_limited")
        self.assertIn("rate_limit", first["status_detail"])
        evaluation = json.loads((self.out / "trusted" / first["attempt_id"] / "evaluation.json").read_text())
        self.assertIsNone(evaluation["official"])  # never a financial miss of the model
        self.assertEqual(self.waits, [45.0])  # cooldown = max(cooldown_s, provider Retry-After)
        self.assertEqual(summary["arms"]["td"]["statuses"], {"rate_limited": 1, "completed": 2})
        manifest = json.loads((self.out / "run_manifest.json").read_text())
        self.assertEqual(manifest["invocations"][-1]["stop_reason"], "plan_finished")

    def test_finite_slot_retries_then_the_slot_stays_unmeasured(self):
        plan, sha = self.plan(repetitions=1, max_slot_retries=1, max_consecutive_service_failures=10)
        limited = ProviderError("rate_limit", "HTTP 429", status=429)
        summary = self.run_plan(plan, sha, [limited, limited])
        self.assertEqual(sorted(a["status"] for a in self.attempts().values()), ["rate_limited", "rate_limited"])
        self.assertEqual(summary["arms"]["td"]["evaluable"], 0)
        self.assertEqual(self.waits, [30.0])

    def test_daily_quota_stops_the_run_with_a_reason(self):
        plan, sha = self.plan(repetitions=3)
        summary = self.run_plan(plan, sha, [ProviderError("quota_exhausted", "HTTP 429 (quota scope: day)", status=429)])
        self.assertEqual(len(self.attempts()), 1)  # no further attempt burns quota
        invocation = json.loads((self.out / "run_manifest.json").read_text())["invocations"][-1]
        self.assertEqual(invocation["stop_reason"], "provider_quota_exhausted")
        self.assertEqual(summary["last_invocation"]["stop_reason"], "provider_quota_exhausted")
        # Resume after the quota resets: the refused slot is retried, finished ones are not, nothing is lost.
        self.run_plan(plan, sha, [self.submit(), self.submit(), self.submit()])
        statuses = sorted(a["status"] for a in self.attempts().values())
        self.assertEqual(statuses, ["completed", "completed", "completed", "quota_exhausted"])
        self.assertEqual(len(json.loads((self.out / "run_manifest.json").read_text())["invocations"]), 2)

    def test_consecutive_service_failures_stop_the_run(self):
        plan, sha = self.plan(repetitions=3, max_consecutive_service_failures=2, max_slot_retries=5)
        down = ProviderError("server", "HTTP 503", status=503)
        self.run_plan(plan, sha, [down, down, down])
        self.assertEqual(len(self.attempts()), 2)
        invocation = json.loads((self.out / "run_manifest.json").read_text())["invocations"][-1]
        self.assertEqual(invocation["stop_reason"], "consecutive_service_failures")

    def test_completed_attempts_are_never_paid_again(self):
        plan, sha = self.plan(repetitions=2)
        self.run_plan(plan, sha, [self.submit(), self.submit()])
        before = self.attempts()
        self.run_plan(plan, sha, [])  # an empty script would fail any new model call
        self.assertEqual(self.attempts(), before)
        invocations = json.loads((self.out / "run_manifest.json").read_text())["invocations"]
        self.assertEqual((len(invocations), invocations[-1]["attempts_started"]), (2, []))

    def test_bad_service_settings_are_refused(self):
        plan, sha = self.plan(max_slot_retries=-1)
        with self.assertRaises(experiment.PlanError):
            self.run_plan(plan, sha, [])


if __name__ == "__main__":
    unittest.main()
