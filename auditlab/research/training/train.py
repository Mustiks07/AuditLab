"""train(training_config, curriculum_ref, output_dir) -> training_ref (path to training.json).

Phases: validate -> dry_run | access check -> upload -> create job -> poll -> finalize. Every paid or
identity-bearing step is recorded in output_dir/training_state.json (an append-only event list, written
atomically) before the next step starts, so a resume never duplicates paid work:
  * an uploaded file id is reused;
  * a job id, once stored, is only ever polled again, never recreated;
  * before creating a job the provider's job list is searched for the deterministic job key (derived
    from the dataset hash and settings) and a match is adopted, which covers a crash between the create
    request and the state write.
Providers: "openai_compatible" (openai_backend.py, paid API) and "local_lora" (local_lora.py, TRL LoRA
on a GPU machine). A paid job is created only with a configured price, budget_usd > 0, an estimate
within the budget, the key variable present and an explicit "confirm_paid": true; a local LoRA job only
when its preflight finds the GPU stack, CUDA, disk space and the base model. Otherwise training.json says
not_started_missing_access and lists every missing condition. Nothing here fabricates a result; a
record made with a test-double backend or stand-in trainer carries "mock": true.

training.json carries at top level the fields the acceptance check reads (kind, claimed_as, job_id,
checkpoint_id, result_model, mock, provider, status) plus the teacher-source record: dataset sources
must be within `allowed_teacher_sources` (default oracle_demonstration + self), third_party_model needs
`teacher_terms_acknowledged` in both the dataset and the config, and "self" must name base_model.

Example config (paths are relative to the config file):
    {"schema_version": "1.0", "protocol_id": "auditlab-research-600s", "provider": "openai_compatible",
     "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY",
     "base_model": "gpt-4.1-mini-2025-04-14", "dataset": "runs/sft-random",
     "validation_dataset": null, "hyperparameters": {"n_epochs": 3}, "seed": 7, "suffix": null,
     "price_per_million_training_tokens": 5.0, "budget_usd": 20, "confirm_paid": false,
     "allowed_splits": ["train"], "poll_interval_s": 60}
The price above is an example only; it must be checked against the provider's current price list.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from fractions import Fraction
from pathlib import Path

from auditlab.research.agents.providers import ProviderError
from auditlab.research.training.backend import ACTIVE_JOB_STATES, TERMINAL_JOB_STATES
from auditlab.research.training.dataset import (DEFAULT_SPLITS, DEFAULT_TEACHER_SOURCES, MIN_EXAMPLES, DatasetError,
                                                acknowledged, check_allowed_splits, check_teacher_policy,
                                                curriculum_problem, git_source_sha, load_curriculum, load_dataset,
                                                now_iso, read_json, sha256_file, validate_example)

SCHEMA_VERSION = "1.0"
PROVIDERS = ("openai_compatible", "local_lora")
STATUSES = ("dry_run", "not_started_missing_access", "running", "succeeded", "failed", "cancelled")
SERIES_NOTE = ("trainable-model series (kind=weights); never pooled with the Claude series into one causal claim")
ACCESS_ERROR_KINDS = frozenset({"auth", "permission", "configuration", "not_found"})


class TrainingError(ValueError):
    """Invalid config, dataset or curriculum: nothing was started."""


# ----------------------------------------------------------------------------------- state
class TrainingState:
    """Append-only event log; the current state is the latest value of each key."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.events: list[dict] = []
        if self.path.is_file():
            data = read_json(self.path)
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise TrainingError(f"{self.path} is not a training state file")
            self.events = data["events"]

    def append(self, phase: str, **data) -> None:
        self.events.append({"seq": len(self.events), "at": now_iso(), "phase": phase, **data})
        _write_scrubbed(self.path, {"schema_version": SCHEMA_VERSION, "events": self.events})

    def get(self, key: str, default=None):
        for event in reversed(self.events):
            if key in event:
                return event[key]
        return default


def _write_scrubbed(path: Path, data: dict) -> None:
    """Atomic JSON write; any configured secret value that somehow reached the data is redacted."""
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


_SECRET_ENV_NAMES: set[str] = {"OPENAI_API_KEY"}


