"""Provider interface for the LLM auditor and the Anthropic Messages API backend.

The agent keeps a provider-neutral transcript; each backend converts it to its own wire format and
returns a ProviderResponse with the native assistant content (appended back unchanged on the next
request, so Claude's thinking blocks stay valid), the parsed tool calls, usage and served model.

Neutral transcript entries:
    {"role": "user", "text": str}
    {"role": "assistant", "native": object, "text": str, "tool_calls": [{"id", "name", "input"}]}
    {"role": "tool_results", "results": [{"id", "content": str, "is_error": bool}], "text": str | None}

Secrets: a backend reads its key from the environment variable named in its config, inside the
network bridge process only. The key never enters a request record, journal, artifact or log.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

ERROR_KINDS = ("rate_limit", "quota_exhausted", "timeout", "server", "connection", "auth", "permission",
               "bad_request", "not_found", "invalid_response", "refusal", "budget", "deadline", "configuration")
# rate_limit: a short-window limit (per minute); waiting helps. quota_exhausted: a long-window quota (per day,
# or the provider says so); waiting inside an attempt cannot help, so it is never retried and stops the run.
RETRYABLE = frozenset({"rate_limit", "timeout", "server", "connection"})


class ProviderError(RuntimeError):
    """A failed provider call. `kind` is one of ERROR_KINDS; the message never contains the key."""

    def __init__(self, kind: str, message: str, *, status: int | None = None, retry_after: float | None = None,
                 request_id: str | None = None):
        if kind not in ERROR_KINDS:
            raise ValueError(f"unknown provider error kind {kind!r}")
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": str(self), "status": self.status,
                "retry_after": self.retry_after, "request_id": self.request_id}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ProviderRequest:
    system: str
    transcript: list[dict]
    tools: list[ToolSpec]
    max_tokens: int
    timeout_s: float


@dataclass
class ToolCall:
    id: str
    name: str
    input: object  # whatever the model sent; validated by the agent, never trusted


@dataclass
class ProviderResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    native: object
    usage: dict
    model_requested: str
    model_served: str
    request_id: str | None = None
    served_by_fallback: bool = False
    latency_s: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        record = asdict(self)
        record["tool_calls"] = [asdict(call) for call in self.tool_calls]
        return record

    @classmethod
    def from_record(cls, record: dict) -> "ProviderResponse":
        data = dict(record)
        data["tool_calls"] = [ToolCall(**call) for call in data["tool_calls"]]
        return cls(**data)


class Provider(Protocol):
    name: str
    model: str
    is_test_double: bool

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...

    def describe(self) -> dict: ...


def estimate_input_tokens(request: ProviderRequest) -> int:
    """Conservative UTF-8 byte bound plus framing allowance for budget reservation."""
    size = len(request.system) + len(json.dumps([asdict(tool) for tool in request.tools]))
    size += len(json.dumps(request.transcript, default=str))
    return size * 4 + 4096


# ----------------------------------------------------------------------------------- Anthropic
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    """Claude through the official `anthropic` Python SDK (Messages API, client tools, manual loop).

    One request per agent turn, `tool_choice` auto with parallel tool use disabled so every response
    carries at most one action. The SDK's own retries are disabled (max_retries=0): every retry is made
    and charged by the bounded policy in reliability.py. Adaptive thinking is the model default; only
    `effort` is set. Server-side refusal fallback is opt-in per config; a response served by a fallback
    model is flagged, because it is no longer the configured model.
    """

    name = "anthropic"
    is_test_double = False

    def __init__(self, model: str = "claude-opus-5-5", *, effort: str = "high", api_key_env: str = "ANTHROPIC_API_KEY",
                 base_url: str | None = None, fallbacks: str | None = None, strict_tools: bool = True,
                 prompt_cache: bool = True, client: object = None):
        self.model = model
        self.effort = effort
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.fallbacks = fallbacks
        self.strict_tools = strict_tools
        self.prompt_cache = prompt_cache
        self._client = client

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "effort": self.effort, "fallbacks": self.fallbacks,
                "strict_tools": self.strict_tools, "prompt_cache": self.prompt_cache,
                "api_key_env": self.api_key_env, "base_url": self.base_url, "thinking": "adaptive (model default)",
                "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}, "sdk_max_retries": 0}

    def _sdk(self):
        try:
            import anthropic
        except ImportError as error:
            raise ProviderError("configuration", "the 'anthropic' SDK is not installed in the bridge "
                                "environment (see docs/research/llm-auditor/README.md)") from error
        return anthropic

    def _client_for(self, timeout_s: float):
        if self._client is None:
            anthropic = self._sdk()
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ProviderError("configuration", f"secret environment variable {self.api_key_env} is not set")
            options = {"api_key": key, "max_retries": 0}
            if self.base_url:
                options["base_url"] = self.base_url
            self._client = anthropic.Anthropic(**options)
        return self._client.with_options(timeout=max(1.0, timeout_s), max_retries=0)

    def _tools(self, tools: list[ToolSpec]) -> list[dict]:
        result = []
        for tool in tools:
            entry = {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            if self.strict_tools:
                entry["strict"] = True
            result.append(entry)
        return result

    @staticmethod
    def native_messages(transcript: list[dict]) -> list[dict]:
        messages = []
        for entry in transcript:
            if entry["role"] == "user":
                messages.append({"role": "user", "content": [{"type": "text", "text": entry["text"]}]})
            elif entry["role"] == "assistant":
                messages.append({"role": "assistant", "content": entry["native"]})
            elif entry["role"] == "tool_results":
                content = [{"type": "tool_result", "tool_use_id": item["id"], "content": item["content"],
                            "is_error": bool(item["is_error"])} for item in entry["results"]]
                if entry.get("text"):
                    content.append({"type": "text", "text": entry["text"]})
                messages.append({"role": "user", "content": content})
            else:
                raise ValueError(f"unknown transcript role {entry['role']!r}")
        return messages

    def build_kwargs(self, request: ProviderRequest) -> dict:
        kwargs = {"model": self.model, "max_tokens": request.max_tokens, "system": request.system,
                  "tools": self._tools(request.tools),
                  "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
                  "messages": self.native_messages(request.transcript),
                  "output_config": {"effort": self.effort}}
        if self.prompt_cache:
            kwargs["cache_control"] = {"type": "ephemeral"}  # automatic caching of the growing prefix
        if self.fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = self.fallbacks
        return kwargs

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        client = self._client_for(request.timeout_s)
        kwargs = self.build_kwargs(request)
        try:
            import anthropic
        except ImportError:  # an injected client without the SDK (unit tests) maps its own errors
            anthropic = None
        started = time.monotonic()
        try:
            api = client.beta.messages if self.fallbacks else client.messages
            message = api.create(**kwargs)
        except Exception as error:  # mapped to the documented typed SDK errors below
            raise _map_anthropic_error(error, anthropic) from error
        latency = time.monotonic() - started
        data = message.to_dict()
        return _anthropic_response(data, self.model, getattr(message, "_request_id", None), latency)


def _anthropic_response(data: dict, requested: str, request_id: str | None, latency: float) -> ProviderResponse:
    content = data.get("content") or []
    if not isinstance(content, list):
        raise ProviderError("invalid_response", "response content is not a list", request_id=request_id)
    text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
    calls = [ToolCall(block["id"], block["name"], block.get("input")) for block in content
             if block.get("type") == "tool_use"]
    usage_raw = data.get("usage") or {}
    usage = {key: int(usage_raw.get(key) or 0) for key in
             ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    iterations = usage_raw.get("iterations") or []
    fallback = any(isinstance(item, dict) and item.get("type") == "fallback_message" for item in iterations)
    return ProviderResponse(text=text, tool_calls=calls, stop_reason=str(data.get("stop_reason")), native=content,
                            usage=usage, model_requested=requested, model_served=str(data.get("model") or requested),
                            request_id=request_id, served_by_fallback=fallback, latency_s=latency,
                            extra={"stop_details": data.get("stop_details")})


def _map_anthropic_error(error: Exception, anthropic) -> ProviderError:
    """Most-specific-first mapping of the SDK's typed errors (claude-api skill, Error Handling)."""
    if isinstance(error, ProviderError):
        return error
    if anthropic is None:
        return ProviderError("connection", type(error).__name__)
    request_id = getattr(error, "request_id", None)
    status = getattr(error, "status_code", None)
    if isinstance(error, anthropic.APITimeoutError):
        return ProviderError("timeout", "request timed out", request_id=request_id)
    if isinstance(error, anthropic.APIConnectionError):
        return ProviderError("connection", "connection error", request_id=request_id)
    if isinstance(error, anthropic.RateLimitError):
        retry_after = None
        try:
            retry_after = float(error.response.headers.get("retry-after"))
        except (AttributeError, TypeError, ValueError):
            pass
        return ProviderError("rate_limit", "rate limited", status=status, retry_after=retry_after, request_id=request_id)
    if isinstance(error, anthropic.AuthenticationError):
        return ProviderError("auth", "authentication failed", status=status, request_id=request_id)
    if isinstance(error, anthropic.PermissionDeniedError):
        return ProviderError("permission", "permission denied", status=status, request_id=request_id)
    if isinstance(error, anthropic.NotFoundError):
        return ProviderError("not_found", "model or endpoint not found", status=status, request_id=request_id)
    if isinstance(error, anthropic.BadRequestError):
        return ProviderError("bad_request", getattr(error, "message", "bad request")[:300], status=status,
                             request_id=request_id)
    if isinstance(error, anthropic.APIStatusError):
        kind = "server" if (status or 0) >= 500 else "bad_request"
        return ProviderError(kind, f"HTTP {status}", status=status, request_id=request_id)
    return ProviderError("invalid_response", type(error).__name__)


