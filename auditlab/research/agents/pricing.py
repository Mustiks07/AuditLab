"""Token prices in USD per million tokens. Cost is computed from the usage the provider reported.

Anthropic list prices are copied from the Claude API reference cached 2026-11-03 (claude-api skill,
"Current Models"); verify against https://platform.claude.com/docs/en/about-claude/pricing.md before a
paid run. Cache writes are billed at 1.25x input (5-minute TTL) and cache reads at 0.1x input unless
the table gives an explicit cache-read price. Models absent from the table need prices in the run
config; otherwise cost is null with a reason, never 0.
"""
from __future__ import annotations

from fractions import Fraction

PRICE_SOURCE = "claude-api skill model table cached 2026-11-03; first-party Claude API list prices"

# model id -> (input, output, cache_read or None) USD per 1M tokens
ANTHROPIC_PRICES: dict[str, tuple[str, str, str | None]] = {
    "claude-opus-5-5": ("4", "20", "0.20"),
    "claude-sonnet-5-5": ("2", "10", "0.10"),
    "claude-haiku-5-5": ("0.10", "0.50", None),
    "claude-opus-5": ("5", "25", None),
    "claude-sonnet-5": ("2", "10", None),
    "claude-fable-5-2": ("8", "40", "0.20"),
}

MILLION = Fraction(1_000_000)


def price_for(model: str, override: dict | None = None) -> dict | None:
    """{"input","output","cache_read","cache_write"} as Fractions per token, or None if unknown."""
    if override and model in override:
        entry = override[model]
        inp, out = Fraction(str(entry["input"])), Fraction(str(entry["output"]))
        read = Fraction(str(entry["cache_read"])) if entry.get("cache_read") is not None else inp / 10
        write = Fraction(str(entry["cache_write"])) if entry.get("cache_write") is not None else inp * Fraction(5, 4)
    elif model in ANTHROPIC_PRICES:
        a, b, c = ANTHROPIC_PRICES[model]
        inp, out = Fraction(a), Fraction(b)
        read = Fraction(c) if c is not None else inp / 10
        write = inp * Fraction(5, 4)
    else:
        return None
    return {key: value / MILLION for key, value in
            (("input", inp), ("output", out), ("cache_read", read), ("cache_write", write))}


def cost_usd(model: str, usage: dict, override: dict | None = None) -> tuple[float | None, str | None]:
    """Exact cost of one call from reported usage; (None, reason) when it cannot be priced."""
    prices = price_for(model, override)
    if prices is None:
        return None, f"no price for model {model!r}"
    total = (prices["input"] * int(usage.get("input_tokens") or 0)
             + prices["output"] * int(usage.get("output_tokens") or 0)
             + prices["cache_read"] * int(usage.get("cache_read_input_tokens") or 0)
             + prices["cache_write"] * int(usage.get("cache_creation_input_tokens") or 0))
    return float(total), None


def worst_case_cost(model: str, input_tokens: int, max_output_tokens: int,
                    override: dict | None = None) -> float | None:
    """Upper bound of one call before it is sent, used by the budget guard (all input uncached)."""
    prices = price_for(model, override)
    if prices is None:
        return None
    return float(prices["input"] * input_tokens * Fraction(5, 4) + prices["output"] * max_output_tokens)
