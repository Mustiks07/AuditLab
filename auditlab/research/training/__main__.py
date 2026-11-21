"""CLI: python -m auditlab.research.training {build-sft, sample, equalize, train, status}."""
from __future__ import annotations

import argparse
import json
import sys

from auditlab.research.training.dataset import (DEFAULT_MAX_EXAMPLE_TOKENS, DEFAULT_TEACHER_SOURCES, TEACHER_SOURCES,
                                                DatasetError, build_sft, equal_budget, sample_budget)
from auditlab.research.training.train import TrainingError, status, train


def _summary(manifest: dict) -> dict:
    return {"artifact_id": manifest["artifact_id"], "examples": manifest["counts"]["examples"],
            "rejected": manifest["counts"].get("rejected"), "token_estimate": manifest["token_estimate"]["total"],
            "dataset_sha256": manifest["dataset"]["sha256"], "split_check": manifest["split_check"]["passed"],
            "teacher_sources": manifest.get("teacher_sources")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditlab.research.training")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-sft", help="teacher attempts -> dataset.jsonl + dataset_manifest.json")
    build.add_argument("--roots", nargs="+", required=True, help="experiment output ROOTs (attempts/, trusted/)")
    build.add_argument("--out", required=True)
    build.add_argument("--splits", nargs="+", default=["train"])
    build.add_argument("--min-return", type=float, default=0.0)
    build.add_argument("--curriculum")
    build.add_argument("--system-role", default="system", choices=["system", "developer"])
    build.add_argument("--max-example-tokens", type=int, default=DEFAULT_MAX_EXAMPLE_TOKENS)
    build.add_argument("--teacher-sources", nargs="+", default=list(DEFAULT_TEACHER_SOURCES),
                       choices=list(TEACHER_SOURCES))
    build.add_argument("--self-models", nargs="*", default=[], help="model ids that count as 'self' (the student)")
    build.add_argument("--teacher-terms-acknowledged", help='"<who confirmed, date>"; required for third_party_model')

    sample = sub.add_parser("sample", help="seeded subsample to an example and/or token budget")
    sample.add_argument("--dataset", required=True)
    sample.add_argument("--out", required=True)
    sample.add_argument("--seed", type=int, required=True)
    sample.add_argument("--max-examples", type=int)
    sample.add_argument("--max-tokens", type=int)

    equalize = sub.add_parser("equalize", help="subsample several arms to the smallest arm's budget")
    equalize.add_argument("--datasets", nargs="+", required=True)
    equalize.add_argument("--outs", nargs="+", required=True)
    equalize.add_argument("--seed", type=int, required=True)

    run = sub.add_parser("train", help="create or resume one fine-tuning job")
    run.add_argument("--config", required=True)
    run.add_argument("--curriculum", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-wait", type=float, default=0.0, help="seconds to keep polling (0: poll once)")

    show = sub.add_parser("status", help="print the stored training record (no network)")
    show.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "build-sft":
            result = _summary(build_sft(args.roots, args.out, allowed_splits=args.splits, min_return=args.min_return,
                                        curriculum_ref=args.curriculum, system_role=args.system_role,
                                        max_example_tokens=args.max_example_tokens, allowed_sources=args.teacher_sources,
                                        self_models=args.self_models,
                                        teacher_terms_acknowledged=args.teacher_terms_acknowledged))
        elif args.command == "sample":
            result = _summary(sample_budget(args.dataset, args.out, seed=args.seed, max_examples=args.max_examples,
                                            max_tokens=args.max_tokens))
        elif args.command == "equalize":
            result = [_summary(m) for m in equal_budget(args.datasets, args.outs, seed=args.seed)]
        elif args.command == "train":
            path = train(args.config, args.curriculum, args.out, dry_run=True if args.dry_run else None,
                         max_wait_s=args.max_wait)
            record = json.loads(path.read_text(encoding="utf-8"))
            result = {"training_ref": str(path), "status": record["status"], "job_id": record["job"]["job_id"],
                      "budget": record["budget"], "missing_conditions": record.get("missing_conditions"),
                      "planned_requests": record.get("planned_requests"),
                      "result_model": record["result_model"], "checkpoint_id": record["checkpoint_id"],
                      "mock": record["mock"], "provider": record["provider"]}
        else:
            result = status(args.out)
    except (DatasetError, TrainingError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