# ----------------------------------------------------------------------------------- config
def _load_config(training_config) -> tuple[dict, Path, str]:
    if isinstance(training_config, dict):
        config = dict(training_config)
        base = Path(config.get("base_dir") or ".")
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    else:
        path = Path(training_config)
        config = read_json(path)
        base, digest = path.parent, sha256_file(path)
    if not isinstance(config, dict):
        raise TrainingError("training config must be a JSON object")
    return config, base, digest


def _resolve(base: Path, ref) -> Path | None:
    if not ref:
        return None
    path = Path(ref)
    return path if path.is_absolute() else base / path


def _validate(config: dict, base: Path, curriculum_ref) -> dict:
    if config.get("kind", "weights") != "weights":
        raise TrainingError("this path trains weights only; prompt/few_shot are separate kinds")
    provider = _provider(config)
    for field in ("protocol_id", "base_model", "dataset") + (("api_key_env",) if provider == "openai_compatible" else ()):
        if not isinstance(config.get(field), str) or not config[field]:
            raise TrainingError(f"config field {field!r} is required")
    try:
        allowed = check_allowed_splits(config.get("allowed_splits") or DEFAULT_SPLITS)
        sources = check_teacher_policy(config.get("allowed_teacher_sources") or DEFAULT_TEACHER_SOURCES,
                                       config.get("teacher_terms_acknowledged"))
        curriculum = load_curriculum(curriculum_ref)
        datasets = {}
        for role in ("dataset", "validation_dataset"):
            directory = _resolve(base, config.get(role))
            if directory is None:
                continue
            manifest, lines = load_dataset(directory)
            if manifest.get("kind") != "weights":
                raise TrainingError(f"{role} manifest kind is {manifest.get('kind')!r}, not 'weights'")
            for number, line in enumerate(lines):
                problems = validate_example(json.loads(line), manifest["selection_rule"]["max_example_tokens"])
                if problems:
                    raise TrainingError(f"{role} line {number + 1}: {'; '.join(problems[:3])}")
            for source in manifest["source_attempts"]:
                if source["split"] not in allowed:
                    raise TrainingError(f"{role}: attempt {source['attempt_id']} has split {source['split']!r}")
                problem = curriculum_problem(source["episode_ref"], source["split"], curriculum, allowed)
                if problem:
                    raise TrainingError(f"{role}: attempt {source['attempt_id']}: {problem}")
            _check_teachers(role, manifest, config, sources)
            datasets[role] = {"dir": directory, "manifest": manifest, "path": directory / manifest["dataset"]["path"]}
    except (DatasetError, OSError, ValueError, KeyError) as error:
        if isinstance(error, TrainingError):
            raise
        raise TrainingError(f"{type(error).__name__}: {error}") from None
    minimum = int(config.get("min_examples", MIN_EXAMPLES))
    examples = datasets["dataset"]["manifest"]["counts"]["examples"]
    if examples < max(minimum, MIN_EXAMPLES):
        raise TrainingError(f"dataset has {examples} examples; at least {max(minimum, MIN_EXAMPLES)} required")
    return {"allowed": allowed, "curriculum": curriculum, "datasets": datasets, "teacher_sources": sources}


def _provider(config: dict) -> str:
    provider = config.get("provider", "openai_compatible")
    if provider not in PROVIDERS:
        raise TrainingError(f"unsupported training provider {provider!r}; known: {list(PROVIDERS)}")
    return provider


def _check_teachers(role: str, manifest: dict, config: dict, allowed_sources) -> None:
    """Teacher-source guard at training time: the dataset's sources must be allowed by this config,
    third-party transcripts need an acknowledgement in both the dataset and the config, and "self"
    must mean the model being trained."""
    used = {s.get("teacher_source") for s in manifest["source_attempts"]}
    if None in used or not used <= set(allowed_sources):
        raise TrainingError(f"{role}: teacher sources {sorted(map(str, used))} not all in allowed "
                            f"{list(allowed_sources)}")
    policy = manifest.get("teacher_policy") or {}
    if "third_party_model" in used and not (acknowledged(policy.get("teacher_terms_acknowledged"))
                                            and acknowledged(config.get("teacher_terms_acknowledged"))):
        raise TrainingError(f'{role}: third_party_model teachers need "teacher_terms_acknowledged" in the dataset '
                            "and in the training config")
    if "self" in used and config["base_model"] not in (policy.get("self_models") or []):
        raise TrainingError(f"{role}: 'self' teachers were labelled for {policy.get('self_models')}, "
                            f"not for base_model {config['base_model']!r}")


