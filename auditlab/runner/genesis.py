"""Genesis FinTech judge adapter (https://genesis-exe.com/fintech-checks.html, published 2026-11-20).

A thin, trusted translation layer between the organizers' harness and the AuditLab runner and
evaluator. It never chooses actions: every policy action is translated 1:1, every observation is
translated back, and the step budget, deadline and grading stay in AuditEnvironment and the
evaluator. Docs: docs/GENESIS_ADAPTER.md.

Official wire                                     AuditLab runner
----------------------------------------------    -----------------------------------------------
reset(seed, {"mode", "episode_dir", "team_config"}) reset(seed, {"snapshot_dir": episode_dir, ...})
{"name": N, "args": A}                             {"action": N, "args": A}
{"name": "submit", "args": {"findings": F}}        {"action": "submit", "args": F}
first observation {task, manifest, policy,         internal first observation minus "tools"
  remaining_steps}
{"result", "error", "remaining_steps"}             {"ok", "result" | "error", "remaining_steps"}
submit -> {"accepted": true}                       {"ok": true, "result": {"status": "submitted"}}
score_episode -> {tp, fp, fn, precision, recall,   evaluator Score (+ "reward"); invalid submit:
  f1, return}                                       return=-1, every other metric null

Trust boundary: generate_episode, score_episode, episode_dir, seed, config and truth stay with the
caller (the judge). Observations and info carry only public data; errors carry public codes only.
"""
from __future__ import annotations

import json
import logging
import os
import time
from hashlib import sha256
from pathlib import Path
from typing import Callable

from auditlab.contracts import MAX_STEPS
from auditlab.evaluator.schema import RULES_GENESIS
from auditlab.runner.env import TOOLS, AuditEnvironment
from auditlab.runner.wire import json_snapshot, rejected_marker

PROTOCOL = "genesis-fintech-checks-2026-11-20"
ADAPTER_VERSION = "1.1"
# Generator profile for a team_config that names no config_version (adapter 1.0 fell back to the
# generator's historical default 1.0: 28 ledger rows, one page, the same structure for every seed).
# An explicit config_version, including "1.0", is passed through unchanged.
DEFAULT_PROFILE = "1.3"
TIME_LIMIT_S = 60.0
ACTIONS = (*TOOLS, "submit")
OFFICIAL_TABLES = ("ledger", "invoices", "payments", "fx")  # plus the documented extra table "policy"
CONFIG_KEYS = ("mode", "episode_dir", "team_config")
MALFORMED = "$malformed_envelope"  # internal name of an envelope that is not {"name": str, "args": ...}
METRICS = ("tp", "fp", "fn", "precision", "recall", "f1")
TASK = ("Audit the company's records for the reporting period in the manifest, then call submit once. "
        "Send each action as {\"name\": <action>, \"args\": {...}}. Actions: "
        "list_records {table, cursor, limit<=50} (first cursor null), get_record {table, id}, "
        "get_document {doc_id}, calculator {expression}, submit {findings: {episode_id, findings: [...]}}. "
        "Tables: ledger, invoices, payments, fx, policy. At most 100 actions including submit.")

log = logging.getLogger("auditlab.judge_adapter")


class AdapterError(ValueError):
    """Judge-side misuse of the adapter (bad config or directory). Never shown to a policy."""


def _trusted_log(event: dict) -> None:
    """The evaluator journal: the `auditlab.judge_adapter` logger, plus AUDITLAB_JUDGE_LOG (JSONL) if set."""
    line = json.dumps({"protocol": PROTOCOL, **event}, ensure_ascii=False, sort_keys=True, default=str)
    log.info(line)
    path = os.environ.get("AUDITLAB_JUDGE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")


# ------------------------------------------------------------------------------------- generate
def generator_config(team: dict) -> dict:
    """The adapter's generation profile: fill config_version only when the caller left it out."""
    if "config_version" in team:
        return dict(team)
    return {**team, "config_version": DEFAULT_PROFILE}


def _redacted(team: object) -> object:
    """Trusted journal copy of a team_config: the episode key itself is never written."""
    if not isinstance(team, dict) or "episode_key" not in team:
        return team
    key = team["episode_key"]
    marker = {"redacted": True}
    if isinstance(key, str):
        marker["sha256_prefix"] = sha256(key.encode("utf-8")).hexdigest()[:16]
    return {**team, "episode_key": marker}


def _team_config(config: object) -> dict:
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise AdapterError("config must be an object")
    if "team_config" in config:
        if set(config) - set(CONFIG_KEYS):
            raise AdapterError("config with team_config may only contain mode, episode_dir, team_config")
        team = config["team_config"]
        if team is None:
            return {}
        if not isinstance(team, dict):
            raise AdapterError("team_config must be an object")
        return dict(team)
    return dict(config)


