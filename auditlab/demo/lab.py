"""Trusted experiment service: builds companies, runs the auditor, scores, exports.

This module may read training truth (fixtures' private/ and runs/demo/trusted/).
It runs as a separate process started by ``python -m auditlab.demo.lab ...``;
the web server only launches it and later reads the finished public export.
Every export is written to a temporary directory and published with one rename;
an existing run directory is never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import engines, standin
from .paths import PACKAGE, REPO, DemoPaths
from .snapshot import PUBLIC_FILES, Snapshot
from .store import OP_KEY_RE, ExportStore, NotFound, Refused

SAMPLE_CREATED = "2026-11-19T12:00:00+05:00"
SAMPLE_NOTE = "Учебный экспорт: открытые разработческие данные, прогон учебной сборки консоли. Не результат конкурсного раннера."
SAMPLES = [
    # run_id, source dir, company, pair_id
    ("TRAIN-S01-reconcile", REPO / "fixtures" / "foundation" / "S01", "Компания S01 · зачёт до границы", "foundation-allocation"),
    ("TRAIN-S02-reconcile", REPO / "fixtures" / "foundation" / "S02", "Компания S02 · зачёт после границы", "foundation-allocation"),
    ("TRAIN-FX01-reconcile", PACKAGE / "scenarios" / "FX01", "Компания FX01 · платёж 100 USD", None),
]
KEY_RE = re.compile(r"^[a-z]+/[A-Za-z0-9_./-]{1,80}$")


class LabError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _atomic_json(path: Path, data) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    _dump(tmp, data)
    os.replace(tmp, path)


def _short(text: str, n: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n].upper()


# ------------------------------------------------------------------ truth store
def _truth_path(paths: DemoPaths, episode_id: str) -> Path:
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", episode_id):
        raise LabError("bad_episode", "Invalid episode id.")
    return paths.trusted / "episodes" / episode_id / "truth.json"


def _save_truth(paths: DemoPaths, truth: dict, provenance: dict) -> None:
    target = _truth_path(paths, str(truth["episode_id"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(target, truth)
    _atomic_json(target.parent / "provenance.json", provenance)


def _load_truth(paths: DemoPaths, episode_id: str) -> dict:
    path = _truth_path(paths, episode_id)
    if not path.exists():
        raise LabError("truth_unavailable", "The trusted side has no training truth for this episode.")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- one experiment
def execute(paths: DemoPaths, public_src: Path, truth: dict, meta: dict, *, policy_name: str = "reconcile",
            truncate_after: int | None = None) -> dict:
    """Run the auditor on a public snapshot and publish a completed export."""
    paths.ensure()
    run_id = meta["run_id"]
    final = paths.exports / run_id
    if final.exists():
        return json.loads((final / "run.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{run_id}-", dir=paths.exports))
    try:
        (tmp / "public").mkdir()
        for name in PUBLIC_FILES:
            if (public_src / name).exists():
                shutil.copyfile(public_src / name, tmp / "public" / name)
        snap = Snapshot(tmp / "public")
        session, session_engine = engines.session(tmp / "public")
        policy, policy_engine = engines.policy(policy_name, 0)
        score_fn, score_engine = engines.scorer()
        started = time.monotonic()
        result = standin.run_episode(session, policy, truth, snap.manifest, score_fn)
        elapsed = round(time.monotonic() - started, 4)
        trajectory = result["trajectory"]
        status = result["status"]
        if truncate_after is not None:  # labelled sample of an interrupted attempt
            trajectory = trajectory[:truncate_after]
            status = "aborted"
        with (tmp / "trajectories.jsonl").open("w", encoding="utf-8") as out:
            for line in trajectory:
                out.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
        if status != "aborted":
            _dump(tmp / "submission.json", result["submission"])
            _dump(tmp / "score.json", result["score"])
        verify, verify_engine = engines.verifier()
        if verify is not None:
            findings = [dict(f) for f in verify(tmp / "public")]
            _dump(tmp / "verifier.json", {"engine": verify_engine, "findings": findings})
        run = {
            "run_id": run_id,
            "episode_id": snap.manifest.get("episode_id"),
            "policy": getattr(policy, "name", policy_name),
            "mode": "train",
            "status": status,
            "label": meta.get("label", "live_experiment"),
            "company": meta.get("company") or f"Компания {snap.manifest.get('episode_id')}",
            "period": {"start": snap.manifest.get("period_start"), "end": snap.manifest.get("period_end")},
            "created_at": meta.get("created_at") or _now(),
            "engine": {"session": session_engine, "policy": policy_engine, "evaluator": score_engine,
                       "generator": meta.get("generator", "fixture"), "verifier": verify_engine or "not_connected"},
            "steps": len(trajectory),
            "inspection_steps": result["inspection_steps"] if status != "aborted" else len(trajectory),
            "reward": result["score"]["reward"] if status != "aborted" else None,
            "findings": len(result["submission"]["findings"]) if status != "aborted" else None,
            "elapsed_s": elapsed if not meta.get("created_at") else None,
        }
        for key in ("pair_id", "parent_run_id", "change", "prediction", "note", "reason", "journal_available",
                    "derived_from", "op_key"):
            if meta.get(key) is not None:
                run[key] = meta[key]
        _dump(tmp / "run.json", run)
        _save_truth(paths, truth, {"run_id": run_id, "source": str(meta.get("generator", "fixture")),
                                   "parent_run_id": meta.get("parent_run_id"), "change": meta.get("change")})
        os.replace(tmp, final)
        return run
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def build_samples(paths: DemoPaths) -> list[str]:
    """Labelled training exports from the open foundation pair and the FX example."""
    made = []
    for run_id, source, company, pair_id in SAMPLES:
        truth = json.loads((source / "private" / "truth.json").read_text(encoding="utf-8"))
        execute(paths, source / "public", truth, {"run_id": run_id, "label": "training_sample", "company": company,
                                                  "pair_id": pair_id, "created_at": SAMPLE_CREATED, "note": SAMPLE_NOTE})
        made.append(run_id)
    s02 = SAMPLES[1][1]
    truth = json.loads((s02 / "private" / "truth.json").read_text(encoding="utf-8"))
    execute(paths, s02 / "public", truth, {
        "run_id": "TRAIN-S02-aborted", "label": "training_sample", "company": "Компания S02 · оборванная попытка",
        "created_at": SAMPLE_CREATED, "note": SAMPLE_NOTE + " Обрыв смоделирован для проверки отображения.",
        "reason": "policy_crash: адаптер политики завершился с кодом 1 после шага 3", "journal_available": True},
        truncate_after=3)
    made.append("TRAIN-S02-aborted")
    return made


# ------------------------------------------------------------------ operations
class Operation:
    """Idempotent operation keyed by the client's operation key."""

    def __init__(self, paths: DemoPaths, op_key: str, kind: str, source_run_id: str):
        if not OP_KEY_RE.match(op_key or ""):
            raise LabError("bad_operation_key", "Operation key must be 6-64 characters [A-Za-z0-9_-].")
        paths.ensure()
        self.path = paths.ops / f"{op_key}.json"
        self.op_key, self.kind, self.source = op_key, kind, source_run_id

    def existing(self) -> dict | None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("status") == "completed":
                return data
        return None

    def write(self, status: str, **extra) -> dict:
        data = {"op_key": self.op_key, "kind": self.kind, "source_run_id": self.source, "status": status,
                "updated_at": _now(), **extra}
        _atomic_json(self.path, data)
        return data


