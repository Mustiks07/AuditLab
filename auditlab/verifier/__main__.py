"""python -m auditlab.verifier PUBLIC_DIR [--engine duckdb|sqlite] [--out report.json]"""
import argparse
import json
from pathlib import Path
import sys

from .core import verify_report
from .engine import ENGINES


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditlab.verifier",
                                     description="Independent SQL verification of a public/ snapshot.")
    parser.add_argument("public_dir", type=Path)
    parser.add_argument("--engine", choices=ENGINES)
    parser.add_argument("--out", type=Path, help="write the JSON report here (must not exist)")
    args = parser.parse_args(argv)
    report = verify_report(args.public_dir, args.engine)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        if args.out.exists():
            parser.error(f"{args.out} already exists; reports are never overwritten")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.out.with_suffix(args.out.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(args.out)
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