def generate_episode(seed: int, config: dict, output_dir: str) -> None:
    """Wrap the AuditLab generator: write <output_dir>/public/ and <output_dir>/private/.

    `config` is the AuditLab generator configuration (docs/GENESIS_ADAPTER.md), either directly
    or as {"team_config": {...}}. output_dir may be new or an existing EMPTY directory; an existing
    episode is never overwritten. Same seed and config give the same files.
    """
    from auditlab.generator import generate_episode as generate

    team = generator_config(_team_config(config))
    output = Path(output_dir)
    keyed = "episode_key" in team
    _trusted_log({"event": "generate_episode", "output_dir": str(output_dir), "config_version": team["config_version"],
                  "randomness": "keyed" if keyed else "unkeyed",
                  **({} if keyed else {"note": "without episode_key a small seed space with a guessable config "
                                               "is recoverable by enumeration (docs/GENESIS_ADAPTER.md)"})})
    if not output.exists():
        generate(seed, team, output)
        return
    if not output.is_dir() or any(output.iterdir()):
        raise AdapterError("output_dir exists and is not an empty directory; nothing was overwritten")
    staging = output / ".auditlab-staging"
    generate(seed, team, staging)
    for part in ("public", "private"):
        (staging / part).rename(output / part)
    staging.rmdir()


# ------------------------------------------------------------------------------------- scoring
def official_result(graded: dict | None) -> dict | None:
    """Evaluator reply -> official score_episode dict. Invalid submit: return=-1, metrics null."""
    if graded is None:
        return None
    if graded["status"] == "invalid_submission":
        return {**{key: None for key in METRICS}, "return": -1.0}
    score = graded["score"]
    return {**{key: score[key] for key in METRICS}, "return": score["reward"]}


def _episode_paths(episode_dir: str | os.PathLike) -> tuple[Path, Path]:
    episode = Path(episode_dir)
    public, private = episode / "public", episode / "private"
    if not (public / "manifest.json").is_file() or not (private / "truth.json").is_file():
        raise AdapterError("episode_dir must contain public/manifest.json and private/truth.json "
                           "(the layout written by generate_episode)")
    return public, private


def score_episode(episode_dir: str, findings: dict, inspection_steps: int) -> dict:
    """Closed evaluator call under the Genesis rules; the policy never calls it.

    `findings` is the findings.json object {episode_id, findings: [...]}. Returns
    {tp, fp, fn, precision, recall, f1, return}; an invalid submit returns return=-1 with null
    metrics, and the structural reason goes to the evaluator journal only.
    """
    from auditlab.evaluator.service import handle

    public, private = _episode_paths(episode_dir)
    graded = handle({"private_dir": str(private), "public_dir": str(public), "submission": findings,
                     "inspection_steps": inspection_steps, "rules": RULES_GENESIS})
    result = official_result(graded)
    _trusted_log({"event": "score_episode", "episode_dir": str(episode_dir), "inspection_steps": inspection_steps,
                  "status": graded["status"], "reason": graded["reason"], "matches": graded["matches"],
                  "unmatched": graded["unmatched"], "result": result})
    return result


# ------------------------------------------------------------------------------------- environment
def _translate(action: object) -> tuple[dict, bool]:
    """Official envelope -> internal action, 1:1. Returns (internal action, envelope_was_malformed).

    Every step call is an action of the budget (fintech-checks: "все предыдущие вызовы step, в том
    числе ошибочные"). So a broken envelope or a value JSON cannot carry becomes an internal action
    that the runner counts and answers with a bounded error, instead of an uncounted transport error.
    """
    if not (type(action) is dict and type(action.get("name")) is str and set(action) <= {"name", "args"}):
        return {"action": MALFORMED, "args": {}}, True
    name = action["name"]
    _, problem = json_snapshot(action)
    if problem == "not_json":
        args: object = rejected_marker("not_json")
    elif "args" in action:
        args = action["args"]
    else:
        args = None
    if name == "submit":
        # The official submit body is {"findings": <findings.json object>}; anything else is a
        # recognised submit with a broken structure, graded as invalid (return -1), never dropped.
        if type(args) is dict and set(args) == {"findings"}:
            return {"action": "submit", "args": args["findings"]}, False
        return {"action": "submit", "args": rejected_marker("submit_args")}, False
    return ({"action": name, "args": args} if args is not None else {"action": name}), False


