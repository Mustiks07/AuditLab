"""Local LoRA supervised fine-tuning of an open-weights chat model (Hugging Face TRL + PEFT).

Runs on a separate GPU machine (resource profile "gpu-training"; dependencies in
docker/train-gpu/requirements.in). It is never part of the offline AuditLab bundle and nothing in the
offline release imports it. This module itself is stdlib-only: torch/transformers/peft/trl/datasets
are imported lazily inside the training entry point.

APIs verified 2026-11-21 (docs pages, not run here: this machine has no CUDA GPU and none of the packages):
  * https://huggingface.co/docs/trl/sft_trainer (TRL v1.18.2): SFTTrainer(model=<hub id or path>,
    args=SFTConfig, train_dataset, peft_config=LoraConfig, callbacks); conversational datasets get the
    chat template applied automatically; "Tool Calling with SFT": messages with `tool_calls` and `tool`
    role messages plus a `tools` column of JSON schemas. SFTConfig fields used: output_dir,
    num_train_epochs, per_device_train_batch_size, gradient_accumulation_steps, learning_rate,
    max_length (default 1024), assistant_only_loss (needs {% generation %} in the chat template; TRL
    patches known families such as Qwen3), save_strategy, save_steps, logging_steps, seed, data_seed,
    report_to. Adapters: higher learning rate (about 1e-4) suggested.
  * https://huggingface.co/docs/trl/dataset_formats (Tool Calling): tool_calls `arguments` are a JSON
    OBJECT (dict), tool messages carry `name`; build the Dataset with Dataset.from_list(...,
    on_mixed_types="use_json") (datasets >= 4.7) or, on older datasets, store `tools` as a JSON string.
    to_trl_row() converts our OpenAI-format JSONL (arguments as JSON strings) accordingly.
  * https://huggingface.co/docs/peft/package_reference/lora (PEFT v0.22.0): LoraConfig(r, lora_alpha,
    lora_dropout, target_modules incl. "all-linear", bias, task_type="CAUSAL_LM").
  * https://docs.vllm.ai/en/latest/features/lora.html: `vllm serve <base> --enable-lora
    --lora-modules <name>=<adapter dir> [--max-lora-rank N]`; requests select the adapter through the
    `model` parameter; an adapter dir must contain adapter_config.json. The page's example uses
    /v1/completions; use with /v1/chat/completions is implied but not shown.
  * https://docs.vllm.ai/en/latest/features/tool_calling.html: `--enable-auto-tool-choice` plus
    `--tool-call-parser` (e.g. `hermes` for Qwen/Hermes, `llama3_json` with a tool chat template for
    Llama 3.x); tool_choice "auto" supported; parallel tool calls depend on model/parser.
Not verified: exact adapter file names written by PEFT save_pretrained; that vLLM rejects an adapter
trained on a different base model.

Serving the result with the same agent (on the GPU machine, after status "succeeded"):
    vllm serve <base_model> --enable-lora --max-lora-rank <r> \
        --lora-modules <lora_module_name>=<job_dir>/adapter \
        --enable-auto-tool-choice --tool-call-parser <parser for the model family>
then run the auditor with provider config
    {"provider": "openai_compatible", "model": "<lora_module_name>", "base_url": "http://<host>:8000/v1",
     "api_key_env": "<env var holding vLLM's --api-key, if set>"}
so auditlab.research.agents.openai_chat.OpenAICompatibleProvider evaluates the trained adapter with the
same prompt and tools; the base model is evaluated the same way with model=<base_model>.

Job model: job id = "lora-" + hash of dataset + settings. A job directory holds run_config.json,
job_state.json (pid, start time, log path, command, exit code), runner.json (written by the process),
checkpoints.json (step-numbered adapter checkpoints) and result.json. A job id whose directory exists is
never launched again, whether its process is running, finished or was interrupted while launching.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from auditlab.research.training.dataset import now_iso, read_json, sha256_file, write_json

REQUIRED_MODULES = ("torch", "transformers", "peft", "trl", "datasets")
DEFAULT_MIN_FREE_DISK_GB = 50.0
MISSING_DEPS_EXIT = 3
_PROCESSES: dict[str, subprocess.Popen] = {}  # launched in this interpreter: lets us reap and read exit codes
REPO_ROOT = Path(__file__).resolve().parents[3]


# ----------------------------------------------------------------------------------- preflight
def missing_conditions(config: dict, *, find_spec=importlib.util.find_spec, disk_path=None) -> tuple[list[str], dict]:
    """Every condition that prevents a real run, plus facts found. Starts no process."""
    missing, facts = [], {}
    absent = [name for name in REQUIRED_MODULES if find_spec(name) is None]
    facts["modules_missing"] = absent
    for name in absent:
        missing.append(f"python module {name!r} is not importable (install docker/train-gpu/requirements.in "
                       "on the GPU training machine)")
    if "torch" in absent:
        facts["cuda_available"] = None
        missing.append("CUDA availability unknown because torch is not importable")
    else:
        try:
            import torch  # noqa: PLC0415 - optional, GPU machine only
            facts["cuda_available"] = bool(torch.cuda.is_available())
            facts["cuda_device_count"] = int(torch.cuda.device_count()) if facts["cuda_available"] else 0
        except Exception as error:  # noqa: BLE001 - a broken torch install is a missing condition
            facts["cuda_available"] = None
            missing.append(f"torch failed to import: {type(error).__name__}")
        else:
            if not facts["cuda_available"]:
                missing.append("CUDA is not available (torch.cuda.is_available() is False)")
    model = config.get("base_model")
    if not isinstance(model, str) or not model:
        missing.append("base_model (local model path or hub id) is not set")
    elif model.startswith((".", "/", "\\")) or (len(model) > 1 and model[1] == ":") or Path(model).exists():
        if not Path(model).is_dir():
            missing.append(f"base_model path {model!r} is not a directory")
        facts["base_model_kind"] = "local_path"
    else:
        facts["base_model_kind"] = "hub_id (downloaded on the GPU machine at run time)"
    probe = Path(disk_path or config.get("jobs_root") or ".")
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    free_gb = shutil.disk_usage(probe).free / 1e9
    need = float(config.get("min_free_disk_gb", DEFAULT_MIN_FREE_DISK_GB))
    facts["free_disk_gb"] = round(free_gb, 1)
    if free_gb < need:
        missing.append(f"free disk {free_gb:.1f} GB at {probe} is below min_free_disk_gb {need}")
    return missing, facts


# ----------------------------------------------------------------------------------- job files
def record_checkpoint(job_dir: Path, job_id: str, step: int, path, metrics: dict | None = None, *,
                      final: bool = False) -> dict:
    """Append one checkpoint to checkpoints.json (also used by test stand-in scripts)."""
    job_dir = Path(job_dir)
    file = job_dir / "checkpoints.json"
    items = read_json(file) if file.is_file() else []
    entry = {"id": f"{job_id}:{'final' if final else 'step'}-{int(step)}", "object": "local_lora.checkpoint",
             "job_id": job_id, "step_number": int(step), "path": str(path), "final": final,
             "metrics": metrics or {}, "created_at": now_iso()}
    items.append(entry)
    write_json(file, items)
    return entry


def write_result(job_dir: Path, status: str, **fields) -> None:
    write_json(Path(job_dir) / "result.json", dict(status=status, finished_at=now_iso(), **fields))


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ----------------------------------------------------------------------------------- backend
class LocalLoraBackend:
    """TrainingBackend that runs the trainer as a detached local process per job id."""

    name = "local_lora"

    def __init__(self, *, jobs_root, lora_module_name: str | None = None, trainer_command: list[str] | None = None,
                 lora: dict | None = None, find_spec=importlib.util.find_spec, test_double: bool = False):
        self.jobs_root = Path(jobs_root)
        self.lora_module_name = lora_module_name
        self.trainer_command = list(trainer_command) if trainer_command else None
        self.lora = dict(lora or {})
        self.find_spec = find_spec
        # Any replacement trainer command is a stand-in, never a real training run.
        self.is_test_double = bool(test_double or self.trainer_command)

    @classmethod
    def from_config(cls, config: dict, output_dir) -> "LocalLoraBackend":
        return cls(jobs_root=config.get("jobs_root") or Path(output_dir) / "jobs",
                   lora_module_name=config.get("lora_module_name"), trainer_command=config.get("trainer_command"),
                   lora=config.get("lora"), test_double=config.get("test_double") is True)

    def describe(self) -> dict:
        return {"provider": self.name, "jobs_root": str(self.jobs_root), "test_double": self.is_test_double,
                "trainer": self.trainer_command or [sys.executable, "-m", "auditlab.research.training.local_lora", "run"]}

    def preflight(self, config: dict, *, network: bool = False) -> dict:
        missing, facts = ([], {"skipped": "stand-in trainer (test double)"}) if self.trainer_command else \
            missing_conditions(config, find_spec=self.find_spec, disk_path=self.jobs_root)
        return dict(self.describe(), missing_conditions=missing, facts=facts)

    def upload(self, path) -> str:
        digest = sha256_file(path)
        target = self.jobs_root / "files" / f"{digest[:16]}.jsonl"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        return f"localfile-{digest[:16]}"

    def _file(self, file_id: str | None) -> str | None:
        return str(self.jobs_root / "files" / f"{file_id.removeprefix('localfile-')}.jsonl") if file_id else None

    def create_job(self, *, model: str, training_file: str, validation_file: str | None, method: dict,
                   suffix: str | None, seed: int | None, metadata: dict, idempotency_key: str) -> dict:
        job_id = "lora-" + idempotency_key.removeprefix("al-")
        job_dir = self.jobs_root / job_id
        try:
            job_dir.mkdir(parents=True)  # the directory is the launch lock: one process per job id, ever
        except FileExistsError:
            return self.get_job(job_id)
        run_config = {"job_id": job_id, "job_dir": str(job_dir), "base_model": model,
                      "training_file": self._file(training_file), "validation_file": self._file(validation_file),
                      "hyperparameters": (method.get("supervised") or {}).get("hyperparameters") or {},
                      "lora": method.get("lora") or self.lora, "seed": seed,
                      "lora_module_name": self.lora_module_name or suffix or job_id}
        write_json(job_dir / "run_config.json", run_config)
        command = (self.trainer_command or [sys.executable, "-m", "auditlab.research.training.local_lora", "run"]) + \
            ["--config", str(job_dir / "run_config.json")]
        state = {"job_id": job_id, "pid": None, "started_at": now_iso(), "log_path": str(job_dir / "train.log"),
                 "command": command, "metadata": metadata, "exit_code": None}
        write_json(job_dir / "job_state.json", state)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])))
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        with open(job_dir / "train.log", "ab") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       cwd=str(REPO_ROOT), env=env, **options)
        _PROCESSES[job_id] = process
        write_json(job_dir / "job_state.json", dict(state, pid=process.pid))
        return self.get_job(job_id)

    def find_job(self, metadata_key: str, metadata_value: str) -> dict | None:
        if not self.jobs_root.is_dir():
            return None
        for directory in sorted(self.jobs_root.glob("lora-*")):
            state_file = directory / "job_state.json"
            if state_file.is_file() and (read_json(state_file).get("metadata") or {}).get(metadata_key) == metadata_value:
                return self.get_job(directory.name)
        return None

    def get_job(self, job_id: str) -> dict:
        job_dir = self.jobs_root / job_id
        state_file = job_dir / "job_state.json"
        if not state_file.is_file():
            return {"id": job_id, "status": "failed", "fine_tuned_model": None, "trained_tokens": None,
                    "error": {"code": "launch_interrupted",
                              "message": "job directory without job_state.json; delete it to launch again"}}
        state = read_json(state_file)
        process = _PROCESSES.get(job_id)
        exit_code = state.get("exit_code")
        if process is not None and exit_code is None and process.poll() is not None:
            exit_code = process.returncode
            write_json(state_file, dict(state, exit_code=exit_code))
        result = read_json(job_dir / "result.json") if (job_dir / "result.json").is_file() else None
        run_config = read_json(job_dir / "run_config.json")
        job = {"id": job_id, "object": "local_lora.job", "pid": state.get("pid"), "started_at": state.get("started_at"),
               "log_path": state.get("log_path"), "exit_code": exit_code, "fine_tuned_model": None,
               "trained_tokens": None, "error": None, "result": result}
        if result is not None:
            status = result["status"]
            if status == "succeeded":
                job["fine_tuned_model"] = run_config["lora_module_name"]
            elif status == "not_started_missing_access":
                status = "failed"
                job["error"] = {"code": "missing_access", "message": "; ".join(result.get("missing_conditions") or [])}
            else:
                status = "failed" if status != "cancelled" else status
                job["error"] = result.get("error") or {"code": "failed", "message": "trainer reported failure"}
        elif state.get("pid") is None:
            status = "failed"
            job["error"] = {"code": "launch_interrupted", "message": "no pid recorded; delete the job dir to relaunch"}
        elif exit_code is None and _pid_alive(state.get("pid")):
            status = "running"
        else:
            status = "failed"
            job["error"] = {"code": "process_exited", "message": f"trainer exited (code {exit_code}) without result.json"}
        job["status"] = status
        return job

    def list_events(self, job_id: str) -> list[dict]:
        log = self.jobs_root / job_id / "train.log"
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-50:] if log.is_file() else []
        return [{"object": "local_lora.log_line", "message": line[:500]} for line in lines]

    def list_checkpoints(self, job_id: str) -> list[dict]:
        file = self.jobs_root / job_id / "checkpoints.json"
        return read_json(file) if file.is_file() else []


# ----------------------------------------------------------------------------------- trainer process
def to_trl_row(example: dict) -> dict:
    """OpenAI-format SFT example -> TRL conversational tool-calling row (arguments as objects)."""
    names, messages = {}, []
    for message in example["messages"]:
        message = dict(message)
        if message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                arguments = call["function"]["arguments"]
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    pass  # validated upstream; a raw string is passed through unchanged
                names[call["id"]] = call["function"]["name"]
                calls.append({"id": call["id"], "type": "function",
                              "function": {"name": call["function"]["name"], "arguments": arguments}})
            message["tool_calls"] = calls
        if message.get("role") == "tool":
            message["name"] = names.get(message.get("tool_call_id"), "")
        messages.append(message)
    return {"messages": messages, "tools": example.get("tools") or []}


def _train_with_trl(config: dict) -> dict:
    import torch  # noqa: PLC0415 - GPU machine only
    import transformers  # noqa: PLC0415
    from datasets import Dataset  # noqa: PLC0415
    from peft import LoraConfig  # noqa: PLC0415
    from trl import SFTConfig, SFTTrainer  # noqa: PLC0415

    job_dir, job_id = Path(config["job_dir"]), config["job_id"]
    seed = int(config.get("seed") or 0)
    random.seed(seed)
    torch.manual_seed(seed)
    transformers.set_seed(seed)
    hyper, lora = config.get("hyperparameters") or {}, config.get("lora") or {}

    def dataset(path):
        if not path:
            return None
        rows = [to_trl_row(json.loads(line)) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
        try:
            return Dataset.from_list(rows, on_mixed_types="use_json")
        except TypeError:  # datasets < 4.7: tools as a JSON string (TRL dataset_formats)
            return Dataset.from_list([dict(row, tools=json.dumps(row["tools"])) for row in rows])

    class Recorder(transformers.TrainerCallback):
        last_logs: dict = {}

        def on_log(self, args, state, control, logs=None, **kwargs):
            self.last_logs = dict(logs or {})

        def on_save(self, args, state, control, **kwargs):
            record_checkpoint(job_dir, job_id, state.global_step,
                              Path(args.output_dir) / f"checkpoint-{state.global_step}", self.last_logs)

    args = SFTConfig(output_dir=str(job_dir / "trainer"), num_train_epochs=float(hyper.get("n_epochs", 3)),
                     per_device_train_batch_size=int(hyper.get("batch_size", 1)),
                     gradient_accumulation_steps=int(hyper.get("gradient_accumulation_steps", 8)),
                     learning_rate=float(hyper.get("learning_rate", 1e-4)),
                     max_length=hyper.get("max_length", 16384),
                     assistant_only_loss=bool(hyper.get("assistant_only_loss", True)),
                     save_strategy="steps", save_steps=int(hyper.get("save_steps", 50)),
                     logging_steps=int(hyper.get("logging_steps", 5)), seed=seed, data_seed=seed, report_to="none")
    peft_config = LoraConfig(r=int(lora.get("r", 16)), lora_alpha=int(lora.get("alpha", 32)),
                             lora_dropout=float(lora.get("dropout", 0.05)),
                             target_modules=lora.get("target_modules", "all-linear"), bias="none", task_type="CAUSAL_LM")
    recorder = Recorder()
    trainer = SFTTrainer(model=config["base_model"], args=args, train_dataset=dataset(config["training_file"]),
                         eval_dataset=dataset(config.get("validation_file")), peft_config=peft_config,
                         callbacks=[recorder])
    output = trainer.train()
    adapter = job_dir / "adapter"
    trainer.save_model(str(adapter))
    record_checkpoint(job_dir, job_id, trainer.state.global_step, adapter,
                      {"train_loss": getattr(output, "training_loss", None)}, final=True)
    return {"adapter_path": str(adapter), "global_step": trainer.state.global_step,
            "train_loss": getattr(output, "training_loss", None)}


def run(config_path) -> int:
    """Trainer process entry point; always leaves result.json behind."""
    config = read_json(config_path)
    job_dir = Path(config["job_dir"])
    write_json(job_dir / "runner.json", {"pid": os.getpid(), "started_at": now_iso(), "python": sys.version})
    absent = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if absent:
        write_result(job_dir, "not_started_missing_access", exit_code=MISSING_DEPS_EXIT,
                     missing_conditions=[f"python module {name!r} is not importable" for name in absent])
        return MISSING_DEPS_EXIT
    started = time.monotonic()
    try:
        summary = _train_with_trl(config)
    except Exception as error:  # noqa: BLE001 - recorded as the job's failure reason
        write_result(job_dir, "failed", exit_code=1, error={"code": type(error).__name__, "message": str(error)[:2000]})
        return 1
    write_result(job_dir, "succeeded", exit_code=0, wall_seconds=round(time.monotonic() - started, 1),
                 lora_module_name=config["lora_module_name"], **summary)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditlab.research.training.local_lora")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="train one LoRA job from its run_config.json (GPU machine)")
    run_parser.add_argument("--config", required=True)
    check = sub.add_parser("preflight", help="list missing conditions; starts no process")
    check.add_argument("--base-model", default="")
    check.add_argument("--jobs-root", default=".")
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.config)
    missing, facts = missing_conditions({"base_model": args.base_model, "jobs_root": args.jobs_root})
    print(json.dumps({"missing_conditions": missing, "facts": facts}, indent=2))
    return 0 if not missing else MISSING_DEPS_EXIT


if __name__ == "__main__":
    sys.exit(main())
