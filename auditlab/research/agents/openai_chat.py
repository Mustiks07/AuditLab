"""OpenAI-compatible Chat Completions backend (stdlib only: urllib + json).

Used for the trainable-model series: the same agent loop against a base or fine-tuned model served
behind an OpenAI-compatible API. Its results are a separate series from the Claude series and are
never pooled with it into one causal claim.

Wire format verified 2026-11-21 against the Chat Completions API reference
(https://platform.openai.com/docs/api-reference/chat, now served from
https://developers.openai.com/api/reference/resources/chat):
  * POST {base_url}/chat/completions, bearer auth;
  * `max_completion_tokens` (the older `max_tokens` is documented as deprecated in its favour);
  * `parallel_tool_calls` (boolean, default true; we send false so a turn carries at most one action);
  * tools as {"type": "function", "function": {name, description, parameters, strict}};
  * roles system/developer, user, assistant (tool_calls [{id, type "function", function {name,
    arguments: JSON string}}]) and tool (tool_call_id, content);
  * usage prompt_tokens, completion_tokens, prompt_tokens_details.cached_tokens,
    completion_tokens_details.reasoning_tokens; finish_reason stop/length/tool_calls/content_filter.
Not verified from the reference excerpt: the `x-request-id` response header (read if present).

Secrets: the key is read from os.environ[api_key_env] at call time only. It is never stored on the
object, never put in describe(), and redacted from any provider error text before it is raised.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from auditlab.research.agents.providers import (ProviderError, ProviderRequest, ProviderResponse, ToolCall,
                                                ToolSpec)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_ASSISTANT_INPUT_KEYS = ("role", "content", "refusal", "tool_calls", "name")


# ----------------------------------------------------------------------------------- HTTP helper
@dataclass
class HttpResult:
    status: int
    headers: dict
    body: object  # parsed JSON


def _redact(text: str, key: str | None) -> str:
    if key and key in text:
        text = text.replace(key, "[redacted]")
    return text


def _retry_after(headers) -> float | None:
    try:
        value = headers.get("retry-after") if headers is not None else None
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error_message(raw: bytes, key: str | None) -> str:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        message = data.get("error", {}).get("message") if isinstance(data, dict) else None
    except (ValueError, AttributeError):
        message = None
    return _redact(str(message or "")[:300], key)


def http_request(method: str, url: str, *, key: str | None, body: bytes | None = None,
                 content_type: str = "application/json", timeout_s: float = 60.0,
                 extra_headers: dict | None = None) -> HttpResult:
    """One HTTP call returning parsed JSON; every failure becomes a ProviderError without the key."""
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = content_type
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(extra_headers or {})
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_s))) as response:
            raw = response.read()
            status, response_headers = response.status, response.headers
    except urllib.error.HTTPError as error:
        status = error.code
        request_id = error.headers.get("x-request-id") if error.headers is not None else None
        try:
            raw_error = error.read()
        except Exception:  # noqa: BLE001 - the body is optional diagnostics only
            raw_error = b""
        # 401/403 bodies may echo a masked key fragment, so their text is never kept.
        detail = "" if status in (401, 403) else _error_message(raw_error, key)
        message = f"HTTP {status}" + (f": {detail}" if detail else "")
        if status == 429:
            kind = "rate_limit"
        elif status == 408:
            kind = "timeout"
        elif status >= 500:
            kind = "server"
        else:
            kind = {401: "auth", 403: "permission", 404: "not_found"}.get(status, "bad_request")
        raise ProviderError(kind, message, status=status, retry_after=_retry_after(error.headers),
                            request_id=request_id) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise ProviderError("timeout", "request timed out") from None
        raise ProviderError("connection", _redact(f"connection error: {error.reason}", key)[:300]) from None
    except (TimeoutError, socket.timeout):
        raise ProviderError("timeout", "request timed out") from None
    except (ConnectionError, OSError, http.client.HTTPException) as error:
        raise ProviderError("connection", f"connection error: {type(error).__name__}") from None
    request_id = response_headers.get("x-request-id")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProviderError("invalid_response", "response body is not JSON", status=status,
                            request_id=request_id) from None
    return HttpResult(status, {k.lower(): v for k, v in response_headers.items()}, parsed)


# ----------------------------------------------------------------------------------- conversion
def tool_definitions(tools: list, strict: bool = False) -> list[dict]:
    """ToolSpec (or its dict form) -> Chat Completions function tools."""
    result = []
    for tool in tools:
        spec = tool if isinstance(tool, ToolSpec) else ToolSpec(tool["name"], tool.get("description", ""),
                                                                tool["input_schema"])
        function = {"name": spec.name, "description": spec.description, "parameters": spec.input_schema}
        if strict:
            function["strict"] = True
        result.append({"type": "function", "function": function})
    return result


def tool_call_arguments(value: object) -> str:
    """The wire `arguments` string: raw strings (unparseable model output) are kept as they were."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def rebuild_assistant(text: str, tool_calls: list[dict]) -> dict:
    """Assistant chat message from the neutral fields only (visible text + tool calls, no reasoning)."""
    message: dict = {"role": "assistant"}
    if text:
        message["content"] = text
    calls = [{"id": call["id"], "type": "function",
              "function": {"name": call["name"], "arguments": tool_call_arguments(call.get("input"))}}
             for call in tool_calls or []]
    if calls:
        message["tool_calls"] = calls
    if "content" not in message:
        message["content"] = None
    return message