def _plan(config: dict, checked: dict) -> dict:
    hyper = dict(config.get("hyperparameters") or {})
    epochs = hyper.get("n_epochs")
    train_tokens = checked["datasets"]["dataset"]["manifest"]["token_estimate"]["total"]
    valid = checked["datasets"].get("validation_dataset")
    valid_tokens = valid["manifest"]["token_estimate"]["total"] if valid else 0
    tokens = (train_tokens + valid_tokens) * epochs if isinstance(epochs, int) and not isinstance(epochs, bool) and epochs > 0 else None
    price = config.get("price_per_million_training_tokens")
    priced = isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0
    cost = float(Fraction(str(price)) * tokens / 1_000_000) if priced and tokens is not None else None
    budget = config.get("budget_usd")
    plan = {"train_tokens_estimate": train_tokens, "validation_tokens_estimate": valid_tokens, "n_epochs": epochs,
            "billable_tokens_estimate": tokens, "price_per_million_training_tokens": price if priced else None,
            "estimated_usd": cost, "budget_usd": budget,
            "estimate_method": "(train + validation estimated tokens, chars/3) x n_epochs x price; conservative estimate"}
    if _provider(config) == "local_lora":
        plan.update(estimated_usd=None, estimate_method="tokens: (train + validation chars/3) x n_epochs; "
                    "local GPU compute is not priced here (record GPU hours separately)")
    return plan


def _settings(config: dict, checked: dict) -> dict:
    method = {"type": "supervised", "supervised": {"hyperparameters": dict(config.get("hyperparameters") or {})}}
    if _provider(config) == "local_lora":
        method = dict(method, type="supervised_lora", lora=dict(config.get("lora") or {}))
    datasets = checked["datasets"]
    valid = datasets.get("validation_dataset")
    settings = {"base_model": config["base_model"], "method": method, "seed": config.get("seed"),
                "dataset_sha256": datasets["dataset"]["manifest"]["dataset"]["sha256"],
                "validation_sha256": valid["manifest"]["dataset"]["sha256"] if valid else None,
                "job_nonce": config.get("job_nonce")}
    job_key = "al-" + hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:24]
    suffix = config.get("suffix") or f"al-{settings['dataset_sha256'][:12]}"
    return dict(settings, job_key=job_key, suffix=str(suffix)[:64])


def _missing_conditions(config: dict, plan: dict) -> tuple[list[str], str | None]:
    missing, refusal = [], None
    if plan["price_per_million_training_tokens"] is None:
        missing.append("price_per_million_training_tokens is not configured (positive USD per 1M training tokens)")
    budget = plan["budget_usd"]
    budget_ok = isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0
    if not budget_ok:
        missing.append("budget_usd is not configured or not > 0")
    if plan["billable_tokens_estimate"] is None:
        missing.append("hyperparameters.n_epochs must be a positive integer so the cost can be bounded")
    if budget_ok and plan["estimated_usd"] is not None and plan["estimated_usd"] > budget:
        missing.append(f"estimated cost {plan['estimated_usd']:.4f} USD exceeds budget_usd {budget}")
        refusal = "budget_exceeded"
    if not os.environ.get(config["api_key_env"]):
        missing.append(f"environment variable {config['api_key_env']} is not set")
    if config.get("confirm_paid") is not True:
        missing.append('"confirm_paid": true is not set in the config')
    return missing, refusal


def _gate(config: dict, plan: dict, backend) -> tuple[list[str], str | None, dict | None]:
    """(missing conditions, refusal, preflight facts). Never touches the network or starts a process."""
    if _provider(config) == "openai_compatible":
        missing, refusal = _missing_conditions(config, plan)
        return missing, refusal, None
    preflight = backend.preflight(config)
    missing = list(preflight["missing_conditions"])
    if plan["billable_tokens_estimate"] is None:
        missing.append("hyperparameters.n_epochs must be a positive integer so the training budget is fixed")
    return missing, None, preflight