class GenesisEnvironment:
    """make_env() object: reset(seed, config) -> (observation, info); step(action) -> 5-tuple."""

    def __init__(self, *, evaluator: object = "subprocess", clock: Callable[[], float] = time.monotonic,
                 generator=None, session_opener=None, trusted_dir: str | Path | None = None,
                 time_limit_s: float = TIME_LIMIT_S):
        self._options = {"evaluator": evaluator, "clock": clock, "generator": generator,
                         "session_opener": session_opener, "trusted_dir": trusted_dir}
        self._clock = clock
        self._time_limit = time_limit_s
        self._env: AuditEnvironment | None = None
        self._remaining = MAX_STEPS
        self.mode: str | None = None

    # -------------------------------------------------------------- reset
    def reset(self, seed: int, config: dict) -> tuple[dict, dict]:
        started = self._clock()  # the 60 s count starts at entry into reset, before any loading
        if not isinstance(config, dict):
            raise AdapterError("config must be an object")
        unknown = sorted(set(config) - set(CONFIG_KEYS))
        mode = config.get("mode", "closed")
        if mode not in ("train", "closed"):
            raise AdapterError("mode must be 'train' or 'closed'")
        team = config.get("team_config") or {}
        if not isinstance(team, dict):
            raise AdapterError("team_config must be an object")
        internal: dict = {"time_limit_s": self._time_limit, "started_at": started, "policy_name": "genesis-judge"}
        episode_dir = config.get("episode_dir")
        if episode_dir is not None:
            public, _ = _episode_paths(episode_dir)
            internal["snapshot_dir"] = str(public.parent)  # loaded as is: no regeneration, no new IDs
        else:
            clash = sorted(set(team) & {"snapshot_dir", "export_dir", "policy_name", "run_id", "time_limit_s",
                                        "started_at"})
            if clash:
                raise AdapterError(f"team_config may not set runner keys: {clash}")
            internal.update(generator_config(team))
        if self._env is None or self._env.mode != mode:
            if self._env is not None:
                self._env.close()
            self._env = AuditEnvironment(mode, rules=RULES_GENESIS,
                                         **{key: value for key, value in self._options.items() if value is not None})
        self.mode = mode
        observation, info = self._env.reset(seed, internal)  # a repeated reset starts the episode again
        self._remaining = observation["remaining_steps"]
        _trusted_log({"event": "reset", "mode": mode, "seed": seed, "episode_dir": episode_dir,
                      "team_config": _redacted(team) if episode_dir is None else None,
                      "ignored_config_keys": unknown,
                      "episode_id": info["episode_id"], "versions": self.versions()})
        first = {"task": TASK, "manifest": observation["manifest"], "policy": observation["policy"],
                 "remaining_steps": observation["remaining_steps"]}
        return first, {"episode_id": info["episode_id"], "max_steps": MAX_STEPS, "mode": mode}

    # -------------------------------------------------------------- step
    def step(self, action: object) -> tuple[dict, float, bool, bool, dict]:
        if self._env is None:
            observation = {"result": None, "error": {"code": "no_active_episode", "message": "Call reset first."},
                           "remaining_steps": 0}
            return observation, 0.0, False, False, {"protocol_error": "no_active_episode"}
        internal, malformed = _translate(action)
        observation, reward, terminated, truncated, info = self._env.step(internal)
        return self._observation(observation, internal["action"], malformed), reward, terminated, truncated, info

    def _observation(self, internal: dict, name: str, malformed: bool) -> dict:
        if "remaining_steps" in internal:
            self._remaining = internal["remaining_steps"]
        if internal.get("ok") is True:
            result = {"accepted": True} if name == "submit" else internal.get("result")
            return {"result": result, "error": None, "remaining_steps": self._remaining}
        error = internal.get("error") or {"code": "internal_error", "message": "Internal error."}
        if malformed and error.get("code") == "unknown_action":
            error = {"code": "malformed_action", "message": "Expected {\"name\": string, \"args\": object}."}
        return {"result": None, "error": dict(error), "remaining_steps": self._remaining}

    # -------------------------------------------------------------- trusted helpers (judge side only)
    def expire(self) -> tuple[dict, float, bool, bool, dict]:
        """The judge stops a policy that ran out of time: truncated, empty findings."""
        if self._env is None:
            raise AdapterError("no episode")
        observation, reward, terminated, truncated, info = self._env.expire()
        return self._observation(observation, "", False), reward, terminated, truncated, info

    def abort(self, reason: str) -> tuple[dict, float, bool, bool, dict]:
        """The judge's driver ends an episode whose policy crashed or broke its channel.

        Recorded as policy_failure (or as a timeout once the deadline has passed), never as an
        auditor's financial answer. Used by research controllers for provider/bridge failures.
        """
        if self._env is None:
            raise AdapterError("no episode")
        observation, reward, terminated, truncated, info = self._env.abort(str(reason)[:200])
        return self._observation(observation, "", False), reward, terminated, truncated, info

    def seconds_left(self) -> float:
        return self._env.seconds_left() if self._env is not None else 0.0

    def trusted_result(self) -> dict | None:
        """After the episode: status, inspection_steps and the official score (never shown to a policy)."""
        if self._env is None or self._env.final is None:
            return None
        final = self._env.final
        return {"status": final["status"], "terminated": final["terminated"], "truncated": final["truncated"],
                "inspection_steps": final["inspection_steps"], "score": official_result(final["graded"]),
                "evaluation": final["graded"], "infra": final.get("infra", [])}

    @staticmethod
    def versions() -> dict:
        from auditlab.runner.env import VERSIONS
        return {**VERSIONS, "adapter": ADAPTER_VERSION, "protocol": PROTOCOL, "rules": RULES_GENESIS,
                "default_generator_profile": DEFAULT_PROFILE}

    def close(self) -> None:
        if self._env is not None:
            self._env.close()


def make_env(**options) -> GenesisEnvironment:
    """Official entry point. Keyword options are for AuditLab's own tests (evaluator, clock, ...)."""
    return GenesisEnvironment(**options)


__all__ = ["AdapterError", "GenesisEnvironment", "generate_episode", "make_env", "official_result", "score_episode"]