def tool_messages(entry: dict) -> list[dict]:
    """A neutral tool_results entry -> tool messages (plus a user message for its optional text)."""
    messages = []
    for item in entry["results"]:
        content = str(item["content"])
        if item.get("is_error"):  # Chat Completions has no is_error flag; keep the signal in the text
            content = "[error] " + content
        messages.append({"role": "tool", "tool_call_id": item["id"], "content": content})
    if entry.get("text"):
        messages.append({"role": "user", "content": entry["text"]})
    return messages


def chat_messages(system: str, transcript: list[dict], *, system_role: str = "system",
                  use_native: bool = True) -> list[dict]:
    messages = [{"role": system_role, "content": system}] if system else []
    for entry in transcript:
        role = entry["role"]
        if role == "user":
            messages.append({"role": "user", "content": entry["text"]})
        elif role == "assistant":
            native = entry.get("native")
            if use_native and isinstance(native, dict) and native.get("role") == "assistant":
                messages.append({k: native[k] for k in _ASSISTANT_INPUT_KEYS
                                 if k in native and (native[k] is not None or k == "content")})
            else:
                messages.append(rebuild_assistant(entry.get("text") or "", entry.get("tool_calls") or []))
        elif role == "tool_results":
            messages.extend(tool_messages(entry))
        else:
            raise ValueError(f"unknown transcript role {role!r}")
    return messages


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_chat_response(data: object, requested: str, request_id: str | None, latency: float) -> ProviderResponse:
    if not isinstance(data, dict):
        raise ProviderError("invalid_response", "response is not a JSON object", request_id=request_id)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError("invalid_response", "response has no choices", request_id=request_id)
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderError("invalid_response", "choice has no message", request_id=request_id)
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    calls = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(function, dict) or not isinstance(raw.get("id"), str):
            raise ProviderError("invalid_response", "malformed tool call", request_id=request_id)
        arguments = function.get("arguments")
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            parsed = arguments  # the agent validates and reports it as an invalid action
        calls.append(ToolCall(raw["id"], str(function.get("name")), parsed))
    usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt, cached = _int(usage_raw.get("prompt_tokens")), _int((usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens"))
    usage = {"input_tokens": max(0, prompt - cached), "output_tokens": _int(usage_raw.get("completion_tokens")),
             "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
    extra = {"completion_id": data.get("id"), "refusal": message.get("refusal"),
             "reasoning_tokens": _int((usage_raw.get("completion_tokens_details") or {}).get("reasoning_tokens"))}
    return ProviderResponse(text=text, tool_calls=calls, stop_reason=str(choices[0].get("finish_reason")),
                            native=message, usage=usage, model_requested=requested,
                            model_served=str(data.get("model") or requested), request_id=request_id,
                            latency_s=latency, extra=extra)


# ----------------------------------------------------------------------------------- provider
class OpenAICompatibleProvider:
    """A model behind an OpenAI-compatible /chat/completions endpoint, one action per turn.

    `native` is the assistant message as returned by the API; it is sent back on the next request
    restricted to the documented assistant input fields (role, content, refusal, tool_calls, name).
    No SDK and no automatic retries: every retry is made and charged by the agent's retry policy.
    """

    name = "openai_compatible"
    is_test_double = False

    def __init__(self, model: str, *, api_key_env: str | None = "OPENAI_API_KEY", base_url: str = DEFAULT_BASE_URL,
                 system_role: str = "system", strict_tools: bool = False, prices: dict | None = None,
                 reasoning_effort: str | None = None):
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.system_role = system_role
        self.strict_tools = strict_tools
        self.prices = dict(prices) if prices else None
        self.reasoning_effort = reasoning_effort

    @classmethod
    def from_config(cls, config: dict) -> "OpenAICompatibleProvider":
        if not config.get("model"):
            raise ProviderError("configuration", "openai_compatible provider needs 'model'")
        return cls(config["model"], api_key_env=config.get("api_key_env", "OPENAI_API_KEY"),
                   base_url=config.get("base_url") or DEFAULT_BASE_URL,
                   system_role=config.get("system_role", "system"), strict_tools=config.get("strict_tools", False),
                   prices=config.get("prices"), reasoning_effort=config.get("reasoning_effort"))

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "base_url": self.base_url,
                "api_key_env": self.api_key_env, "system_role": self.system_role,
                "strict_tools": self.strict_tools, "parallel_tool_calls": False,
                "reasoning_effort": self.reasoning_effort, "prices": self.prices,
                "max_tokens_param": "max_completion_tokens", "client_retries": 0}

    def build_body(self, request: ProviderRequest) -> dict:
        body = {"model": self.model,
                "messages": chat_messages(request.system, request.transcript, system_role=self.system_role),
                "max_completion_tokens": int(request.max_tokens)}
        if request.tools:
            body["tools"] = tool_definitions(request.tools, self.strict_tools)
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = False
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        # api_key_env=None: an unauthenticated local server (e.g. vLLM serving a LoRA adapter on the GPU host).
        key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if self.api_key_env and not key:
            raise ProviderError("configuration", f"secret environment variable {self.api_key_env} is not set")
        body = json.dumps(self.build_body(request), ensure_ascii=False).encode("utf-8")
        started = time.monotonic()
        result = http_request("POST", f"{self.base_url}/chat/completions", key=key, body=body,
                              timeout_s=request.timeout_s)
        return parse_chat_response(result.body, self.model, result.headers.get("x-request-id"),
                                   time.monotonic() - started)