def _planned_requests(config: dict, checked: dict, settings: dict, backend=None) -> list[dict]:
    if _provider(config) == "local_lora":
        return [{"action": "copy dataset into the jobs root", "files": [str(e["path"]) for e in checked["datasets"].values()]},
                {"action": "launch detached trainer process", "command": backend.describe()["trainer"] + ["--config", "<job_dir>/run_config.json"],
                 "job_id": "lora-" + settings["job_key"].removeprefix("al-"), "method": settings["method"],
                 "base_model": settings["base_model"], "seed": settings["seed"]},
                {"action": "poll job_state.json / checkpoints.json / result.json"}]
    base = (config.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    requests = [{"method": "GET", "url": f"{base}/fine_tuning/jobs?limit=1", "purpose": "access preflight (free)"}]
    for role, entry in checked["datasets"].items():
        requests.append({"method": "POST", "url": f"{base}/files", "purpose": f"upload {role}",
                         "fields": {"purpose": "fine-tune", "file": str(entry["path"])}})
    requests.append({"method": "GET", "url": f"{base}/fine_tuning/jobs", "purpose": f"adopt job with key {settings['job_key']}"})
    requests.append({"method": "POST", "url": f"{base}/fine_tuning/jobs", "purpose": "create job (paid)",
                     "body": {"model": settings["base_model"], "training_file": "<uploaded id>",
                              "validation_file": "<uploaded id>" if "validation_dataset" in checked["datasets"] else None,
                              "method": settings["method"], "suffix": settings["suffix"], "seed": settings["seed"],
                              "metadata": {"auditlab_key": settings["job_key"]}}})
    requests.append({"method": "GET", "url": f"{base}/fine_tuning/jobs/<id>", "purpose": "poll"})
    return requests


# ----------------------------------------------------------------------------------- record
def _job_status(raw: str | None) -> str:
    if raw in TERMINAL_JOB_STATES:
        return raw
    return "running"


def _record(*, status: str, state: TrainingState, config: dict, config_sha: str, checked: dict, plan: dict,
            settings: dict, curriculum_ref, mock: bool, job: dict | None = None, extra: dict | None = None) -> dict:
    assert status in STATUSES
    provider = _provider(config)
    datasets = checked["datasets"]
    price = plan["price_per_million_training_tokens"]
    trained = job.get("trained_tokens") if job else None
    actual = (float(Fraction(str(price)) * trained / 1_000_000)
              if price is not None and isinstance(trained, int) and not isinstance(trained, bool) else None)
    error = job.get("error") if job else None

    def dataset_ref(role):
        entry = datasets.get(role)
        if not entry:
            return None
        manifest = entry["manifest"]
        return {"ref": str(entry["path"]), "sha256": manifest["dataset"]["sha256"],
                "manifest_artifact_id": manifest["artifact_id"],
                "manifest_sha256": sha256_file(entry["path"].parent / "dataset_manifest.json"),
                "examples": manifest["counts"]["examples"], "token_estimate": manifest["token_estimate"],
                "teachers": manifest.get("teachers"), "sampling": manifest.get("sampling") and {
                    k: manifest["sampling"][k] for k in ("parent_sha256", "seed", "max_examples", "max_tokens", "kept")}}

    curriculum = checked["curriculum"]
    manifest = datasets["dataset"]["manifest"]
    record = {
        "schema_version": SCHEMA_VERSION, "artifact_id": state.get("artifact_id"), "source_sha": git_source_sha(),
        "protocol_id": config["protocol_id"], "kind": "weights", "claimed_as": "training", "provider": provider,
        "mock": bool(mock), "series": SERIES_NOTE, "status": status,
        "job_id": state.get("job_id"), "checkpoint_id": None,
        "result_model": job.get("fine_tuned_model") if job and status == "succeeded" else None,
        "updated_at": now_iso(), "config_sha256": config_sha, "base_model": config["base_model"],
        "teacher": {"sources": manifest.get("teacher_sources"), "teachers": manifest.get("teachers"),
                    "allowed_sources": list(checked["teacher_sources"]),
                    "self_models": (manifest.get("teacher_policy") or {}).get("self_models"),
                    "teacher_terms_acknowledged": config.get("teacher_terms_acknowledged"),
                    "dataset_teacher_terms_acknowledged": (manifest.get("teacher_policy") or {}).get("teacher_terms_acknowledged")},
        "dataset": dataset_ref("dataset"), "validation_dataset": dataset_ref("validation_dataset"),
        "curriculum": {"ref": str(curriculum_ref), "sha256": curriculum["sha256"], "kind": curriculum["kind"],
                       "artifact_id": curriculum["artifact_id"]},
        "settings": {"method": settings["method"], "suffix": settings["suffix"], "seed": settings["seed"],
                     "job_key": settings["job_key"], "allowed_splits": list(checked["allowed"]),
                     "poll_interval_s": config.get("poll_interval_s", 60)},
        "budget": dict(plan, actual_usd=actual, actual_basis="job.trained_tokens x price" if actual is not None else None,
                       actual_unavailable_reason=None if actual is not None else "no trained_tokens reported yet"),
        "job": {"provider": provider, "job_id": state.get("job_id"),
                "base_url": (config.get("base_url") or "https://api.openai.com/v1") if provider == "openai_compatible" else None,
                "api_key_env": config.get("api_key_env") if provider == "openai_compatible" else None,
                "jobs_root": config.get("jobs_root") if provider == "local_lora" else None,
                "training_file_id": state.get("training_file_id"), "validation_file_id": state.get("validation_file_id"),
                "provider_status": job.get("status") if job else None},
        "checkpoints": [], "events": [],
        "result": {"fine_tuned_model": job.get("fine_tuned_model") if job else None,
                   "trained_tokens": trained, "error": error,
                   "error_reason": None},
    }
    record.update(extra or {})
    checkpoints = [c for c in record["checkpoints"] if isinstance(c, dict) and c.get("id")]
    if checkpoints:
        record["checkpoint_id"] = max(enumerate(checkpoints),
                                      key=lambda item: (int(item[1].get("step_number") or 0), item[0]))[1]["id"]
    if record["result"]["fine_tuned_model"] is None and record["result"]["error_reason"] is None:
        record["result"]["error_reason"] = {
            "dry_run": "dry run: no job was created", "not_started_missing_access": "not started: see missing_conditions",
            "running": "job not finished", "failed": "job failed: see result.error",
            "cancelled": "job cancelled", "succeeded": "provider reported success without a model id"}[status]
    return record


# ----------------------------------------------------------------------------------- train
def train(training_config, curriculum_ref, output_dir, *, dry_run: bool | None = None, max_wait_s: float = 0.0,
          backend=None, sleep=time.sleep, clock=time.monotonic) -> Path:
    """Run (or resume) one weights-training job; returns the path to training.json."""
    config, base, config_sha = _load_config(training_config)
    _SECRET_ENV_NAMES.add(str(config.get("api_key_env") or "OPENAI_API_KEY"))
    checked = _validate(config, base, curriculum_ref)
    plan, settings = _plan(config, checked), _settings(config, checked)
    output_dir = Path(output_dir)
    output = output_dir / "training.json"
    state = TrainingState(output_dir / "training_state.json")
    if not state.events:
        state.append("initialized", artifact_id=f"training-{uuid.uuid4().hex}", job_key=settings["job_key"],
                     dataset_sha256=settings["dataset_sha256"], config_sha256=config_sha)
    elif state.get("job_key") != settings["job_key"]:
        raise TrainingError(f"{output_dir} belongs to a different dataset/settings (job key {state.get('job_key')}); "
                            "use a new output directory")

    if backend is None:  # constructing a backend has no side effects (no network, no process, no files)
        if _provider(config) == "local_lora":
            from auditlab.research.training.local_lora import LocalLoraBackend
            backend = LocalLoraBackend.from_config(config, output_dir)
        else:
            from auditlab.research.training.openai_backend import OpenAIFineTuningBackend
            backend = OpenAIFineTuningBackend.from_config(config)
    mock = config.get("test_double") is True or bool(getattr(backend, "is_test_double", False))

    def write(status, job=None, **extra):
        record = _record(status=status, state=state, config=config, config_sha=config_sha, checked=checked,
                         plan=plan, settings=settings, curriculum_ref=curriculum_ref, mock=mock, job=job, extra=extra)
        _write_scrubbed(output, record)
        return output

    if dry_run is None:
        dry_run = bool(config.get("dry_run"))
    job_id = state.get("job_id")
    if dry_run:
        if job_id:
            return output  # a real job exists; a dry run never overwrites its record
        missing, refusal, preflight = _gate(config, plan, backend)
        state.append("dry_run")
        return write("dry_run", planned_requests=_planned_requests(config, checked, settings, backend),
                     missing_conditions=missing, refusal=refusal, preflight=preflight)

    if job_id is None:
        missing, refusal, preflight = _gate(config, plan, backend)
        if missing:
            state.append("not_started", missing_conditions=missing)
            return write("not_started_missing_access", missing_conditions=missing, refusal=refusal, preflight=preflight)
        try:
            backend.preflight(config, network=True)
            for role, key in (("dataset", "training_file_id"), ("validation_dataset", "validation_file_id")):
                entry = checked["datasets"].get(role)
                if entry and not state.get(key):
                    state.append("upload_started", role=role)
                    state.append("uploaded", **{key: backend.upload(entry["path"])})
            state.append("create_started", job_key=settings["job_key"])
            job = backend.find_job("auditlab_key", settings["job_key"])
            if job is not None:
                state.append("job_adopted", job_id=job["id"])
            else:
                job = backend.create_job(model=settings["base_model"], training_file=state.get("training_file_id"),
                                         validation_file=state.get("validation_file_id"), method=settings["method"],
                                         suffix=settings["suffix"], seed=settings["seed"],
                                         metadata={"auditlab_key": settings["job_key"],
                                                   "auditlab_dataset_sha256": settings["dataset_sha256"]},
                                         idempotency_key=settings["job_key"])
                state.append("job_created", job_id=job["id"])
            job_id = job["id"]
        except ProviderError as error:
            state.append("start_error", error=error.to_dict())
            if error.kind in ACCESS_ERROR_KINDS:
                reason = f"provider refused access ({error.kind}, HTTP {error.status}); see the access notice"
                return write("not_started_missing_access", missing_conditions=[reason], provider_error=error.to_dict())
            return write("failed", provider_error=error.to_dict(),
                         resume="rerun train with the same --out; stored file/job ids are reused")
    return _poll(backend, job_id, state, config, write, max_wait_s, sleep, clock)


def _poll(backend, job_id: str, state: TrainingState, config: dict, write, max_wait_s: float, sleep, clock) -> Path:
    deadline = clock() + max(0.0, float(max_wait_s or 0))
    interval = max(1.0, float(config.get("poll_interval_s", 60)))
    job, poll_error = None, None
    while True:
        try:
            job = backend.get_job(job_id)
            poll_error = None
        except ProviderError as error:
            poll_error = error.to_dict()
            if not error.retryable:
                break
        if job is not None:
            if job.get("status") != state.get("provider_status"):
                state.append("job_status", provider_status=job.get("status"))
            if job.get("status") in TERMINAL_JOB_STATES:
                break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    extra = {"poll_error": poll_error}
    if job is None:
        return write("running", **extra)
    status = _job_status(job.get("status"))
    if (job.get("error") or {}).get("code") == "missing_access":  # the local trainer found its deps missing
        status = "not_started_missing_access"
        extra["missing_conditions"] = [job["error"].get("message")]
    if job.get("status") not in TERMINAL_JOB_STATES | ACTIVE_JOB_STATES:
        extra["unknown_provider_status"] = job.get("status")
    try:
        extra["events"] = backend.list_events(job_id)
        extra["checkpoints"] = backend.list_checkpoints(job_id) if status != "running" else []
    except ProviderError as error:
        extra["listing_error"] = error.to_dict()
    if status in TERMINAL_JOB_STATES:
        state.append("finalized", status=status, fine_tuned_model=job.get("fine_tuned_model"))
    return write(status, job=job, **extra)


def status(output_dir) -> dict:
    """The stored record (no network)."""
    output_dir = Path(output_dir)
    record = read_json(output_dir / "training.json") if (output_dir / "training.json").is_file() else None
    state = TrainingState(output_dir / "training_state.json")
    return {"training": record, "state_events": len(state.events), "job_id": state.get("job_id"),
            "training_file_id": state.get("training_file_id")}