def _store(paths: DemoPaths, extra_roots: list[Path]) -> ExportStore:
    return ExportStore([paths.exports, *extra_roots], paths.ops)


def live_change(paths: DemoPaths, source_run_id: str, change: dict, prediction: str | None, op_key: str,
                extra_roots: list[Path] = ()) -> dict:
    op = Operation(paths, op_key, "change", source_run_id)
    done = op.existing()
    if done:
        return done
    op.write("running")
    try:
        store = _store(paths, list(extra_roots))
        try:
            source_dir = store.run_dir(source_run_id)
        except (NotFound, Refused) as exc:
            raise LabError("unknown_run", f"Run {source_run_id} is not an open completed export.") from exc
        source_meta = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
        h = _short(op_key)
        episode = f"{str(source_meta['episode_id'])[:40]}-L{h[:4]}"
        with tempfile.TemporaryDirectory(dir=paths.root) as scratch:
            built = standin.apply_change(source_dir / "public", change, Path(scratch) / "public", episode)
            pid = source_meta.get("pair_id") or f"PAIR-{source_run_id}"
            run = execute(paths, Path(scratch) / "public", built["truth"], {
                "run_id": f"LIVE-{h}", "label": "live_experiment", "generator": engines.STANDIN,
                "company": f"{source_meta.get('company') or source_meta['episode_id']} · изменён факт",
                "parent_run_id": source_run_id, "pair_id": pid, "op_key": op_key,
                "change": {**change, "moved": built["moved"]},
                "prediction": (prediction or "")[:500] or None})
        return op.write("completed", run_id=run["run_id"])
    except standin.ChangeRejected as exc:
        op.write("failed", error={"code": exc.code, "message": str(exc)})
        raise LabError(exc.code, str(exc)) from exc
    except LabError as exc:
        op.write("failed", error={"code": exc.code, "message": str(exc)})
        raise
    except Exception as exc:  # infrastructure failure, recorded separately from the financial result
        op.write("failed", error={"code": "infrastructure_error", "message": type(exc).__name__})
        raise


