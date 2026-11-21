"""OpenAICompatibleProvider against the local fake server (TEST DOUBLE), real stdlib HTTP code."""
from __future__ import annotations

import json
import os
import socket
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_openai_server import FakeOpenAIServer  # noqa: E402

from auditlab.research.agents.openai_chat import OpenAICompatibleProvider  # noqa: E402
from auditlab.research.agents.providers import ProviderError, ProviderRequest, ToolSpec, make_provider  # noqa: E402

KEY_ENV = "AUDITLAB_TEST_CHAT_KEY"


def completion(message: dict, finish: str = "tool_calls", usage: dict | None = None) -> dict:
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": "fake-model-2026",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20,
                               "prompt_tokens_details": {"cached_tokens": 40}}}


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.key = "sk-SENTINEL-" + uuid.uuid4().hex
        os.environ[KEY_ENV] = self.key
        self.server = FakeOpenAIServer(self.key).start()
        self.provider = make_provider({"provider": "openai_compatible", "model": "fake-model", "api_key_env": KEY_ENV,
                                       "base_url": self.server.base_url, "prices": {"input": 1, "output": 2}})
        self.tools = [ToolSpec("query", "Read rows.", {"type": "object", "properties": {"table": {"type": "string"}}})]

    def tearDown(self):
        self.server.stop()
        os.environ.pop(KEY_ENV, None)

    def request(self, transcript, timeout=5.0):
        return ProviderRequest(system="You audit.", transcript=transcript, tools=self.tools, max_tokens=256,
                               timeout_s=timeout)

    def test_round_trip_with_tool_call(self):
        assistant = {"role": "assistant", "content": None, "refusal": None, "annotations": [],
                     "tool_calls": [{"id": "call_1", "type": "function",
                                     "function": {"name": "query", "arguments": "{\"table\": \"ledger\"}"}}]}
        self.server.chat_script.append({"body": completion(assistant)})
        response = self.provider.complete(self.request([{"role": "user", "text": "obs"}]))
        self.assertIsInstance(self.provider, OpenAICompatibleProvider)
        self.assertEqual([(c.id, c.name, c.input) for c in response.tool_calls], [("call_1", "query", {"table": "ledger"})])
        self.assertEqual(response.usage, {"input_tokens": 60, "output_tokens": 20, "cache_read_input_tokens": 40,
                                          "cache_creation_input_tokens": 0})
        self.assertEqual((response.stop_reason, response.model_served, response.request_id),
                         ("tool_calls", "fake-model-2026", "req_fake"))
        self.assertEqual(response.native, assistant)
        sent = json.loads(self.server.requests[-1]["body"])
        self.assertEqual(sent["max_completion_tokens"], 256)
        self.assertNotIn("max_tokens", sent)
        self.assertIs(sent["parallel_tool_calls"], False)
        self.assertEqual(sent["tools"][0], {"type": "function", "function": {
            "name": "query", "description": "Read rows.", "parameters": self.tools[0].input_schema}})
        self.assertEqual(sent["messages"], [{"role": "system", "content": "You audit."}, {"role": "user", "content": "obs"}])

        # The next turn appends the native assistant message back and answers its call with a tool message.
        self.server.chat_script.append({"body": completion({"role": "assistant", "content": "done"}, "stop")})
        transcript = [{"role": "user", "text": "obs"},
                      {"role": "assistant", "native": response.native, "text": response.text,
                       "tool_calls": [{"id": "call_1", "name": "query", "input": {"table": "ledger"}}]},
                      {"role": "tool_results", "results": [{"id": "call_1", "content": "rows", "is_error": False}],
                       "text": "Note: 3 actions left."}]
        second = self.provider.complete(self.request(transcript))
        self.assertEqual((second.text, second.tool_calls), ("done", []))
        messages = json.loads(self.server.requests[-1]["body"])["messages"]
        self.assertEqual(messages[2], {"role": "assistant", "content": None, "tool_calls": assistant["tool_calls"]})
        self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "call_1", "content": "rows"})
        self.assertEqual(messages[4], {"role": "user", "content": "Note: 3 actions left."})

    def test_unparseable_arguments_are_kept_raw(self):
        self.server.chat_script.append({"body": completion({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "query", "arguments": "{not json"}}]})})
        response = self.provider.complete(self.request([{"role": "user", "text": "obs"}]))
        self.assertEqual(response.tool_calls[0].input, "{not json")

    def assert_error(self, item, kind, timeout=5.0):
        self.server.chat_script.append(item)
        with self.assertRaises(ProviderError) as caught:
            self.provider.complete(self.request([{"role": "user", "text": "obs"}], timeout))
        self.assertEqual(caught.exception.kind, kind)
        self.assertNotIn(self.key, str(caught.exception))
        self.assertNotIn(self.key, json.dumps(caught.exception.to_dict()))
        return caught.exception

    def test_error_mapping(self):
        error = self.assert_error({"status": 429, "body": {"error": {"message": "slow down"}},
                                   "headers": {"Retry-After": "7"}}, "rate_limit")
        self.assertEqual((error.retry_after, error.status, error.retryable), (7.0, 429, True))
        self.assertEqual(self.assert_error({"status": 500, "body": {}}, "server").status, 500)
        self.assert_error({"status": 503, "body": b"<html>"}, "server")
        self.assert_error({"status": 403, "body": {"error": {"message": f"key {self.key} lacks access"}}}, "permission")
        self.assert_error({"status": 404, "body": {"error": {"message": "model not found"}}}, "not_found")
        self.assert_error({"status": 408, "body": {}}, "timeout")
        bad = self.assert_error({"status": 400, "body": {"error": {"message": f"bad param near {self.key}"}}}, "bad_request")
        self.assertIn("[redacted]", str(bad))
        self.assert_error({"status": 200, "body": b"{malformed"}, "invalid_response")
        self.assert_error({"status": 200, "body": {"choices": []}}, "invalid_response")

    def test_auth_error_never_echoes_key(self):
        os.environ[KEY_ENV] = self.key + "-wrong"  # the fake echoes the received Authorization header in its 401 body
        try:
            with self.assertRaises(ProviderError) as caught:
                self.provider.complete(self.request([{"role": "user", "text": "obs"}]))
        finally:
            os.environ[KEY_ENV] = self.key
        self.assertEqual(caught.exception.kind, "auth")
        self.assertNotIn(self.key, str(caught.exception))
        self.assertNotIn(self.key, repr(caught.exception.to_dict()))

    def test_timeout_via_slow_handler(self):
        self.assert_error({"delay": 2.5, "body": completion({"role": "assistant", "content": "late"}, "stop")},
                          "timeout", timeout=1.0)

    def test_connection_error(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        provider = OpenAICompatibleProvider("m", api_key_env=KEY_ENV, base_url=f"http://127.0.0.1:{port}/v1")
        with self.assertRaises(ProviderError) as caught:
            provider.complete(self.request([{"role": "user", "text": "obs"}], 15.0))
        self.assertEqual(caught.exception.kind, "connection")

    def test_missing_key_and_describe(self):
        os.environ.pop(KEY_ENV)
        with self.assertRaises(ProviderError) as caught:
            self.provider.complete(self.request([{"role": "user", "text": "obs"}]))
        self.assertEqual(caught.exception.kind, "configuration")
        self.assertEqual(self.server.requests, [])
        described = json.dumps(self.provider.describe())
        self.assertNotIn(self.key, described)
        self.assertEqual(self.provider.describe()["prices"], {"input": 1, "output": 2})
        self.assertFalse(self.provider.is_test_double)
        self.assertEqual(self.provider.name, "openai_compatible")


if __name__ == "__main__":
    unittest.main()
