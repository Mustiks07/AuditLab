"""Reject preloaded project images under any published Docker identity."""
import argparse
import json
from pathlib import Path
import subprocess


def check(images, inspect=None):
    if inspect is None:
        inspect = lambda identity: subprocess.run(
            ["docker", "image", "inspect", identity], capture_output=True).returncode == 0
    checked = {role: {value: bool(inspect(value)) for value in dict.fromkeys(
        image[key] for key in ("id", "oci_manifest_digest", "tag") if image.get(key))}
        for role, image in images.items()}
    present = {role: any(values.values()) for role, values in checked.items()}
    return {"images_present_before_load": present, "checked_identities": checked,
            "status": "failed" if not checked or any(present.values()) else "passed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # A broken daemon must not look like an empty daemon.
    subprocess.run(["docker", "info"], check=True, capture_output=True)
    result = check(json.loads(args.manifest.read_text())["images"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return int(result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