def rescore(paths: DemoPaths, source_run_id: str, finding_index: int, replace_from: str, replace_to: str,
            op_key: str, extra_roots: list[Path] = ()) -> dict:
    op = Operation(paths, op_key, "rescore", source_run_id)
    done = op.existing()
    if done:
        return done
    op.write("running")
    try:
        store = _store(paths, list(extra_roots))
        try:
            source_dir = store.run_dir(source_run_id)
        except (NotFound, Refused) as exc:
            raise LabError("unknown_run", f"Run {source_run_id} is not an open completed export.") from exc
        meta = json.loads((source_dir / "run.json").read_text(encoding="utf-8"))
        submission = store.read(source_run_id, "submission.json")
        trajectory = store.read(source_run_id, "trajectories.jsonl") or []
        if not submission:
            raise LabError("no_submission", "The source attempt has no submission to copy.")
        findings = [dict(f) for f in submission.get("findings") or []]
        if not 0 <= finding_index < len(findings):
            raise LabError("bad_finding", "No finding with this index.")
        evidence = list(findings[finding_index].get("evidence_ids") or [])
        if replace_from not in evidence:
            raise LabError("bad_evidence", "The finding does not cite this evidence key.")
        if not KEY_RE.match(replace_to or ""):
            raise LabError("bad_evidence", "Replacement must be a public evidence key such as ledger/R6.")
        findings[finding_index]["evidence_ids"] = [replace_to if e == replace_from else e for e in evidence]
        copy = {**submission, "findings": findings}
        truth = _load_truth(paths, str(meta["episode_id"]))
        score_fn, score_engine = engines.scorer()
        steps = sum(1 for line in trajectory if (line.get("action") or {}).get("action") != "submit")
        new_score = score_fn(copy, truth, steps)
        run_id = f"COPY-{_short(op_key)}"
        final = paths.exports / run_id
        if not final.exists():
            tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{run_id}-", dir=paths.exports))
            try:
                shutil.copytree(source_dir / "public", tmp / "public")
                with (tmp / "trajectories.jsonl").open("w", encoding="utf-8") as out:
                    for line in trajectory:
                        line = dict(line)
                        if (line.get("action") or {}).get("action") == "submit":
                            line["action"] = {"action": "submit", "args": copy}
                            line["reward"] = new_score["reward"]
                        out.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
                _dump(tmp / "submission.json", copy)
                _dump(tmp / "score.json", new_score)
                if (source_dir / "verifier.json").exists():
                    shutil.copyfile(source_dir / "verifier.json", tmp / "verifier.json")
                run = {**{k: meta[k] for k in ("episode_id", "policy", "mode", "company", "period", "pair_id") if k in meta},
                       "run_id": run_id, "status": "completed", "label": "rescore_copy", "created_at": _now(),
                       "engine": {**(meta.get("engine") or {}), "evaluator": score_engine},
                       "steps": len(trajectory), "inspection_steps": steps, "reward": new_score["reward"],
                       "findings": len(findings), "parent_run_id": source_run_id, "op_key": op_key,
                       "derived_from": {"run_id": source_run_id, "finding_index": finding_index,
                                        "replaced": {"from": replace_from, "to": replace_to}}}
                _dump(tmp / "run.json", run)
                os.replace(tmp, final)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        return op.write("completed", run_id=run_id)
    except LabError as exc:
        op.write("failed", error={"code": exc.code, "message": str(exc)})
        raise
    except Exception as exc:
        op.write("failed", error={"code": "infrastructure_error", "message": type(exc).__name__})
        raise


