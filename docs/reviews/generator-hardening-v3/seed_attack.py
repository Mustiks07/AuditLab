"""Seed-recovery attack on self-generated episodes, and the same attack against profile 1.3.

Threat model: the attacker has the open AuditLab source and one episode's public/ directory
(what a policy can read through the tools), but no private/, seed, config or key. The attack is:

1. enumerate a seed space and recompute the first public invoice_id each seed would draw
   (profiles 1.0-1.2: from the seed alone; profile 1.3: from seed+config, so every guessed config
   is a separate sweep);
2. regenerate the episode for each hit and each guessed config; a byte-identical public/ confirms
   the guess and the regenerated private/truth.json is the answer.

Run (pinned tzdata, e.g. in the runtime image), from the repository root:
    python docs/reviews/generator-hardening-v3/seed_attack.py --space 1000000 --out attack.json
Victim seeds are drawn from the space by a fixed RNG; nothing here touches any closed holdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import judge_adapter  # noqa: E402
from auditlab.generator import profile13  # noqa: E402

# What an attacker would guess: the documented typical configurations (docs/GENESIS_ADAPTER.md).
DOCUMENTED = [
    {},
    {"issue_counts": {"duplicate_invoice": 1}},
    {"issue_counts": {"payment_allocation": 1}},
    {"currencies": ["KZT", "USD"], "issue_counts": {"fx_conversion": 1}},
    {"issue_counts": {"period_cutoff": 1}},
    {"currencies": ["KZT", "USD"], "interaction": "allocation_fx_same_payment",
     "issue_counts": {"payment_allocation": 1, "fx_conversion": 1}},
    {"currencies": ["KZT", "USD"], "interaction": "cutoff_fx_same_invoice",
     "issue_counts": {"period_cutoff": 1, "fx_conversion": 1}},
]
PROFILES = ("1.0", "1.1", "1.2", "1.3")
VICTIM_KEY = "6a1f0c3e9b8d47e2a5c4f1d0b9e8a7c6"  # a test key for a self-made episode, not a secret


def guesses():
    """(profile, config) pairs the attacker tries; the adapter default adds config_version 1.3."""
    for profile in PROFILES:
        for config in DOCUMENTED:
            yield profile, {**config, "config_version": profile}


def victims(space: int) -> list[dict]:
    rng = random.Random(20261122)
    seed = lambda: rng.randrange(space)  # noqa: E731
    return [
        {"name": "adapter-1.0-empty", "seed": seed(), "config": {"config_version": "1.0"}},
        {"name": "profile-1.1-allocation", "seed": seed(),
         "config": {"config_version": "1.1", "issue_counts": {"payment_allocation": 1}}},
        {"name": "profile-1.2-fx", "seed": seed(),
         "config": {"config_version": "1.2", "currencies": ["KZT", "USD"], "issue_counts": {"fx_conversion": 1}}},
        {"name": "adapter-1.3-empty-unkeyed", "seed": seed(), "config": {}},
        {"name": "profile-1.3-unlisted-config-unkeyed", "seed": seed(),
         "config": {"issue_counts": {"payment_allocation": 2}, "company_size": {"ledger_rows_target": 777,
                                                                                "documents_target": 66}}},
        {"name": "adapter-1.3-empty-keyed", "seed": seed(), "config": {"episode_key": VICTIM_KEY}},
    ]


def _first_invoice_legacy(seed: int) -> str:
    key = hashlib.sha256(f"AuditLab/v1/ids/invoice/{seed}".encode("ascii")).digest()
    return f"I{random.Random(key).getrandbits(96):024X}"


def _first_invoice_13(seed: int, config: dict) -> str:
    _, ids = profile13.materials(seed, config)
    key = hashlib.sha256(f"AuditLab/v1/ids/invoice/{ids}".encode("ascii")).digest()
    return f"I{random.Random(key).getrandbits(96):024X}"


def public_invoice_ids(public: Path) -> set[str]:
    lines = (public / "invoices.jsonl").read_text(encoding="utf-8").splitlines()
    return {json.loads(line)["invoice_id"] for line in lines}


def _tree(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(file.relative_to(path).as_posix().encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


def attack(public: Path, space: int, workdir: Path) -> dict:
    """Recover (seed, config) and the answer from public/ alone, or report that nothing matched."""
    observed = public_invoice_ids(public)
    target = _tree(public)
    started = time.perf_counter()
    hits = [seed for seed in range(space) if _first_invoice_legacy(seed) in observed]
    candidates = [(seed, profile, config) for seed in hits for profile, config in guesses() if profile != "1.3"]
    sweeps = {"seed_only": time.perf_counter() - started}
    for profile, config in guesses():
        if profile != "1.3":
            continue
        sweep = time.perf_counter()
        candidates += [(seed, profile, config) for seed in range(space)
                       if _first_invoice_13(seed, config) in observed]
        sweeps[json.dumps(config, sort_keys=True)] = time.perf_counter() - sweep
    search_s = time.perf_counter() - started
    confirm = time.perf_counter()
    for index, (seed, profile, config) in enumerate(candidates):
        trial = workdir / f"trial-{index}"
        try:
            judge_adapter.generate_episode(seed, config, str(trial))
        except ValueError:
            continue
        if _tree(trial / "public") == target:
            truth = json.loads((trial / "private" / "truth.json").read_text(encoding="utf-8"))
            return {"recovered": True, "seed": seed, "recovered_config": config, "truth_issues": len(truth["issues"]),
                    "truth_sha256": hashlib.sha256((trial / "private" / "truth.json").read_bytes()).hexdigest(),
                    "seed_hits": len(candidates), "search_s": round(search_s, 2),
                    "confirm_s": round(time.perf_counter() - confirm, 2), "sweeps_s": _rounded(sweeps)}
    return {"recovered": False, "seed_hits": len(candidates), "search_s": round(search_s, 2),
            "confirm_s": round(time.perf_counter() - confirm, 2), "sweeps_s": _rounded(sweeps)}


def _rounded(values: dict) -> dict:
    return {key: round(value, 2) for key, value in values.items()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--space", type=int, default=1_000_000, help="seed space size (six digits: 10^6)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for victim in victims(args.space):
            episode = tmp / victim["name"]
            judge_adapter.generate_episode(victim["seed"], victim["config"], str(episode))
            truth_sha = hashlib.sha256((episode / "private" / "truth.json").read_bytes()).hexdigest()
            result = attack(episode / "public", args.space, tmp / f"{victim['name']}-attack")
            if result["recovered"]:
                result["seed_correct"] = result["seed"] == victim["seed"]
                result["truth_identical"] = result.pop("truth_sha256") == truth_sha
            config = {key: ("<test key>" if key == "episode_key" else value) for key, value in victim["config"].items()}
            rows.append({"victim": victim["name"], "config": config, **result})
            print(json.dumps(rows[-1], sort_keys=True))
    report = {"space": args.space, "documented_guesses": len(DOCUMENTED) * len(PROFILES),
              "python": sys.version.split()[0], "machine": platform.machine(), "processor": platform.processor(),
              "single_process": True, "results": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
