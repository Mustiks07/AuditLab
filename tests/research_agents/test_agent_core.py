"""LLM auditor core: provider backend, reliability, memory, pre-submit check, attempts, experiments.

No test here calls a real provider. ScriptedProvider is an explicitly labelled TEST DOUBLE; the
Anthropic backend is exercised through an injected fake client that records the exact SDK call.
Scripts that need a correct finding read fixture truth in TEST code only; the agent never sees it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import judge_adapter  # noqa: E402
from auditlab.research.agents import experiment  # noqa: E402
from auditlab.research.agents.__main__ import main as cli_main, preflight, smoke  # noqa: E402
from auditlab.research.agents.agent import AgentFailure, LLMAuditor, precheck_submission, prompt_hash  # noqa: E402
from auditlab.research.agents.execute import InProcessBridge, PolicyBridge, finalize_interrupted, run_attempt  # noqa: E402
from auditlab.research.agents.memory import ReferenceIndex  # noqa: E402
from auditlab.research.agents.pricing import cost_usd  # noqa: E402
from auditlab.research.agents.providers import (AnthropicProvider, ProviderError, ProviderRequest, ScriptedProvider,  # noqa: E402
                                                ToolSpec, _map_anthropic_error, scripted_turn)
from auditlab.research.agents.reliability import ModelCaller, RetryPolicy, SpendGuard  # noqa: E402

FIXTURES = ROOT / "fixtures" / "foundation"
LIST = lambda table: scripted_turn("list_records", {"table": table, "cursor": None, "limit": 50})  # noqa: E731


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def oracle_finding(episode: Path) -> dict:
    """TEST-ONLY knowledge used to script a correct answer for the test double."""
    issue = json.loads((episode / "private" / "truth.json").read_text())["issues"][0]
    return {"type": issue["type"], "affected_ids": issue["affected_ids"], "claimed_amount_minor": issue["expected_amount_minor"],
            "currency": issue["currency"], "evidence_ids": issue["evidence_sets"][0], "explanation": "test double"}


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.episodes = {}
        for name in ("S01", "S02"):
            self.episodes[name] = self.tmp / "episodes" / name
            shutil.copytree(FIXTURES / name, self.episodes[name])
        self.out = self.tmp / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def spec(self, attempt_id, episode="S02", variant="memory", protocol="auditlab-research-600s", budget=None, **extra):
        return {"attempt_id": attempt_id, "episode_ref": str(self.episodes[episode]),
                "episode": {"family_id": episode, "split": "dev"}, "variant": variant, "agent": {"variant": variant},
                "provider": {}, "protocol_id": protocol, "budget_usd": budget, **extra}

    def evaluation(self, attempt_id):
        return json.loads((self.out / "trusted" / attempt_id / "evaluation.json").read_text())

    def attempt_files(self, attempt_id):
        return self.out / "attempts" / attempt_id


# ------------------------------------------------------------------------------------- provider
class FakeMessage:
    def __init__(self, data):
        self._data, self._request_id = data, "req_test"

    def to_dict(self):
        return self._data


class FakeClient:
    """Records the exact keyword arguments of messages.create / beta.messages.create."""

    def __init__(self, data):
        self.calls, self.options, self._data = [], [], data
        self.messages = types.SimpleNamespace(create=self._create("messages"))
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create("beta")))

    def _create(self, api):
        def create(**kwargs):
            self.calls.append((api, kwargs))
            return FakeMessage(self._data)
        return create

    def with_options(self, **options):
        self.options.append(options)
        return self


class AnthropicBackend(unittest.TestCase):
    DATA = {"model": "claude-opus-5-5", "stop_reason": "tool_use",
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                        {"type": "text", "text": "Reading payments."},
                        {"type": "tool_use", "id": "toolu_1", "name": "list_records",
                         "input": {"table": "payments", "cursor": None, "limit": 50}}],
            "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 3000,
                      "cache_creation_input_tokens": 0}}

    def request(self):
        transcript = [{"role": "user", "text": "start"},
                      {"role": "assistant", "native": [{"type": "tool_use", "id": "t0", "name": "calculator",
                                                        "input": {"expression": "1+1"}}], "text": "", "tool_calls": []},
                      {"role": "tool_results", "results": [{"id": "t0", "content": "{\"result\":\"2\"}", "is_error": False}],
                       "text": "notes"}]
        return ProviderRequest("SYSTEM", transcript, [ToolSpec("calculator", "calc", {"type": "object"})], 16000, 30.0)

    def test_request_shape_and_response_parsing(self):
        client = FakeClient(self.DATA)
        provider = AnthropicProvider("claude-opus-5-5", effort="high", client=client)
        response = provider.complete(self.request())
        api, kwargs = client.calls[0]
        self.assertEqual(api, "messages")
        self.assertEqual(kwargs["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True})
        self.assertEqual(kwargs["output_config"], {"effort": "high"})
        self.assertEqual(kwargs["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("thinking", kwargs)  # adaptive thinking is the model default; never disabled
        self.assertTrue(kwargs["tools"][0]["strict"])
        self.assertEqual(kwargs["messages"][1]["content"][0]["id"], "t0")  # native assistant content unchanged
        self.assertEqual(kwargs["messages"][2]["content"][0]["type"], "tool_result")
        self.assertEqual(kwargs["messages"][2]["content"][1], {"type": "text", "text": "notes"})
        self.assertEqual(client.options[-1]["max_retries"], 0)
        self.assertEqual([c.name for c in response.tool_calls], ["list_records"])
        self.assertEqual(response.native, self.DATA["content"])  # thinking block kept for the next request
        self.assertEqual(response.usage["cache_read_input_tokens"], 3000)
        self.assertFalse(response.served_by_fallback)
        self.assertEqual(response.request_id, "req_test")

    def test_fallback_is_beta_and_flagged(self):
        data = dict(self.DATA, model="claude-opus-5", usage={**self.DATA["usage"], "iterations": [
            {"type": "message"}, {"type": "fallback_message"}]})
        client = FakeClient(data)
        response = AnthropicProvider(fallbacks="default", client=client).complete(self.request())
        api, kwargs = client.calls[0]
        self.assertEqual((api, kwargs["betas"], kwargs["fallbacks"]), ("beta", ["server-side-fallback-2026-07-01"], "default"))
        self.assertTrue(response.served_by_fallback)
        self.assertEqual(response.model_served, "claude-opus-5")

    def test_missing_sdk_or_key_is_a_configuration_error(self):
        provider = AnthropicProvider(api_key_env="AUDITLAB_TEST_NO_SUCH_KEY")
        with self.assertRaises(ProviderError) as caught:
            provider.complete(self.request())
        self.assertEqual(caught.exception.kind, "configuration")

    def test_error_mapping_most_specific_first(self):
        class APIError(Exception):
            pass

        class APIConnectionError(APIError):
            pass

        class APITimeoutError(APIConnectionError):
            pass

        class APIStatusError(APIError):
            def __init__(self, status, headers=None):
                self.status_code = status
                self.response = types.SimpleNamespace(headers=headers or {})

        names = ("RateLimitError", "AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError")
        sdk = types.SimpleNamespace(APIConnectionError=APIConnectionError, APITimeoutError=APITimeoutError,
                                    APIStatusError=APIStatusError,
                                    **{name: type(name, (APIStatusError,), {}) for name in names})
        cases = [(APITimeoutError(), "timeout"), (APIConnectionError(), "connection"),
                 (sdk.RateLimitError(429, {"retry-after": "7"}), "rate_limit"), (sdk.AuthenticationError(401), "auth"),
                 (sdk.PermissionDeniedError(403), "permission"), (sdk.NotFoundError(404), "not_found"),
                 (sdk.BadRequestError(400), "bad_request"), (APIStatusError(529), "server")]
        for error, kind in cases:
            with self.subTest(kind=kind):
                self.assertEqual(_map_anthropic_error(error, sdk).kind, kind)
        self.assertEqual(_map_anthropic_error(sdk.RateLimitError(429, {"retry-after": "7"}), sdk).retry_after, 7.0)

    def test_cost_from_reported_usage(self):
        cost, reason = cost_usd("claude-opus-5-5", self.DATA["usage"])
        self.assertIsNone(reason)
        self.assertAlmostEqual(cost, (1200 * 4 + 80 * 20 + 3000 * 0.2) / 1e6, places=12)
        self.assertEqual(cost_usd("unknown-model", self.DATA["usage"]), (None, "no price for model 'unknown-model'"))


# ------------------------------------------------------------------------------------- reliability
class Reliability(unittest.TestCase):
    def call(self, script, *, budget=None, model="test-double", deadline=100.0, tries=4, allow_unpriced=True):
        clock = FakeClock(0.0)
        provider = ScriptedProvider(script, model=model)
        caller = ModelCaller(provider, RetryPolicy(max_tries=tries, base_delay_s=1.0), SpendGuard(budget, allow_unpriced=allow_unpriced),
                             clock=clock, sleep=clock.sleep)
        request = ProviderRequest("s", [{"role": "user", "text": "x"}], [], 1000, 0.0)
        return caller, clock, lambda: caller.call(request, deadline)

    def test_rate_limit_waits_for_retry_after_and_every_try_is_recorded(self):
        caller, clock, run = self.call([ProviderError("rate_limit", "slow", retry_after=3.0),
                                        ProviderError("server", "boom"), scripted_turn("calculator", {"expression": "1"})])
        response = run()
        self.assertEqual(response.tool_calls[0].name, "calculator")
        self.assertEqual([r["status"] for r in caller.records], ["error", "error", "ok"])
        self.assertEqual(clock.slept[0], 3.0)
        self.assertEqual(len(clock.slept), 2)

    def test_non_retryable_and_exhausted(self):
        caller, _, run = self.call([ProviderError("auth", "no")])
        with self.assertRaises(ProviderError) as caught:
            run()
        self.assertEqual((caught.exception.kind, len(caller.records)), ("auth", 1))
        caller, _, run = self.call([ProviderError("server", "x")] * 3, tries=3)
        with self.assertRaises(ProviderError):
            run()
        self.assertEqual(len(caller.records), 3)

    def test_retry_never_passes_the_deadline(self):
        caller, _, run = self.call([ProviderError("rate_limit", "slow", retry_after=50.0)], deadline=20.0)
        with self.assertRaises(ProviderError) as caught:
            run()
        self.assertEqual(caught.exception.kind, "deadline")
        caller, _, run = self.call([], deadline=0.5)
        with self.assertRaises(ProviderError) as caught:
            run()
        self.assertEqual(caught.exception.kind, "deadline")

    def test_spend_guard(self):
        caller, _, run = self.call([scripted_turn("calculator", {})], model="claude-opus-5-5", budget=0.0001,
                                   allow_unpriced=False)
        with self.assertRaises(ProviderError) as caught:
            run()
        self.assertEqual((caught.exception.kind, len(caller.records)), ("budget", 0))  # refused before any call
        caller, _, run = self.call([scripted_turn("calculator", {})], model="mystery-model", budget=10.0,
                                   allow_unpriced=False)
        with self.assertRaises(ProviderError):
            run()


# ------------------------------------------------------------------------------------- agent
class AgentBehaviour(Workspace):
    def test_memory_tools_cost_no_environment_action_and_precheck_never_rewrites(self):
        finding = {**oracle_finding(self.episodes["S02"]), "evidence_ids": ["payments/D9", "ledger/R404"]}
        script = [scripted_turn("note", {"kind": "question", "text": "is P9 applied?", "evidence_ids": [], "type": None,
                                         "affected_ids": None, "claimed_amount_minor": None, "currency": None}),
                  LIST("payments"),
                  scripted_turn("submit", {"episode_id": "S02", "findings": [finding]}),
                  scripted_turn("submit", {"episode_id": "S02", "findings": [finding]}),
                  scripted_turn("submit", {"episode_id": "S02", "findings": [finding]})]
        provider = ScriptedProvider(script)
        record = run_attempt(self.spec("mem"), self.out, lambda: InProcessBridge(provider))
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["totals"]["environment_actions"], 2)  # list_records + one real submit
        steps = [json.loads(line) for line in (self.attempt_files("mem") / "steps.jsonl").read_text().splitlines()]
        submitted = steps[-1]["action"]["args"]["findings"]["findings"][0]
        self.assertEqual(submitted["evidence_ids"], ["payments/D9", "ledger/R404"])  # the model's own answer, unchanged
        transcript = (self.attempt_files("mem") / "transcript.jsonl").read_text()
        self.assertEqual(transcript.count("evidence ids not read in this episode"), 2)  # two prechecks, then submitted
        self.assertEqual(self.evaluation("mem")["official"]["tp"], 0)  # unread R404 is unavailable -> FP

    def test_no_tool_call_is_nudged_then_fails(self):
        provider = ScriptedProvider([scripted_turn(None, text="thinking aloud")] * 3)
        record = run_attempt(self.spec("nudge", variant="tools"), self.out, lambda: InProcessBridge(provider))
        self.assertEqual(record["status"], "invalid_response")
        self.assertIsNone(self.evaluation("nudge")["official"])
        self.assertIn("ended with invalid_response", self.evaluation("nudge")["not_evaluable_reason"])

    def test_refusal_and_provider_failure_are_not_financial_results(self):
        refusal = ScriptedProvider([scripted_turn(None, text="", stop_reason="refusal")])
        self.assertEqual(run_attempt(self.spec("refusal"), self.out, lambda: InProcessBridge(refusal))["status"],
                         "provider_refusal")
        broken = ScriptedProvider([ProviderError("server", "down")] * 4)
        record = run_attempt(self.spec("down"), self.out, lambda: InProcessBridge(broken))
        self.assertEqual(record["status"], "provider_error")
        self.assertEqual(record["totals"]["provider_calls"], 4)  # every failed try stays in the record
        self.assertIsNone(self.evaluation("down")["official"])

    def test_invalid_action_goes_to_the_environment(self):
        provider = ScriptedProvider([scripted_turn("delete_everything", {"path": "/"}),
                                     scripted_turn("submit", {"episode_id": "S01", "findings": []})])
        record = run_attempt(self.spec("bad", episode="S01", variant="tools"), self.out, lambda: InProcessBridge(provider))
        steps = [json.loads(line) for line in (self.attempt_files("bad") / "steps.jsonl").read_text().splitlines()]
        self.assertEqual(steps[0]["observation"]["error"]["code"], "unknown_action")
        self.assertEqual(record["totals"]["inspection_steps"], 1)  # the bad action consumed a step

    def test_budget_stop(self):
        provider = ScriptedProvider([LIST("payments")], model="claude-opus-5-5")
        provider.is_test_double = False  # priced like the real model so the guard is exercised
        record = run_attempt(self.spec("budget", budget=0.000001), self.out, lambda: InProcessBridge(provider))
        self.assertEqual(record["status"], "budget_stop")
        self.assertEqual(record["totals"]["provider_calls"], 0)

    def test_timeout_includes_model_latency(self):
        clock = FakeClock(0.0)

        def slow(request):
            clock.now += 61.0  # one model call takes longer than the whole official limit
            return LIST("ledger")
        provider = ScriptedProvider([slow, LIST("ledger")])
        env_factory = lambda: judge_adapter.make_env(evaluator="inprocess", clock=clock, time_limit_s=60.0)  # noqa: E731
        record = run_attempt(self.spec("slow", protocol="auditlab-research-600s"), self.out,
                             lambda: InProcessBridge(provider), env_factory=env_factory)
        self.assertEqual(record["status"], "timeout")
        self.assertEqual(self.evaluation("slow")["env_status"], "truncated_timeout")
        self.assertEqual(self.evaluation("slow")["official"]["tp"], 0)  # published rule: empty findings

    def test_official_protocol_refuses_an_unisolated_bridge(self):
        with self.assertRaises(ValueError):
            run_attempt(self.spec("official", protocol="genesis-official-60s"), self.out,
                        lambda: InProcessBridge(ScriptedProvider([])))

    def test_model_never_receives_private_data(self):
        finding = oracle_finding(self.episodes["S02"])
        script = [LIST(t) for t in ("payments", "ledger", "policy", "invoices", "fx")] + [
            scripted_turn("get_document", {"doc_id": "../private/truth.json"}),
            scripted_turn("submit", {"episode_id": "S02", "findings": [finding]})]
        provider = ScriptedProvider(script)
        run_attempt(self.spec("leak", seed=424242), self.out, lambda: InProcessBridge(provider))
        # Everything the ENVIRONMENT side put in front of the model: system prompt, user turns, tool results.
        # (The test double's own tool calls are excluded: it deliberately asks for ../private/truth.json.)
        shown = [provider.requests[-1].system]
        for entry in provider.requests[-1].transcript:
            if entry["role"] == "user":
                shown.append(entry["text"])
            elif entry["role"] == "tool_results":
                shown += [item["content"] for item in entry["results"]] + [entry.get("text") or ""]
        sent = json.dumps(shown, ensure_ascii=False)
        truth = json.loads((self.episodes["S02"] / "private" / "truth.json").read_text())
        for secret in [str(self.episodes["S02"]), "private", "evidence_sets", "expected_amount_minor", "424242",
                       *(issue["issue_id"] for issue in truth["issues"])]:
            self.assertNotIn(secret, sent)
        public = json.dumps([json.loads(p.read_text()) for p in [self.attempt_files("leak") / "attempt.json"]])
        self.assertNotIn("issue_id", public)

    def test_bridge_imports_nothing_trusted(self):
        code = ("import sys; import auditlab.research.agents.bridge; "
                "print(sorted(m for m in sys.modules if m.startswith('auditlab')))")
        loaded = json.loads(subprocess.run([sys.executable, "-c", code.replace("print(", "import json; print(json.dumps(")
                                            + ")"], capture_output=True, text=True, cwd=ROOT, check=True).stdout)
        for forbidden in ("auditlab.evaluator", "auditlab.runner", "auditlab.generator", "auditlab.environment",
                          "auditlab.verifier", "auditlab.release"):
            self.assertFalse([m for m in loaded if m.startswith(forbidden)], forbidden)

    def test_precheck_reports_format_and_references_only(self):
        refs = ReferenceIndex()
        refs.evidence.update({"payments/D9"})
        good = {"type": "payment_allocation", "affected_ids": ["invoice/I4"], "claimed_amount_minor": 5,
                "currency": "KZT", "evidence_ids": ["payments/D9"], "explanation": "x"}
        self.assertEqual(precheck_submission({"episode_id": "S02", "findings": [good]}, "S02", refs), [])
        problems = precheck_submission({"episode_id": "S01", "findings": [dict(good, type="odd", claimed_amount_minor="5",
                                                                               evidence_ids=["payments/D9"] * 11)]}, "S02", refs)
        self.assertEqual(len(problems), 4)
        self.assertNotIn("amount should", " ".join(problems))  # never suggests a financial value

    def test_prompt_hash_depends_on_variant_and_provider(self):
        a = prompt_hash("tools", {"model": "m"}, 1000)
        self.assertNotEqual(a, prompt_hash("memory", {"model": "m"}, 1000))
        self.assertNotEqual(a, prompt_hash("tools", {"model": "n"}, 1000))
        self.assertEqual(a, prompt_hash("tools", {"model": "m"}, 1000))


# ------------------------------------------------------------------------------------- experiments
class Experiments(Workspace):
    def plan(self, **changes):
        plan = {"schema_version": "1.0", "artifact_id": "exp-test", "protocol_id": "auditlab-research-600s",
                "purpose": "selection",
                "episodes": [{"episode_ref": str(self.episodes[n]), "family_id": n, "split": "dev"} for n in ("S01", "S02")],
                "arms": [{"name": "empty", "agent": {"kind": "baseline_policy", "policy": "empty"}},
                         {"name": "reconcile", "agent": {"kind": "baseline_policy", "policy": "reconcile"}}],
                "repetitions": 2, "analysis": {"pairs": [["reconcile", "empty"]], "bootstrap": 200}}
        plan.update(changes)
        path = self.tmp / f"plan-{len(list(self.tmp.glob('plan-*')))}.json"
        path.write_text(json.dumps(plan))
        return experiment.load_plan(path)

    def test_run_resume_and_summary(self):
        plan, sha = self.plan()
        summary = experiment.run_experiment(plan, sha, self.out, log=lambda _: None)
        self.assertEqual(summary["attempts_total"], 8)
        self.assertEqual(summary["arms"]["reconcile"]["micro"], {"tp": 2, "fp": 0, "fn": 0, "f1": 1.0})
        self.assertEqual(summary["arms"]["empty"]["clean_attempts"], 2)
        self.assertEqual(summary["arms"]["empty"]["clean_with_fp"], 0)
        self.assertTrue(summary["is_experiment"])
        pair = summary["paired"][0]
        self.assertEqual(pair["families"], 2)
        before = sorted(p.name for p in (self.out / "attempts").iterdir())
        experiment.run_experiment(plan, sha, self.out, log=lambda _: None)  # resume: nothing re-run
        self.assertEqual(sorted(p.name for p in (self.out / "attempts").iterdir()), before)
        changed, changed_sha = self.plan(repetitions=3)
        with self.assertRaises(experiment.PlanError):
            experiment.run_experiment(changed, changed_sha, self.out, log=lambda _: None)

    def test_interrupted_attempt_is_kept_and_retried_with_a_new_id(self):
        plan, sha = self.plan(arms=[{"name": "empty", "agent": {"kind": "baseline_policy", "policy": "empty"}}], repetitions=1)
        slot = experiment.attempt_slots(plan)[0]["slot"]
        partial = self.out / "attempts" / f"{slot}.partial"
        partial.mkdir(parents=True)
        (partial / "attempt.json").write_text(json.dumps({"artifact_id": slot, "variant": "empty", "totals": {},
                                                          "episode": {"family_id": "S01"}}))
        (partial / "provider_calls.jsonl").write_text(json.dumps({"status": "ok", "cost_usd": 0.25}) + "\n")
        experiment.run_experiment(plan, sha, self.out, log=lambda _: None)
        interrupted = json.loads((self.out / "attempts" / slot / "attempt.json").read_text())
        self.assertEqual((interrupted["status"], interrupted["totals"]["cost_usd"]), ("interrupted", 0.25))
        retry = json.loads((self.out / "attempts" / f"{slot}--retry1" / "attempt.json").read_text())
        self.assertEqual(retry["status"], "completed")

    def test_plan_guards(self):
        with self.assertRaises(experiment.PlanError):  # selection may not touch the final holdout
            self.plan(episodes=[{"episode_ref": str(self.episodes["S01"]), "family_id": "S01", "split": "final"}])
        with self.assertRaises(experiment.PlanError):  # official limits need an isolated bridge
            self.plan(protocol_id="genesis-official-60s",
                      arms=[{"name": "claude", "agent": {"kind": "llm", "variant": "tools"},
                             "provider": {"provider": "anthropic", "model": "claude-opus-5-5"}}])
        paid, sha = self.plan(arms=[{"name": "claude", "agent": {"kind": "llm", "variant": "tools"},
                                     "provider": {"provider": "anthropic", "model": "claude-opus-5-5"}}])
        report = experiment.dry_run(paid, sha)
        self.assertEqual((report["paid_attempts"], report["network_calls"]), (4, 0))
        self.assertTrue(any("max_usd_total" in m for m in report["missing_conditions"]))
        with self.assertRaises(experiment.PlanError):
            experiment.run_experiment(paid, sha, self.out, log=lambda _: None)
        self.assertFalse((self.out / "attempts").exists())  # refused before any attempt

    def test_test_double_runs_are_never_an_experiment(self):
        finding = oracle_finding(self.episodes["S02"])
        plan, sha = self.plan(episodes=[{"episode_ref": str(self.episodes["S02"]), "family_id": "S02", "split": "dev"}],
                              arms=[{"name": "td", "agent": {"kind": "llm", "variant": "tools"},
                                     "provider": {"provider": "test_double"}}], repetitions=1)
        provider = ScriptedProvider([LIST("payments"), scripted_turn("submit", {"episode_id": "S02", "findings": [finding]})])
        summary = experiment.run_experiment(plan, sha, self.out, provider_override=provider, log=lambda _: None)
        self.assertEqual((summary["test_double"], summary["is_experiment"]), (True, False))


class Cli(unittest.TestCase):
    def test_preflight_and_smoke_without_access_make_no_calls(self):
        config = {"provider": "anthropic", "model": "claude-opus-5-5", "api_key_env": "AUDITLAB_TEST_NO_SUCH_KEY"}
        report = preflight(config)
        self.assertFalse(report["ready_for_paid_calls"])
        self.assertTrue(any("AUDITLAB_TEST_NO_SUCH_KEY" in m for m in report["missing_conditions"]))
        with tempfile.TemporaryDirectory() as tmp:
            result = smoke(config, 1.0, True, Path(tmp))
            self.assertEqual((result["status"], result["network_calls"]), ("not_checked", 0))
            self.assertFalse((Path(tmp) / "smoke.json").exists())

    def test_cli_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            arms = Path(tmp) / "arms.json"
            arms.write_text(json.dumps({"arms": [{"name": "empty", "agent": {"kind": "baseline_policy", "policy": "empty"}}]}))
            plan = Path(tmp) / "plan.json"
            self.assertEqual(cli_main(["plan", "--episodes", str(FIXTURES), "--split", "dev", "--arms", str(arms),
                                       "--protocol", "auditlab-research-600s", "--repetitions", "1", "--out", str(plan)]), 0)
            self.assertEqual(len(json.loads(plan.read_text())["episodes"]), 2)
            self.assertEqual(cli_main(["dry-run", "--plan", str(plan)]), 0)


if __name__ == "__main__":
    unittest.main()


class SubprocessTransport(Workspace):
    """The real bridge process (python -m auditlab.research.agents.bridge) behind sandbox.BridgeProcess."""

    def test_attempt_through_the_bridge_process(self):
        from auditlab.research.agents.transport import SubprocessBridge
        finding = oracle_finding(self.episodes["S02"])
        script = [LIST("payments").to_record(), LIST("ledger").to_record(), LIST("policy").to_record(),
                  scripted_turn("submit", {"episode_id": "S02", "findings": [finding]}).to_record()]
        provider = {"provider": "test_double", "script": script}
        record = run_attempt(dict(self.spec("proc", variant="tools"), provider=provider), self.out,
                             lambda: SubprocessBridge(provider))
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["test_double"])
        self.assertFalse(record["isolated_bridge"])
        self.assertEqual(self.evaluation("proc")["official"]["tp"], 1)
        self.assertEqual(record["totals"]["provider_calls"], 4)

    def test_bridge_failure_reply_is_typed(self):
        from auditlab.research.agents.transport import SubprocessBridge
        provider = {"provider": "test_double", "script": []}  # exhausted at once -> invalid_response
        record = run_attempt(dict(self.spec("empty-script", variant="tools"), provider=provider), self.out,
                             lambda: SubprocessBridge(provider))
        self.assertEqual(record["status"], "invalid_response")

    def test_subprocess_transport_refused_for_official_protocol(self):
        from auditlab.research.agents.transport import SubprocessBridge
        provider = {"provider": "test_double", "script": []}
        with self.assertRaises(ValueError):  # run_attempt refuses an unisolated bridge for API agents
            run_attempt(dict(self.spec("off", protocol="genesis-official-60s"), provider=provider), self.out,
                        lambda: SubprocessBridge(provider))


class ReviewExportAndDemonstrations(Workspace):
    def test_export_matches_the_independent_layout(self):
        plan = {"schema_version": "1.0", "artifact_id": "exp-export", "protocol_id": "auditlab-research-600s",
                "episodes": [{"episode_ref": str(self.episodes[n]), "family_id": n, "split": "dev"} for n in ("S01", "S02")],
                "arms": [{"name": "reconcile", "agent": {"kind": "baseline_policy", "policy": "reconcile"}},
                         {"name": "empty", "agent": {"kind": "baseline_policy", "policy": "empty"}}],
                "repetitions": 1, "analysis": {"pairs": [["reconcile", "empty"]]}}
        path = self.tmp / "plan.json"
        path.write_text(json.dumps(plan))
        plan, sha = experiment.load_plan(path)
        experiment.run_experiment(plan, sha, self.out, log=lambda _: None)
        result = experiment.export_for_review(self.out, self.tmp / "review", plan)
        self.assertEqual((result["attempts"], result["episodes"]), (4, 2))
        exported = json.loads((self.tmp / "review" / "experiment.json").read_text())
        self.assertEqual(exported["comparisons"], [{"name": "reconcile_vs_empty", "a": "reconcile", "b": "empty"}])
        self.assertEqual({e["variant"] for e in exported["episodes"].values()}, {"clean", "error"})
        rows = [json.loads(p.read_text()) for p in (self.tmp / "review" / "attempts").glob("*.json")]
        needed = {"attempt_id", "arm", "episode_key", "repetition", "status", "final_answer", "inspection_steps", "actions",
                  "input_tokens", "output_tokens", "wall_time_s", "cost_usd", "live"}
        for row in rows:
            self.assertLessEqual(needed, set(row))
            self.assertIn(row["episode_key"], exported["episodes"])
            self.assertFalse(row["live"])  # local policies are not live model calls
            self.assertEqual(row["final_answer"]["episode_id"], json.loads(
                (Path(exported["episodes"][row["episode_key"]]["episode_dir"]) / "public" / "manifest.json").read_text())["episode_id"])

    def test_demonstrations_only_on_training_splits_and_labelled(self):
        from auditlab.research.training.demonstrations import run_demonstration
        record = run_demonstration({"episode_ref": str(self.episodes["S02"]), "family_id": "S02", "split": "train"}, self.out)
        self.assertEqual((record["status"], record["agent"]["kind"], record["test_double"]), ("completed", "oracle_demonstration", False))
        self.assertEqual(self.evaluation(record["attempt_id"])["official"]["tp"], 1)
        self.assertEqual(record["totals"]["cost_usd"], 0.0)
        self.assertNotIn("\\", record["episode"]["episode_ref"])  # POSIX refs match curricula on every OS
        from auditlab.research.training.dataset import _ref_key
        self.assertEqual(_ref_key(r"datasets\open\train\E1"), "datasets/open/train/E1")
        for split in ("dev", "final", "holdout", "test"):
            with self.assertRaises(ValueError):
                run_demonstration({"episode_ref": str(self.episodes["S01"]), "family_id": "S01", "split": split}, self.out, f"x-{split}")