def replay_check(paths: DemoPaths, run_id: str, extra_roots: list[Path] = ()) -> dict:
    """Re-execute recorded actions on the exported snapshot and re-score the recorded submission."""
    store = _store(paths, list(extra_roots))
    try:
        trajectory = store.read(run_id, "trajectories.jsonl") or []
        public = store.public_dir(run_id)
        meta = store.read(run_id, "run.json")
    except (NotFound, Refused) as exc:
        raise LabError("unknown_run", f"Run {run_id} is not an open completed export.") from exc
    session, session_engine = engines.session(public)
    mismatches, checked = [], 0
    for line in trajectory:
        action = line.get("action") or {}
        if action.get("action") == "submit":
            continue
        checked += 1
        again = dict(session.inspect(action))
        recorded = {k: v for k, v in (line.get("observation") or {}).items() if k != "remaining_steps"}
        if again != recorded:
            mismatches.append({"step": line.get("step")})
    out = {"run_id": run_id, "steps_checked": checked, "mismatches": mismatches, "session": session_engine,
           "observations_match": not mismatches}
    submission, recorded_score = store.read(run_id, "submission.json"), store.read(run_id, "score.json")
    if submission is not None and recorded_score is not None:
        try:
            truth = _load_truth(paths, str(meta["episode_id"]))
            score_fn, score_engine = engines.scorer()
            again = score_fn(submission, truth, recorded_score.get("inspection_steps", checked))
            keys = ("tp", "fp", "fn", "reward")
            out.update({"evaluator": score_engine, "score_again": {k: again.get(k) for k in keys},
                        "score_match": all(again.get(k) == recorded_score.get(k) for k in keys)})
        except LabError as exc:
            out.update({"score_match": None, "score_note": str(exc)})
    return out


# -------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditlab.demo.lab", description="Trusted AuditLab demo lab")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--exports", type=Path, action="append", default=[], help="extra read-only export root")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("samples")
    ch = sub.add_parser("change")
    ch.add_argument("--source", required=True)
    ch.add_argument("--target", required=True)
    ch.add_argument("--field", default="timestamp")
    ch.add_argument("--value", required=True)
    ch.add_argument("--prediction", default=None)
    ch.add_argument("--op", required=True)
    rs = sub.add_parser("rescore")
    rs.add_argument("--source", required=True)
    rs.add_argument("--finding", type=int, default=0)
    rs.add_argument("--replace", required=True)
    rs.add_argument("--with", dest="replacement", required=True)
    rs.add_argument("--op", required=True)
    rp = sub.add_parser("replay-check")
    rp.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    paths = (DemoPaths(args.root.resolve()) if args.root else DemoPaths.default()).ensure()
    try:
        if args.cmd == "samples":
            result: object = {"built": build_samples(paths)}
        elif args.cmd == "change":
            result = live_change(paths, args.source, {"target": args.target, "field": args.field, "value": args.value},
                                 args.prediction, args.op, args.exports)
        elif args.cmd == "rescore":
            result = rescore(paths, args.source, args.finding, args.replace, args.replacement, args.op, args.exports)
        else:
            result = replay_check(paths, args.run, args.exports)
    except LabError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
