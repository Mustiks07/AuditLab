"""Shared wire types and module boundaries. Runtime schema validation belongs to the runner."""
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict

IssueType = Literal["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]
Mode = Literal["train", "closed"]
MAX_STEPS = 100
PAGE_LIMIT = 50
MAX_FINDINGS = 30
MAX_EVIDENCE = 10
MAX_EXPLANATION = 1000


class Action(TypedDict):
    action: str
    args: dict[str, object]


class Finding(TypedDict):
    type: IssueType
    affected_ids: list[str]
    claimed_amount_minor: int
    currency: str
    evidence_ids: list[str]
    explanation: str


class Submission(TypedDict):
    episode_id: str
    findings: list[Finding]


class ToolReply(TypedDict):
    ok: bool
    result: NotRequired[object]
    error: NotRequired[dict[str, str]]


class Score(TypedDict):
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    reward: float


Observation = dict[str, object]
StepResult = tuple[Observation, float, bool, bool, dict[str, object]]


class PublicSession(Protocol):
    """Reads only public data. The runner owns the step budget and finalization."""

    def inspect(self, action: Action) -> ToolReply: ...


class Policy(Protocol):
    def act(self, public_observation: Observation) -> Action: ...


class Environment(Protocol):
    def reset(self, seed: int, config: dict[str, object]) -> tuple[Observation, dict[str, object]]: ...

    def step(self, action: Action) -> StepResult: ...


class Generator(Protocol):
    def __call__(self, seed: int, config: dict[str, object], output_dir: Path) -> None: ...


class Verifier(Protocol):
    def __call__(self, public_dir: Path) -> list[Finding]: ...
