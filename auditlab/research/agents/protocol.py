"""Run protocols. Every attempt records exactly one protocol_id; only the official one may be compared
with the organizers' limits, and even it is AuditLab's own measurement, not an official acceptance.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

ARTIFACT_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Protocol:
    protocol_id: str
    time_limit_s: float      # wall clock from entry into reset, INCLUDING model API latency and retries
    max_actions: int         # environment actions including submit (never extended)
    official: bool           # True only for the published 60 s / 100 action limits
    resumable: bool          # a resumed attempt restarts the clock, so it can never be official
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


PROTOCOLS = {
    "genesis-official-60s": Protocol(
        "genesis-official-60s", 60.0, 100, True, False,
        "Published fintech-checks limits: 60 s from reset including the policy, API latency and retries; "
        "100 actions including submit. AuditLab measurement, not the organizers' acceptance."),
    "auditlab-research-600s": Protocol(
        "auditlab-research-600s", 600.0, 100, False, True,
        "Research protocol: same 100 actions and tools, 600 s wall clock for slow API models. "
        "Never reported as an official-protocol result."),
}


def get_protocol(protocol_id: str) -> Protocol:
    try:
        return PROTOCOLS[protocol_id]
    except KeyError:
        raise ValueError(f"unknown protocol_id {protocol_id!r}; known: {sorted(PROTOCOLS)}") from None
