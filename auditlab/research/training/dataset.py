"""Supervised fine-tuning dataset from teacher attempts, plus equal-budget subsampling.

Input: one or more experiment output ROOTs written by auditlab/research/agents/execute.py:
    ROOT/attempts/<attempt_id>/attempt.json, prompt.json, transcript.jsonl, steps.jsonl, provider_calls.jsonl
    ROOT/trusted/<attempt_id>/evaluation.json        (official {..., return} or null; official_by_rule)
Partial (`<id>.partial`) attempt directories are ignored.

Training data format, verified 2026-11-21:
  * https://platform.openai.com/docs/guides/supervised-fine-tuning (301 to
    https://developers.openai.com/api/docs/guides/supervised-fine-tuning): one JSON object per line
    with `messages`; assistant `tool_calls` [{id, type "function", function {name, arguments: JSON
    string}}]; top-level `tools` (function definitions) and `parallel_tool_calls: false`; at least
    10 examples per file. The page shows only single-turn user/assistant examples. It also states that
    self-serve fine-tuning is being wound down (see openai_backend.py).
  * https://developers.openai.com/api/reference/resources/files: files up to 512 MB, `.jsonl` only.
NOT verified on the current pages (long-standing chat format, relied on): multi-turn examples with
`tool` role messages (tool_call_id, content) answering earlier assistant tool calls, and the
per-example token limit; DEFAULT_MAX_EXAMPLE_TOKENS below is a configurable assumption.
The same JSONL feeds local_lora.py, which converts it to TRL's conversational tool-calling rows.

Selection never reads truth: it uses the attempt's own record and the trusted evaluation summary
(official return), and the training split guard refuses test/final/holdout outright. Examples are
rebuilt from the neutral transcript's visible text and tool calls only, never from provider-native
content, so hidden reasoning (e.g. thinking blocks) can never enter the dataset.

Teacher-source guard (licensing/causality bookkeeping, not a legal decision): every selected attempt
is labelled oracle_demonstration, baseline_policy, self or third_party_model. By default only
oracle_demonstration and self are allowed; third_party_model (e.g. Claude transcripts used to train a
non-Anthropic model) is refused unless `teacher_terms_acknowledged` records who confirmed and when.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from auditlab.research.agents.openai_chat import rebuild_assistant, tool_definitions, tool_messages

SCHEMA_VERSION = "1.0"
FORBIDDEN_SPLITS = frozenset({"test", "final", "holdout"})
DEFAULT_SPLITS = ("train",)
DEFAULT_MAX_EXAMPLE_TOKENS = 65_536   # assumption, not on the current docs page
MAX_FILE_BYTES = 512 * 1024 * 1024    # Files API limit
MIN_EXAMPLES = 10                     # documented minimum per training file
TOKEN_ESTIMATE_METHOD = "estimate: len(json chars) / 3, not a tokenizer count"
DOC_URLS = ("https://developers.openai.com/api/docs/guides/supervised-fine-tuning",
            "https://developers.openai.com/api/reference/resources/files")
ATTEMPT_FILES = ("attempt.json", "prompt.json", "transcript.jsonl", "steps.jsonl", "provider_calls.jsonl")
TEACHER_SOURCES = ("oracle_demonstration", "baseline_policy", "self", "third_party_model")
DEFAULT_TEACHER_SOURCES = ("oracle_demonstration", "self")


class DatasetError(ValueError):
    pass


# ----------------------------------------------------------------------------------- small helpers
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, data: object) -> None:
    """Atomic write (temp file + replace) so a crash never leaves a half-written artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_json(path: Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_source_sha() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                                cwd=Path(__file__).resolve().parent)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and len(sha) == 40 else None


def estimate_tokens(example: dict) -> int:
    return len(json.dumps(example, ensure_ascii=False)) // 3 + 1


def check_allowed_splits(splits) -> tuple[str, ...]:
    splits = tuple(splits)
    bad = sorted(set(splits) & FORBIDDEN_SPLITS)
    if bad:
        raise DatasetError(f"training splits may never include {bad}; allowed splits are training/search splits only")
    if not splits:
        raise DatasetError("no training splits allowed")
    return splits