# ----------------------------------------------------------------------------------- test double
class ScriptedProvider:
    """TEST DOUBLE. Replays scripted turns; every artifact it touches is marked test_double=true and
    can never be reported as an experiment. `script` items are ProviderResponse, ProviderError, or a
    callable(request) returning one of them."""

    name = "scripted-test-double"
    is_test_double = True

    def __init__(self, script: list, model: str = "test-double", delay_s: float = 0.0,
                 clock_advance: Callable[[float], None] | None = None):
        self.model = model
        self._script = list(script)
        self.requests: list[ProviderRequest] = []
        self.delay_s = delay_s
        self._advance = clock_advance

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "test_double": True}

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if self._advance:
            self._advance(self.delay_s)
        if not self._script:
            raise ProviderError("invalid_response", "test double script exhausted")
        item = self._script.pop(0)
        if callable(item) and not isinstance(item, (ProviderResponse, ProviderError)):
            item = item(request)
        if isinstance(item, ProviderError):
            raise item
        return item


def scripted_turn(name: str | None, arguments: dict | None = None, *, text: str = "", call_id: str | None = None,
                  usage: dict | None = None, model: str = "test-double", stop_reason: str | None = None) -> ProviderResponse:
    """One scripted assistant turn calling `name` (or plain text when name is None)."""
    calls = [] if name is None else [ToolCall(call_id or f"call_{name}_{abs(hash(json.dumps(arguments, sort_keys=True, default=str))) % 10**8}",
                                              name, arguments or {})]
    native = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input} for call in calls]
    return ProviderResponse(text=text, tool_calls=calls, stop_reason=stop_reason or ("tool_use" if calls else "end_turn"),
                            native=native, usage=usage or {"input_tokens": 0, "output_tokens": 0,
                                                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                            model_requested=model, model_served=model)


def make_provider(config: dict) -> Provider:
    """Build a provider from a JSON config (no secrets inside: only the NAME of the key variable)."""
    kind = config.get("provider")
    if kind == "anthropic":
        return AnthropicProvider(config.get("model", "claude-opus-5-5"), effort=config.get("effort", "high"),
                                 api_key_env=config.get("api_key_env", "ANTHROPIC_API_KEY"),
                                 base_url=config.get("base_url"), fallbacks=config.get("fallbacks"),
                                 strict_tools=config.get("strict_tools", True),
                                 prompt_cache=config.get("prompt_cache", True))
    if kind == "openai_compatible":
        from auditlab.research.agents.openai_chat import OpenAICompatibleProvider
        return OpenAICompatibleProvider.from_config(config)
    raise ProviderError("configuration", f"unknown provider {kind!r}")
