"""Training backend protocol. Errors are ProviderError (same kinds as the chat providers)."""
from __future__ import annotations

from typing import Protocol

# Provider job states (OpenAI fine-tuning job object, verified 2026-11-21) and how training.json reports them.
TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE_JOB_STATES = frozenset({"validating_files", "queued", "running", "pausing", "paused"})


class TrainingBackend(Protocol):
    name: str

    def preflight(self, config: dict, *, network: bool = False) -> dict:
        """Access summary without secrets. With network=True it makes one free authenticated read."""

    def upload(self, path) -> str:
        """Upload a training/validation JSONL file; returns the provider file id."""

    def create_job(self, *, model: str, training_file: str, validation_file: str | None, method: dict,
                   suffix: str | None, seed: int | None, metadata: dict, idempotency_key: str) -> dict:
        """Create one fine-tuning job; returns the provider job object."""

    def find_job(self, metadata_key: str, metadata_value: str) -> dict | None:
        """An existing job carrying this metadata value (used to adopt a job after a crash)."""

    def get_job(self, job_id: str) -> dict: ...

    def list_events(self, job_id: str) -> list[dict]: ...

    def list_checkpoints(self, job_id: str) -> list[dict]: ...
