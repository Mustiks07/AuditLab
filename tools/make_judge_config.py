"""Create an explicit, private, reproducible configuration for closed generation.

Run only on the trusted judge host. Keep this file out of Git, public observations
and policy mounts. Reuse it for replay; do not silently rotate the key on reset.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets


def create(path: Path) -> None:
    # Exclusive creation preserves an existing key and refuses a symlink at the target.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        json.dump({"config_version": "1.3", "episode_key": secrets.token_hex(32)}, stream, indent=2)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        create(args.output)
    except OSError as error:
        print(f"Private config not created: {type(error).__name__}; existing files are never overwritten.")
        return 1
    print("Private generator configuration created. Keep it on the trusted side and reuse it for replay.")
    if os.name == "nt":
        print("Windows: restrict the file ACL to the judge account before closed generation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