def acknowledged(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


# ----------------------------------------------------------------------------------- teacher sources
def check_teacher_policy(allowed_sources, teacher_terms_acknowledged) -> tuple[str, ...]:
    """Refuse an unknown source, and third_party_model without a recorded acknowledgement."""
    allowed = tuple(allowed_sources)
    unknown = sorted(set(allowed) - set(TEACHER_SOURCES))
    if unknown:
        raise DatasetError(f"unknown teacher sources {unknown}; known: {list(TEACHER_SOURCES)}")
    if "third_party_model" in allowed and not acknowledged(teacher_terms_acknowledged):
        raise DatasetError('teacher source "third_party_model" requires "teacher_terms_acknowledged": '
                           '"<who confirmed, date>"')
    return allowed


def teacher_source(attempt: dict, self_models) -> str | None:
    """oracle_demonstration | baseline_policy | self | third_party_model, or None when unknown.

    "self": the teacher model is listed in `self_models` (the model being trained or its family, as the
    caller lists them) or is a fine-tune of one ("ft:<model>:...")."""
    agent = attempt.get("agent") if isinstance(attempt.get("agent"), dict) else {}
    if agent.get("kind") in ("oracle_demonstration", "baseline_policy"):
        return agent["kind"]
    provider = agent.get("provider") if isinstance(agent.get("provider"), dict) else {}
    model = provider.get("model")
    if not model:
        return None
    model = str(model)
    if any(model == m or model.startswith(f"ft:{m}:") for m in self_models):
        return "self"
    return "third_party_model"


# ----------------------------------------------------------------------------------- curriculum
def load_curriculum(path) -> dict:
    """Curriculum summary: only episode refs, splits, kind and a content hash (never truth)."""
    path = Path(path)
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("episodes"), list):
        raise DatasetError(f"curriculum {path} has no 'episodes' list")
    episodes = {}
    for item in data["episodes"]:
        if not isinstance(item, dict) or not item.get("episode_ref"):
            raise DatasetError("curriculum episode without episode_ref")
        episodes[str(item["episode_ref"])] = {"split": item.get("split"), "family_id": item.get("family_id")}
    return {"ref": str(path), "sha256": sha256_file(path), "kind": data.get("kind"),
            "artifact_id": data.get("artifact_id"), "schema_version": data.get("schema_version"),
            "episodes": episodes}


def curriculum_problem(episode_ref: str, split: str, curriculum: dict, allowed) -> str | None:
    entry = curriculum["episodes"].get(str(episode_ref))
    if entry is None:
        return f"episode {episode_ref!r} is not in the curriculum"
    if entry["split"] != split:
        return f"split mismatch: attempt {split!r} vs curriculum {entry['split']!r}"
    if entry["split"] not in allowed:
        return f"curriculum split {entry['split']!r} not allowed"
    return None


# ----------------------------------------------------------------------------------- attempts
def find_attempt_dirs(roots) -> list[tuple[Path, Path]]:
    """(attempt_dir, trusted evaluation path) for every finalized attempt under the given ROOTs."""
    found = []
    for root in roots:
        root = Path(root).resolve()
        if not (root / "attempts").is_dir():
            raise DatasetError(f"{root} is not an experiment output root (no attempts/ directory)")
        for directory in sorted((root / "attempts").iterdir()):
            if directory.is_dir() and not directory.name.endswith(".partial") and (directory / "attempt.json").is_file():
                found.append((directory, root / "trusted" / directory.name / "evaluation.json"))
    return found


