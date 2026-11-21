"""Bounded retries, rate-limit handling, deadline and spend guard around one provider call.

Every try (successful or not) is recorded and charged; nothing is retried silently. A retry is made
only for retryable kinds, only while the attempt deadline leaves room, and the wait honours the
provider's Retry-After. Before each try the spend guard compares the worst-case cost of the call with
the remaining attempt budget; an unpriceable model cannot be called with a budget.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
import math
from typing import Callable

from auditlab.research.agents.pricing import cost_usd, worst_case_cost
from auditlab.research.agents.providers import (Provider, ProviderError, ProviderRequest, ProviderResponse,
                                                estimate_input_tokens)


@dataclass(frozen=True)
class RetryPolicy:
    max_tries: int = 4            # tries per model call, including the first
    base_delay_s: float = 1.0
    max_delay_s: float = 20.0
    per_call_timeout_s: float = 45.0
    deadline_margin_s: float = 1.0  # keep this much of the attempt deadline for the environment step


class SpendGuard:
    """Per-attempt budget in USD (None = unpriced test double only). Reports what was really charged."""

    def __init__(self, limit_usd: float | None, price_override: dict | None = None, allow_unpriced: bool = False):
        if limit_usd is not None and (type(limit_usd) not in (int, float) or not math.isfinite(limit_usd) or limit_usd < 0):
            raise ValueError("budget must be finite and non-negative")
        self.limit_usd = limit_usd
        self.spent_usd = 0.0
        self.unpriced_calls = 0
        self.price_override = price_override
        self.allow_unpriced = allow_unpriced

    def check(self, model: str, request: ProviderRequest) -> None:
        bound = worst_case_cost(model, estimate_input_tokens(request), request.max_tokens, self.price_override)
        if bound is None:
            if not self.allow_unpriced:
                raise ProviderError("budget", f"model {model!r} has no price; refusing a paid call without a budget")
            return
        if self.limit_usd is None or self.spent_usd + bound > self.limit_usd:
            raise ProviderError("budget", f"worst-case call cost {bound:.4f} USD would exceed the attempt budget "
                                          f"({self.spent_usd:.4f} of {self.limit_usd} USD spent)")

    def charge(self, model: str, usage: dict) -> tuple[float | None, str | None]:
        cost, reason = cost_usd(model, usage, self.price_override)
        if cost is None:
            self.unpriced_calls += 1
        else:
            self.spent_usd += cost
        return cost, reason


def request_digest(request: ProviderRequest) -> str:
    payload = {"system": request.system, "transcript": request.transcript, "max_tokens": request.max_tokens,
               "tools": [(tool.name, tool.description, tool.input_schema) for tool in request.tools]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class ModelCaller:
    """Makes one logical model call with bounded tries. `records` collects one entry per try."""

    def __init__(self, provider: Provider, policy: RetryPolicy, guard: SpendGuard,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None):
        self.provider = provider
        self.policy = policy
        self.guard = guard
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random(0)
        self.records: list[dict] = []
        self.logical_calls = 0

    def call(self, request: ProviderRequest, deadline: float) -> ProviderResponse:
        self.logical_calls += 1
        digest = request_digest(request)
        last: ProviderError | None = None
        for try_index in range(self.policy.max_tries):
            remaining = deadline - self.clock() - self.policy.deadline_margin_s
            if remaining <= 0:
                raise ProviderError("deadline", "no time left for a model call before the attempt deadline")
            request.timeout_s = min(self.policy.per_call_timeout_s, remaining)
            self.guard.check(self.provider.model, request)
            started = self.clock()
            record = {"logical_call": self.logical_calls, "try": try_index + 1, "request_sha256": digest,
                      "model_requested": self.provider.model, "timeout_s": round(request.timeout_s, 3)}
            try:
                response = self.provider.complete(request)
            except ProviderError as error:
                record.update(status="error", error=error.to_dict(), latency_s=self.clock() - started,
                              usage=None, cost_usd=None, cost_note="usage unavailable; call reservation retained")
                reserved = worst_case_cost(self.provider.model, estimate_input_tokens(request), request.max_tokens, self.guard.price_override)
                self.guard.spent_usd += reserved or 0.0
                self.guard.unpriced_calls += 1
                record["budget_debit_usd"] = reserved
                self.records.append(record)
                last = error
                if not error.retryable or try_index + 1 >= self.policy.max_tries:
                    raise
                wait = error.retry_after if error.retry_after is not None else min(
                    self.policy.max_delay_s, self.policy.base_delay_s * 2 ** try_index) * (0.5 + self.rng.random())
                if self.clock() + wait >= deadline - self.policy.deadline_margin_s:
                    raise ProviderError("deadline", f"retry wait {wait:.1f}s would pass the attempt deadline "
                                                    f"after {error.kind}") from error
                record["waited_s"] = wait
                self.sleep(wait)
                continue
            cost, reason = self.guard.charge(response.model_served, response.usage)
            record.update(status="ok", latency_s=self.clock() - started, usage=response.usage, cost_usd=cost,
                          cost_note=reason, budget_debit_usd=cost, model_served=response.model_served,
                          served_by_fallback=response.served_by_fallback, stop_reason=response.stop_reason,
                          request_id=response.request_id, test_double=bool(getattr(self.provider, "is_test_double", False)))
            self.records.append(record)
            return response
        raise last or ProviderError("invalid_response", "no try was made")
