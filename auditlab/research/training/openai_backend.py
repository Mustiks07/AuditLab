"""Stdlib HTTP backend for the OpenAI-compatible fine-tuning API.

Verified 2026-11-21 against https://developers.openai.com/api/reference/resources/fine_tuning and
https://developers.openai.com/api/reference/resources/files (the platform.openai.com/docs URLs 301 there):
  * POST /files, multipart/form-data fields `file` and `purpose` ("fine-tune"); returns {id, ...}.
  * POST /fine_tuning/jobs: model, training_file, validation_file, method {type "supervised",
    supervised {hyperparameters {n_epochs, batch_size, learning_rate_multiplier}}} (top-level
    `hyperparameters` is deprecated in favour of `method`), suffix (1-64 chars), seed (0..2147483647),
    metadata (up to 16 pairs, keys <= 64 chars, values <= 512 chars).
  * GET /fine_tuning/jobs (after, limit), GET /fine_tuning/jobs/{id},
    GET /fine_tuning/jobs/{id}/events (after, limit), GET /fine_tuning/jobs/{id}/checkpoints (after,
    limit; has_more). Job status: validating_files, queued, running, pausing, paused, succeeded, failed,
    cancelled; error {code, message, param}; fine_tuned_model; trained_tokens.
No idempotency header is documented for these endpoints. An `Idempotency-Key` header is sent anyway
(harmless if ignored) but NOT relied on: train.py records each step before and after the request and,
on resume, adopts a job carrying its metadata key instead of creating another.

Access (https://developers.openai.com/api/docs/deprecations, "Update to OpenAI's self-serve
fine-tuning"): since 2026-05-07 job creation is unavailable to organizations that never ran
fine-tuning; since 2026-07-02 also to organizations without inference on a fine-tuned model in the
past 60 days; on 2027-01-06 job creation ends for all. A rejected create is reported as missing
access, never as a result.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from pathlib import Path

from auditlab.research.agents.openai_chat import DEFAULT_BASE_URL, http_request
from auditlab.research.agents.providers import ProviderError

ACCESS_NOTICE = ("OpenAI self-serve fine-tuning job creation is restricted since 2026-05-07 and ends "
                 "2027-01-06 (developers.openai.com/api/docs/deprecations); access must be confirmed by "
                 "the provider, not assumed")
MAX_PAGES = 50


class OpenAIFineTuningBackend:
    name = "openai_compatible"

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, api_key_env: str = "OPENAI_API_KEY",
                 timeout_s: float = 120.0):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s

    @classmethod
    def from_config(cls, config: dict) -> "OpenAIFineTuningBackend":
        return cls(base_url=config.get("base_url") or DEFAULT_BASE_URL,
                   api_key_env=config.get("api_key_env", "OPENAI_API_KEY"),
                   timeout_s=float(config.get("request_timeout_s", 120.0)))

    def describe(self) -> dict:
        return {"provider": self.name, "base_url": self.base_url, "api_key_env": self.api_key_env}

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderError("configuration", f"secret environment variable {self.api_key_env} is not set")
        return key

    def _call(self, method: str, path: str, *, payload: dict | None = None, query: dict | None = None,
              body: bytes | None = None, content_type: str = "application/json", headers: dict | None = None):
        url = self.base_url + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        result = http_request(method, url, key=self._key(), body=body, content_type=content_type,
                              timeout_s=self.timeout_s, extra_headers=headers)
        if not isinstance(result.body, dict):
            raise ProviderError("invalid_response", f"{method} {path}: response is not a JSON object")
        return result.body

    def _list(self, path: str) -> list[dict]:
        items, query = [], {"limit": 100}
        for _ in range(MAX_PAGES):
            page = self._call("GET", path, query=query)
            data = page.get("data")
            if not isinstance(data, list):
                raise ProviderError("invalid_response", f"GET {path}: no data list")
            items.extend(item for item in data if isinstance(item, dict))
            if not page.get("has_more") or not data or not isinstance(data[-1], dict) or not data[-1].get("id"):
                break
            query = {"limit": 100, "after": data[-1]["id"]}
        return items

    def preflight(self, config: dict, *, network: bool = False) -> dict:
        summary = dict(self.describe(), key_present=bool(os.environ.get(self.api_key_env)),
                       notice=ACCESS_NOTICE, network_checked=False)
        if network:
            self._call("GET", "/fine_tuning/jobs", query={"limit": 1})  # free; proves the key is accepted
            summary["network_checked"] = True
        return summary

    def upload(self, path) -> str:
        path = Path(path)
        boundary = "auditlab-" + uuid.uuid4().hex
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nfine-tune\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                f"Content-Type: application/jsonl\r\n\r\n").encode("utf-8")
        body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
        result = self._call("POST", "/files", body=body, content_type=f"multipart/form-data; boundary={boundary}")
        if not isinstance(result.get("id"), str):
            raise ProviderError("invalid_response", "file upload returned no id")
        return result["id"]

    def create_job(self, *, model: str, training_file: str, validation_file: str | None, method: dict,
                   suffix: str | None, seed: int | None, metadata: dict, idempotency_key: str) -> dict:
        payload = {"model": model, "training_file": training_file, "method": method, "metadata": metadata}
        if validation_file:
            payload["validation_file"] = validation_file
        if suffix:
            payload["suffix"] = suffix
        if seed is not None:
            payload["seed"] = int(seed)
        job = self._call("POST", "/fine_tuning/jobs", payload=payload, headers={"Idempotency-Key": idempotency_key})
        if not isinstance(job.get("id"), str):
            raise ProviderError("invalid_response", "job creation returned no id")
        return job

    def find_job(self, metadata_key: str, metadata_value: str) -> dict | None:
        for job in self._list("/fine_tuning/jobs"):
            if (job.get("metadata") or {}).get(metadata_key) == metadata_value:
                return job
        return None

    def get_job(self, job_id: str) -> dict:
        job = self._call("GET", f"/fine_tuning/jobs/{urllib.parse.quote(job_id, safe='')}")
        if job.get("id") != job_id:
            raise ProviderError("invalid_response", "retrieved job id differs from the requested one")
        return job

    def list_events(self, job_id: str) -> list[dict]:
        return self._list(f"/fine_tuning/jobs/{urllib.parse.quote(job_id, safe='')}/events")

    def list_checkpoints(self, job_id: str) -> list[dict]:
        return self._list(f"/fine_tuning/jobs/{urllib.parse.quote(job_id, safe='')}/checkpoints")