def load_attempt(directory: Path, evaluation_path: Path) -> dict:
    directory = Path(directory)
    try:
        attempt = read_json(directory / "attempt.json")
        prompt = read_json(directory / "prompt.json")
        transcript = [json.loads(line) for line in
                      (directory / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as error:
        raise DatasetError("unreadable attempt (attempt.json, prompt.json and transcript.jsonl are required): "
                           f"{type(error).__name__}") from None
    if not isinstance(attempt, dict) or attempt.get("attempt_id") != directory.name:
        raise DatasetError("attempt.json attempt_id does not match its directory name")
    try:
        evaluation = read_json(evaluation_path) if Path(evaluation_path).is_file() else None
    except (OSError, ValueError):
        evaluation = None
    if isinstance(evaluation, dict) and evaluation.get("attempt_id") not in (None, directory.name):
        evaluation = None  # belongs to another attempt; never used
    files = {name: sha256_file(directory / name) for name in ATTEMPT_FILES if (directory / name).is_file()}
    if evaluation is not None:
        files["trusted/evaluation.json"] = sha256_file(evaluation_path)
    return {"dir": directory, "attempt": attempt, "prompt": prompt, "transcript": transcript,
            "evaluation": evaluation, "evaluation_path": Path(evaluation_path), "files": files}


def _return_of(evaluation) -> float | None:
    official = evaluation.get("official") if isinstance(evaluation, dict) else None
    value = official.get("return") if isinstance(official, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def rejection_reason(record: dict, allowed: tuple[str, ...], min_return: float, curriculum: dict | None,
                     sources: tuple[str, ...], self_models) -> str | None:
    attempt = record["attempt"]
    episode = attempt.get("episode") or {}
    agent = attempt.get("agent") if isinstance(attempt.get("agent"), dict) else {}
    if attempt.get("test_double") is not False or agent.get("test_double") is not False:
        return "test double attempt (or test_double not explicitly false)"
    if attempt.get("status") != "completed":
        return f"status {attempt.get('status')!r} is not completed"
    split = episode.get("split")
    if split in FORBIDDEN_SPLITS:
        return f"forbidden split {split!r}"
    if split not in allowed:
        return f"split {split!r} not in allowed training splits"
    source = teacher_source(attempt, self_models)
    record["teacher_source"] = source
    if source is None:
        return "unknown teacher source (no agent.kind and no provider model)"
    if source not in sources:
        return f"teacher source {source!r} not allowed (allowed: {list(sources)})"
    value = _return_of(record["evaluation"])
    if value is None:
        return "no trusted official return"
    if value < min_return:
        return f"return {value} below threshold {min_return}"
    if curriculum is not None:
        problem = curriculum_problem(str(episode.get("episode_ref")), split, curriculum, allowed)
        if problem:
            return problem
    if not isinstance(record["prompt"].get("tools"), list) or not isinstance(record["prompt"].get("system"), str):
        return "prompt.json lacks system/tools"
    return None


# ----------------------------------------------------------------------------------- examples
def transcript_to_example(prompt: dict, transcript: list[dict], *, system_role: str = "system") -> dict:
    """One fine-tuning example: the full multi-turn trajectory, ending on the last assistant turn."""
    messages = [{"role": system_role, "content": prompt["system"]}] if prompt.get("system") else []
    for entry in transcript:
        role = entry.get("role")
        if role == "user":
            messages.append({"role": "user", "content": str(entry.get("text") or "")})
        elif role == "assistant":
            text = entry.get("text") or ""
            message = rebuild_assistant(text if text.strip() else "", entry.get("tool_calls") or [])
            if message.get("content") is None:
                message.pop("content")
            if "content" in message or "tool_calls" in message:
                messages.append(message)
        elif role == "tool_results":
            messages.extend(tool_messages(entry))
        else:
            raise DatasetError(f"unknown transcript role {role!r}")
    while messages and messages[-1]["role"] != "assistant":
        messages.pop()  # trailing tool results/user text after the final action carry no training target
    return {"messages": messages, "tools": tool_definitions(prompt.get("tools") or []), "parallel_tool_calls": False}


def validate_example(example: dict, max_example_tokens: int = DEFAULT_MAX_EXAMPLE_TOKENS) -> list[str]:
    """Structural problems; an empty list means valid. Unanswered tool calls are allowed only in the
    final assistant message (the episode's last action, e.g. submit)."""
    if not isinstance(example, dict):
        return ["example is not an object"]
    problems = []
    extra = set(example) - {"messages", "tools", "parallel_tool_calls"}
    if extra:
        problems.append(f"unexpected keys {sorted(extra)}")
    tools = example.get("tools")
    names = set()
    if not isinstance(tools, list) or not all(isinstance(t, dict) and t.get("type") == "function"
                                              and isinstance(t.get("function", {}).get("name"), str) for t in tools):
        problems.append("tools must be a list of function tools")
    else:
        names = {t["function"]["name"] for t in tools}
    messages = example.get("messages")
    if not isinstance(messages, list) or not messages:
        return problems + ["messages must be a non-empty list"]
    pending: set[str] = set()
    seen_ids: set[str] = set()
    seen_user = False
    for index, message in enumerate(messages):
        role = message.get("role") if isinstance(message, dict) else None
        where = f"message {index} ({role})"
        if role in ("system", "developer"):
            if index != 0:
                problems.append(f"{where}: system message only allowed first")
            if not isinstance(message.get("content"), str):
                problems.append(f"{where}: content must be a string")
        elif role == "user":
            if pending:
                problems.append(f"{where}: tool calls {sorted(pending)} not answered")
            seen_user = True
            if not isinstance(message.get("content"), str):
                problems.append(f"{where}: content must be a string")
        elif role == "assistant":
            if not seen_user:
                problems.append(f"{where}: assistant before any user message")
            if pending:
                problems.append(f"{where}: tool calls {sorted(pending)} not answered")
            content, calls = message.get("content"), message.get("tool_calls")
            if not (isinstance(content, str) and content.strip()) and not calls:
                problems.append(f"{where}: empty assistant message")
            if content is not None and not isinstance(content, str):
                problems.append(f"{where}: content must be a string")
            for call in calls or []:
                function = call.get("function") if isinstance(call, dict) else None
                if (not isinstance(function, dict) or call.get("type") != "function"
                        or not isinstance(call.get("id"), str) or not isinstance(function.get("arguments"), str)):
                    problems.append(f"{where}: malformed tool call")
                    continue
                if function.get("name") not in names:
                    problems.append(f"{where}: tool {function.get('name')!r} not in tools")
                if call["id"] in seen_ids:
                    problems.append(f"{where}: duplicate tool call id {call['id']!r}")
                seen_ids.add(call["id"])
                pending.add(call["id"])
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                problems.append(f"{where}: answers unknown or already answered call {call_id!r}")
            pending.discard(call_id)
            if not isinstance(message.get("content"), str):
                problems.append(f"{where}: content must be a string")
        else:
            problems.append(f"{where}: unknown role")
    if messages[-1].get("role") != "assistant":
        problems.append("last message must be an assistant message")
    if not any(isinstance(m, dict) and m.get("role") == "assistant" for m in messages):
        problems.append("no assistant message to train on")
    tokens = estimate_tokens(example)
    if tokens > max_example_tokens:
        problems.append(f"estimated {tokens} tokens exceeds per-example limit {max_example_tokens} (estimate)")
    return problems


def _example_line(example: dict) -> str:
    return json.dumps(example, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_dataset(output_dir: Path, lines: list[str]) -> tuple[Path, str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "dataset.jsonl"
    data = "".join(line + "\n" for line in lines).encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise DatasetError(f"dataset is {len(data)} bytes, above the {MAX_FILE_BYTES} byte file limit")
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)
    return path, hashlib.sha256(data).hexdigest(), len(data)


def build_sft(roots, output_dir, *, allowed_splits=DEFAULT_SPLITS, min_return: float = 0.0, curriculum_ref=None,
              system_role: str = "system", max_example_tokens: int = DEFAULT_MAX_EXAMPLE_TOKENS,
              allowed_sources=DEFAULT_TEACHER_SOURCES, self_models=(), teacher_terms_acknowledged: str | None = None) -> dict:
    """Select teacher attempts from experiment ROOTs, convert, validate, write dataset.jsonl + manifest."""
    allowed = check_allowed_splits(allowed_splits)
    sources = check_teacher_policy(allowed_sources, teacher_terms_acknowledged)
    self_models = tuple(str(m) for m in self_models)
    curriculum = load_curriculum(curriculum_ref) if curriculum_ref else None
    output_dir = Path(output_dir)
    selected, rejected = [], []
    for directory, evaluation_path in find_attempt_dirs(roots):
        try:
            record = load_attempt(directory, evaluation_path)
        except DatasetError as error:
            rejected.append({"attempt_dir": str(directory), "attempt_id": directory.name, "reason": str(error)})
            continue
        reason = rejection_reason(record, allowed, min_return, curriculum, sources, self_models)
        if reason is None:
            example = transcript_to_example(record["prompt"], record["transcript"], system_role=system_role)
            problems = validate_example(example, max_example_tokens)
            if problems:
                reason = "invalid example: " + "; ".join(problems[:5])
            else:
                record["example"] = example
                selected.append(record)
                continue
        rejected.append({"attempt_dir": str(directory), "attempt_id": directory.name, "reason": reason})
    selected.sort(key=lambda r: (str(r["attempt"]["episode"].get("episode_ref")), str(r["attempt"]["attempt_id"])))
    lines = [_example_line(r["example"]) for r in selected]
    path, digest, size = _write_dataset(output_dir, lines)
    entries, teachers, protocols = [], set(), set()
    for index, record in enumerate(selected):
        attempt, episode = record["attempt"], record["attempt"]["episode"]
        provider = (attempt.get("agent") or {}).get("provider") or {}
        teachers.add((record["teacher_source"], str(provider.get("provider") or attempt["agent"].get("kind")),
                      str(provider.get("model") or "")))
        protocols.add(attempt.get("protocol_id"))
        example = record["example"]
        entries.append({"index": index, "attempt_dir": str(record["dir"]), "attempt_id": attempt["attempt_id"],
                        "artifact_id": attempt.get("artifact_id", attempt["attempt_id"]),
                        "evaluation_ref": str(record["evaluation_path"]), "protocol_id": attempt.get("protocol_id"),
                        "episode_ref": episode.get("episode_ref"), "episode_sha256": episode.get("episode_sha256"),
                        "family_id": episode.get("family_id"), "split": episode.get("split"),
                        "return": _return_of(record["evaluation"]), "teacher_source": record["teacher_source"],
                        "prompt_hash": record["prompt"].get("prompt_hash"), "files_sha256": record["files"],
                        "token_estimate": estimate_tokens(example),
                        "assistant_messages": sum(m["role"] == "assistant" for m in example["messages"]),
                        "tool_calls": sum(len(m.get("tool_calls") or []) for m in example["messages"])})
    observed = sorted({str(s["split"]) for s in entries})
    manifest = {
        "schema_version": SCHEMA_VERSION, "artifact_id": f"sft-{digest[:16]}-{uuid.uuid4().hex[:8]}",
        "source_sha": git_source_sha(), "protocol_id": next(iter(protocols)) if len(protocols) == 1 else None,
        "protocol_ids": sorted(str(p) for p in protocols),
        "kind": "weights", "created_at": now_iso(), "format": "openai_chat_sft_jsonl_with_tools",
        "format_docs": list(DOC_URLS), "roots": [str(Path(r).resolve()) for r in roots],
        "selection_rule": {"status": "completed", "exclude_test_double": True, "allowed_splits": list(allowed),
                           "min_return": min_return, "metric": "ROOT/trusted/<attempt_id>/evaluation.json official.return",
                           "curriculum": curriculum and {k: curriculum[k] for k in ("ref", "sha256", "kind", "artifact_id")},
                           "system_role": system_role, "max_example_tokens": max_example_tokens},
        "teacher_policy": {"allowed_sources": list(sources), "self_models": list(self_models),
                           "teacher_terms_acknowledged": teacher_terms_acknowledged},
        "teacher_sources": dict(sorted(Counter(e["teacher_source"] for e in entries).items())),
        "teachers": [{"source": s, "provider": p, "model": m or None} for s, p, m in sorted(teachers)],
        "source_attempts": entries, "rejected": rejected,
        "counts": {"examples": len(entries), "rejected": len(rejected),
                   "assistant_messages": sum(s["assistant_messages"] for s in entries),
                   "tool_calls": sum(s["tool_calls"] for s in entries),
                   "meets_minimum_examples": len(entries) >= MIN_EXAMPLES, "minimum_examples": MIN_EXAMPLES},
        "token_estimate": {"method": TOKEN_ESTIMATE_METHOD, "total": sum(s["token_estimate"] for s in entries),
                           "max_example": max((s["token_estimate"] for s in entries), default=0)},
        "split_check": {"allowed": list(allowed), "forbidden": sorted(FORBIDDEN_SPLITS), "observed": observed,
                        "passed": not (set(observed) & FORBIDDEN_SPLITS) and set(observed) <= set(allowed)},
        "dataset": {"path": "dataset.jsonl", "sha256": digest, "bytes": size},
        "sampling": None,
    }
    write_json(output_dir / "dataset_manifest.json", manifest)
    return manifest


# ----------------------------------------------------------------------------------- equal budgets
def load_dataset(dataset_dir) -> tuple[dict, list[str]]:
    """(manifest, lines) after checking the JSONL against the manifest hash."""
    dataset_dir = Path(dataset_dir)
    manifest = read_json(dataset_dir / "dataset_manifest.json")
    path = dataset_dir / manifest["dataset"]["path"]
    if sha256_file(path) != manifest["dataset"]["sha256"]:
        raise DatasetError(f"{path} does not match the sha256 in its manifest")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != len(manifest["source_attempts"]):
        raise DatasetError("dataset line count differs from manifest source_attempts")
    return manifest, lines


def sample_budget(dataset_dir, output_dir, *, seed: int, max_examples: int | None = None,
                  max_tokens: int | None = None) -> dict:
    """Seeded subsample to at most `max_examples` and at most `max_tokens` estimated tokens.

    Examples are visited in a seeded random order and kept while they fit; kept examples retain their
    original order. Everything cut is recorded with the reason.
    """
    if max_examples is None and max_tokens is None:
        raise DatasetError("sample needs max_examples and/or max_tokens")
    parent, lines = load_dataset(dataset_dir)
    sources = parent["source_attempts"]
    order = list(range(len(lines)))
    random.Random(seed).shuffle(order)
    kept, cut, tokens = [], [], 0
    for index in order:
        cost = sources[index]["token_estimate"]
        if max_examples is not None and len(kept) >= max_examples:
            cut.append((index, "example budget reached"))
        elif max_tokens is not None and tokens + cost > max_tokens:
            cut.append((index, "token budget would be exceeded"))
        else:
            kept.append(index)
            tokens += cost
    kept.sort()
    output_dir = Path(output_dir)
    path, digest, size = _write_dataset(output_dir, [lines[i] for i in kept])
    new_sources = [dict(sources[i], index=n, parent_index=i) for n, i in enumerate(kept)]
    manifest = dict(parent)
    manifest.update({
        "artifact_id": f"sft-{digest[:16]}-{uuid.uuid4().hex[:8]}", "created_at": now_iso(),
        "source_attempts": new_sources,
        "teacher_sources": dict(sorted(Counter(s["teacher_source"] for s in new_sources).items())),
        "counts": dict(parent["counts"], examples=len(kept),
                       assistant_messages=sum(s["assistant_messages"] for s in new_sources),
                       tool_calls=sum(s["tool_calls"] for s in new_sources),
                       meets_minimum_examples=len(kept) >= MIN_EXAMPLES),
        "token_estimate": dict(parent["token_estimate"], total=tokens,
                               max_example=max((s["token_estimate"] for s in new_sources), default=0)),
        "dataset": {"path": "dataset.jsonl", "sha256": digest, "bytes": size},
        "sampling": {"parent_artifact_id": parent["artifact_id"], "parent_sha256": parent["dataset"]["sha256"],
                     "parent_ref": str(Path(dataset_dir)), "seed": seed, "max_examples": max_examples,
                     "max_tokens": max_tokens, "kept": len(kept),
                     "cut": [{"parent_index": i, "attempt_id": sources[i]["attempt_id"],
                              "episode_ref": sources[i]["episode_ref"], "token_estimate": sources[i]["token_estimate"],
                              "reason": reason} for i, reason in sorted(cut)]},
    })
    write_json(output_dir / "dataset_manifest.json", manifest)
    return manifest


def equal_budget(dataset_dirs, output_dirs, *, seed: int) -> list[dict]:
    """Subsample several arms (e.g. random vs adaptive curriculum) to a common example and token cap:
    the smallest arm's count and estimated tokens."""
    if len(dataset_dirs) != len(output_dirs) or len(dataset_dirs) < 2:
        raise DatasetError("equal_budget needs at least two arms and one output dir per arm")
    manifests = [load_dataset(d)[0] for d in dataset_dirs]
    max_examples = min(m["counts"]["examples"] for m in manifests)
    max_tokens = min(m["token_estimate"]["total"] for m in manifests)
    return [sample_budget(d, o, seed=seed, max_examples=max_examples, max_tokens=max_tokens)
            for d, o in zip(dataset_dirs, output_dirs)]
