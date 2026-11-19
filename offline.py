"""AuditLab bundle launcher: Python 3.11 and a local Linux Docker daemon, no downloads."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

FILES = ("images.tar", "source.tar.gz", "licenses.json", "offline.py")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_bundle(bundle):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format_version") != "1.0":
        raise ValueError("unsupported bundle manifest version")
    if set(manifest.get("files", {})) != set(FILES):
        raise ValueError("manifest must describe all four bundle payload files")
    for name in FILES:
        path = bundle / name
        expected = manifest["files"][name]
        if not isinstance(expected, dict):
            raise ValueError(f"invalid file record: {name}")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing or symlinked bundle file: {name}")
        if path.stat().st_size != expected.get("size_bytes") or sha256(path) != expected.get("sha256"):
            raise ValueError(f"checksum or size mismatch: {name}; no images were loaded")
    if set(manifest.get("images", {})) != {"runtime", "verifier", "policy"}:
        raise ValueError("manifest must name runtime, verifier and policy images")
    for role, image in manifest["images"].items():
        if not isinstance(image, dict) or not IMAGE_ID.fullmatch(image.get("id", "")) or image.get("os") != "linux":
            raise ValueError(f"invalid Linux image identity: {role}")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_revision", "")):
        raise ValueError("invalid source revision")
    return manifest


def select_cpus(explicit=None):
    available = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else list(range(os.cpu_count() or 1))
    if explicit:
        chosen = set()
        for part in explicit.split(","):
            if not re.fullmatch(r"\d+(?:-\d+)?", part):
                raise ValueError("--cpu-set requires two CPU indices, e.g. 0,1 or 0-1")
            values = [int(n) for n in part.split("-")]
            start, end = (values[0], values[-1])
            if end < start or end - start > 1:
                raise ValueError("exactly two distinct CPUs are required")
            chosen.update(range(start, end + 1))
        if len(chosen) != 2 or not chosen <= set(available):
            raise ValueError(f"choose exactly two available CPUs from {available}")
        return ",".join(str(cpu) for cpu in sorted(chosen))
    if len(available) < 2:
        raise ValueError("two available CPUs are required")
    return ",".join(str(cpu) for cpu in available[:2])


def new_output_dir(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = root / f"verify-{stamp}-{uuid.uuid4().hex[:12]}"
    output.mkdir(exist_ok=False)
    return output


def runtime_command(manifest, output, cpu_set, suite):
    images = {role: item["id"] for role, item in manifest["images"].items()}
    command = ["docker", "run", "--rm", "--pull", "never", "--network", "none",
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--cpus", "2", "--cpuset-cpus", cpu_set, "--memory", "2g", "--memory-swap", "2g", "--pids-limit", "256",
               "--tmpfs", "/tmp:rw,nosuid,size=1g",
               "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
               "--mount", f"type=bind,src={Path(output).resolve()},dst=/results",
               "--env", f"AUDITLAB_POLICY_IMAGE={images['policy']}",
               "--env", f"AUDITLAB_VERIFIER_IMAGE={images['verifier']}",
               "--env", f"AUDITLAB_CPUSET={cpu_set}",
               images["runtime"], "python", "tools/container_verify.py",
               "--output", "/results/controller-resources.json", "--",
               "python", "run.py", "verify", "--suite", suite,
               "--output", "/results/verify"]
    if suite == "rehearsal":
        command += ["--policy-image", images["policy"], "--verifier-image", images["verifier"],
                    "--cpu-set", cpu_set]
    return command


def write_report(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "check"))
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--suite", choices=("quick", "rehearsal"), default="quick")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--cpu-set", help="exactly two available CPUs; default: first two allowed CPUs")
    args = parser.parse_args(argv)
    report, report_path = None, None
    started = time.monotonic()
    try:
        if sys.version_info[:2] != (3, 11):
            raise ValueError("launcher requires Python 3.11")
        # Complete every payload check before the first Docker command, especially docker load.
        manifest = verify_bundle(args.bundle)
        print(f"Bundle hashes verified; source {manifest['source_revision']}", flush=True)
        if args.command == "check":
            return 0
        cpu_set = select_cpus(args.cpu_set)
        output = new_output_dir(args.output_root or args.bundle / "results")
        report_path = output / "launch.json"
        report = {"status": "in_progress", "phase": "docker_preflight", "source_revision": manifest["source_revision"],
                  "images": manifest["images"], "cpu_set": cpu_set, "controller_memory_limit": "2g",
                  "network": "none", "suite": args.suite, "started_at": datetime.now(timezone.utc).isoformat()}
        write_report(report_path, report)
        environment = {**os.environ, "DOCKER_HOST": "unix:///var/run/docker.sock"}
        environment.pop("DOCKER_CONTEXT", None)
        info = subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", "info", "--format", "{{json .}}"],
                              check=True, text=True, capture_output=True, env=environment)
        daemon = json.loads(info.stdout)
        if daemon.get("OSType") != "linux":
            raise ValueError("a local Linux Docker daemon is required")
        # Ignore Docker contexts/DOCKER_HOST: this launcher deliberately uses the local socket.
        report.update(phase="loading_images", docker_server_version=daemon.get("ServerVersion"))
        write_report(report_path, report)
        load_started = time.monotonic()
        subprocess.run(["docker", "load", "--input", str(args.bundle.resolve() / "images.tar")], check=True, env=environment)
        report["image_load_seconds"] = time.monotonic() - load_started
        for role, image in manifest["images"].items():
            inspected = subprocess.run(["docker", "image", "inspect", image["id"], "--format", "{{json .}}"],
                                       check=True, capture_output=True, text=True, env=environment)
            actual = json.loads(inspected.stdout)
            if actual.get("Id") != image["id"] or actual.get("Os") != "linux":
                raise ValueError(f"loaded image identity differs for {role}")
        command = runtime_command(manifest, output, cpu_set, args.suite)
        report.update(phase="verification", command=command)
        write_report(report_path, report)
        print(f"Running offline verification on CPUs {cpu_set}; results: {output}", flush=True)
        verification_started = time.monotonic()
        result = subprocess.run(command, env=environment)
        report["verification_container_seconds"] = time.monotonic() - verification_started
        report.update(status="completed" if result.returncode == 0 else "incomplete" if result.returncode == 2 else "failed",
                      exit_code=result.returncode)
        return result.returncode
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Offline launch failed: {error}", file=sys.stderr)
        if report is not None:
            report.update(status="failed", exit_code=1, error=str(error))
        return 1
    except KeyboardInterrupt:
        if report is not None:
            report.update(status="interrupted", exit_code=130)
        return 130
    finally:
        if report is not None and report_path is not None:
            report["elapsed_seconds"] = time.monotonic() - started
            write_report(report_path, report)


if __name__ == "__main__":
    sys.exit(main())
