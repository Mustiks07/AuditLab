"""AuditLab bundle launcher: Python 3.11 and a local Linux Docker daemon, no downloads."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import time
import uuid

FILES = ("images.tar", "source.tar.gz", "licenses.json", "offline.py")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINERD_NAME = "io.containerd.image.name"
MAX_CATALOG_BYTES = 4 * 1024 * 1024


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
        if "oci_manifest_digest" in image and (not isinstance(image["oci_manifest_digest"], str)
                                               or not IMAGE_ID.fullmatch(image["oci_manifest_digest"])):
            raise ValueError(f"invalid OCI manifest identity: {role}")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_revision", "")):
        raise ValueError("invalid source revision")
    return manifest


def archive_image_name(descriptor):
    """The tag the containerd image store gives an OCI index entry; Docker Hub prefixes are implicit."""
    name = (descriptor.get("annotations") or {}).get(CONTAINERD_NAME)
    if not isinstance(name, str):
        return None
    for prefix in ("docker.io/library/", "docker.io/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def validate_image_archive(path, images):
    """Every image must survive docker load on both Docker image stores.

    The classic store reads manifest.json (Config + RepoTags). The containerd store
    reads only the OCI index.json and names images from its annotations. Saving
    anonymous IDs (v0.1.0-rc.1) kept three legacy entries but one untagged OCI
    descriptor, so containerd silently imported one image of three. Returns
    {config ID: OCI manifest digest} for the checked archive.
    """
    with tarfile.open(path) as archive:
        members = {member.name: member for member in archive.getmembers() if member.isfile()}

        def read(name):
            if name not in members or members[name].size > MAX_CATALOG_BYTES:
                raise ValueError(f"saved image archive lacks a valid {name}")
            return archive.extractfile(members[name]).read()

        catalog = json.loads(read("manifest.json"))
        if not isinstance(catalog, list):
            raise ValueError("invalid saved Docker catalog")
        for role, image in images.items():
            matches = [row for row in catalog if isinstance(row, dict) and image.get("tag")
                       and image["tag"] in (row.get("RepoTags") or [])]
            if len(matches) != 1 or Path(str(matches[0].get("Config"))).stem != image["id"].split(":", 1)[1]:
                raise ValueError(f"saved Docker catalog lost the tagged {role} identity")
        if "index.json" not in members:
            if any(image.get("oci_manifest_digest") for image in images.values()):
                raise ValueError("saved image archive lost the OCI index recorded in the bundle manifest")
            return {}  # Docker before 25 writes no OCI index; the containerd store needs one.
        index = json.loads(read("index.json"))
        tags = {image["tag"] for image in images.values()}
        entries = []
        descriptors = index.get("manifests") if isinstance(index, dict) else None
        if not isinstance(descriptors, list):
            raise ValueError("invalid saved OCI index")
        for descriptor in descriptors:
            digest = descriptor.get("digest") if isinstance(descriptor, dict) else None
            if not isinstance(digest, str) or not IMAGE_ID.fullmatch(digest):
                raise ValueError("invalid saved OCI manifest digest")
            data = read("blobs/sha256/" + digest.split(":", 1)[1])
            if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("saved OCI manifest checksum differs")
            config = (json.loads(data).get("config") or {}).get("digest")
            if not isinstance(config, str):
                raise ValueError("saved OCI index entry is not a single-platform image manifest")
            name = archive_image_name(descriptor)
            if name not in tags:
                raise ValueError(f"saved OCI index entry {digest} is not tagged with a bundle image name")
            entries.append((config, name, digest))
        if {config for config, _, _ in entries} != {image["id"] for image in images.values()}:
            raise ValueError("saved OCI index does not cover all three image configurations")
        identities = {}
        for role, image in images.items():
            matches = [digest for config, name, digest in entries if (config, name) == (image["id"], image["tag"])]
            if len(matches) != 1:
                raise ValueError(f"saved OCI index does not reference the {role} image with its tag")
            if image.get("oci_manifest_digest", matches[0]) != matches[0]:
                raise ValueError(f"saved OCI manifest for {role} differs from the bundle manifest")
            identities[image["id"]] = matches[0]
        return identities


def local_docker_environment():
    environment = {**os.environ, "DOCKER_HOST": "unix:///var/run/docker.sock"}
    environment.pop("DOCKER_CONTEXT", None)
    return environment


def loaded_image(image, environment):
    """Classic storage addresses configs; containerd addresses OCI manifests.

    Both immutable identities originate in the checked archive. Never select a
    mutable tag at runtime or accept an unrelated image that happens to exist.
    """
    identities = [image["id"]]
    if image.get("oci_manifest_digest") and image["oci_manifest_digest"] not in identities:
        identities.append(image["oci_manifest_digest"])
    for identity in identities:
        inspected = subprocess.run(["docker", "image", "inspect", identity, "--format", "{{json .}}"],
                                   capture_output=True, text=True, env=environment)
        if inspected.returncode:
            continue
        actual = json.loads(inspected.stdout)
        # A store may resolve the config ID to its manifest-addressed image; either
        # returned identity must still be one recorded in the checked bundle.
        if actual.get("Id") not in identities or actual.get("Os") != "linux" or (
                image.get("architecture") and actual.get("Architecture") != image["architecture"]):
            raise ValueError("loaded image identity or platform differs from the bundle")
        return {**image, "source_id": image["id"], "id": actual["Id"]}
    raise ValueError(f"bundle image is not loaded: {image['id']}; run offline.py verify first")


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


def runtime_command(manifest, output, cpu_set, suite, docker_gid=None, launch_id=None):
    images = {role: item["id"] for role, item in manifest["images"].items()}
    command = ["docker", "run", "--rm", "--pull", "never", "--network", "none",
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", f"{os.getuid()}:{os.getgid()}",
               "--group-add", str(os.getgid() if docker_gid is None else docker_gid),
               "--cpus", "2", "--cpuset-cpus", cpu_set, "--memory", "1536m", "--memory-swap", "1536m", "--pids-limit", "256",
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
    if launch_id is not None:
        if not re.fullmatch(r"[0-9a-f]{32}", launch_id):
            raise ValueError("invalid launch identity")
        command[2:2] = ["--name", "auditlab-controller-" + launch_id,
                        "--label", "org.auditlab.launch=" + launch_id,
                        "--env", "AUDITLAB_LAUNCH_ID=" + launch_id]
    if suite == "rehearsal":
        command += ["--policy-image", images["policy"], "--verifier-image", images["verifier"],
                    "--cpu-set", cpu_set]
    return command


def resource_preflight(daemon):
    fields = ("MemoryLimit", "SwapLimit", "CpuCfsPeriod", "CpuCfsQuota", "CPUSet", "PidsLimit")
    missing = [name for name in fields if daemon.get(name) is not True]
    if daemon.get("CgroupDriver") in (None, "", "none"):
        missing.append("CgroupDriver")
    if daemon.get("CgroupVersion") != "2":
        missing.append("CgroupVersion2")
    return {"status": "not_checked" if missing else "available",
            "reason": "daemon_resource_controls_unavailable" if missing else "actual_container_limits_still_require_verification",
            "missing": missing, "driver": daemon.get("CgroupDriver"), "version": daemon.get("CgroupVersion"),
            "capabilities": {name: daemon.get(name) for name in fields}}


def write_report(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class LaunchInterrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


def cleanup_launch(launch_id, environment):
    """Remove this launch only, controller first so it cannot start more work.

    Siblings have the same random label; it is never forwarded to a policy.
    Recheck after a quiet interval to include daemon-side creates already in flight.
    An unavailable daemon is a failed cleanup, never evidence of an empty launch.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", launch_id):
        raise ValueError("invalid launch identity")
    controller = "auditlab-controller-" + launch_id
    subprocess.run(["docker", "rm", "--force", controller], capture_output=True,
                   env=environment, timeout=10)
    deadline = time.monotonic() + 10
    quiet_since = None
    removed = set()
    while time.monotonic() < deadline:
        found = subprocess.run(["docker", "ps", "--all", "--filter",
                                "label=org.auditlab.launch=" + launch_id, "--format", "{{.ID}}"],
                               capture_output=True, text=True, env=environment, timeout=5, check=True)
        identities = found.stdout.split()
        if any(not re.fullmatch(r"[0-9a-f]{12,64}", identity) for identity in identities):
            raise ValueError("invalid container identity in cleanup response")
        if identities:
            quiet_since = None
            subprocess.run(["docker", "rm", "--force", *identities], capture_output=True,
                           env=environment, timeout=10)
            removed.update(identities)
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= 1:
            return {"status": "passed", "remaining_containers": 0,
                    "removed_siblings": sorted(removed)}
        time.sleep(.1)
    raise ValueError("launch containers remain or continue appearing after cleanup")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "check", "image"))
    parser.add_argument("--role", choices=("runtime", "verifier", "policy"), default="runtime",
                        help="image command: print the verified local immutable ID for this role")
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--suite", choices=("quick", "rehearsal"), default="quick")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--cpu-set", help="exactly two available CPUs; default: first two allowed CPUs")
    args = parser.parse_args(argv)
    report, report_path = None, None
    started = time.monotonic()
    launch_id, environment = None, None
    deferred_signal = None
    creating = False
    def interrupted(signum, _frame):
        nonlocal deferred_signal
        if creating:
            deferred_signal = signum
        else:
            raise LaunchInterrupted(signum)
    handlers = {signum: signal.signal(signum, interrupted) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        if sys.version_info[:2] != (3, 11):
            raise ValueError("launcher requires Python 3.11")
        # Complete every payload check before the first Docker command, especially docker load.
        manifest = verify_bundle(args.bundle)
        if args.command == "image":
            print(loaded_image(manifest["images"][args.role], local_docker_environment())["id"])
            return 0
        print(f"Bundle hashes verified; source {manifest['source_revision']}", flush=True)
        validate_image_archive(args.bundle.resolve() / "images.tar", manifest["images"])
        print("Image archive lists all three tagged images for classic and containerd Docker stores", flush=True)
        if args.command == "check":
            return 0
        if not sys.platform.startswith("linux"):
            raise ValueError("offline verification requires Linux or WSL with a local Linux Docker daemon")
        cpu_set = select_cpus(args.cpu_set)
        output = new_output_dir(args.output_root or args.bundle / "results")
        report_path = output / "launch.json"
        report = {"status": "in_progress", "phase": "docker_preflight", "source_revision": manifest["source_revision"],
                  "images": manifest["images"], "cpu_set": cpu_set, "controller_memory_limit": "1536m",
                  "network": "none", "suite": args.suite, "started_at": datetime.now(timezone.utc).isoformat()}
        write_report(report_path, report)
        environment = local_docker_environment()
        info = subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", "info", "--format", "{{json .}}"],
                              check=True, text=True, capture_output=True, env=environment)
        daemon = json.loads(info.stdout)
        if daemon.get("OSType") != "linux":
            raise ValueError("a local Linux Docker daemon is required")
        report["resource_controls"] = resource_preflight(daemon)
        if report["resource_controls"]["status"] != "available":
            print("Resource limits are not supported by this daemon; functional checks continue, resource acceptance remains not_checked.", flush=True)
        # Ignore Docker contexts/DOCKER_HOST: this launcher deliberately uses the local socket.
        report.update(phase="loading_images", docker_server_version=daemon.get("ServerVersion"))
        write_report(report_path, report)
        load_started = time.monotonic()
        subprocess.run(["docker", "load", "--input", str(args.bundle.resolve() / "images.tar")], check=True, env=environment)
        report["image_load_seconds"] = time.monotonic() - load_started
        resolved = {role: loaded_image(image, environment) for role, image in manifest["images"].items()}
        runtime_manifest = {**manifest, "images": resolved}
        launch_id = uuid.uuid4().hex
        command = runtime_command(runtime_manifest, output, cpu_set, args.suite,
                                  Path("/var/run/docker.sock").stat().st_gid, launch_id)
        report.update(phase="verification", command=command, loaded_images=resolved)
        write_report(report_path, report)
        print(f"Running offline verification on CPUs {cpu_set}; results: {output}", flush=True)
        verification_started = time.monotonic()
        # Create synchronously before attaching: killing a docker-run client can
        # otherwise leave a daemon request creating a container after cleanup.
        report["launch_id"] = launch_id
        write_report(report_path, report)
        creating = True
        try:
            subprocess.run(["docker", "create", *command[2:]], env=environment,
                           capture_output=True, check=True, timeout=30, start_new_session=True)
        finally:
            creating = False
        if deferred_signal is not None:
            raise LaunchInterrupted(deferred_signal)
        result = subprocess.run(["docker", "start", "--attach", "auditlab-controller-" + launch_id],
                                env=environment, start_new_session=True)
        report["verification_container_seconds"] = time.monotonic() - verification_started
        code = result.returncode
        report["child_exit_code"] = code
        controller_path = output / "controller-resources.json"
        controller = json.loads(controller_path.read_text()) if controller_path.is_file() else {}
        verified_resources = (report["resource_controls"]["status"] == "available"
                              and controller.get("status") == "measured"
                              and controller.get("resource_limits_verified") is True)
        report["controller_resource_status"] = "passed" if verified_resources else "not_checked"
        if code == 0 and not verified_resources:
            code = 2
        report.update(status="completed" if code == 0 else "incomplete" if code == 2 else "failed",
                      exit_code=code)
        return code
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Offline launch failed: {error}", file=sys.stderr)
        if report is not None:
            report.update(status="failed", exit_code=1, error=str(error))
        return 1
    except (KeyboardInterrupt, LaunchInterrupted) as error:
        signum = error.signum if isinstance(error, LaunchInterrupted) else signal.SIGINT
        code = 128 + signum
        if report is not None:
            report.update(status="interrupted", exit_code=code, signal=signum)
        return code
    finally:
        # A repeated Ctrl+C must not interrupt the bounded cleanup itself.
        for signum in handlers:
            signal.signal(signum, signal.SIG_IGN)
        cleanup_failed = False
        if launch_id is not None:
            try:
                report["cleanup"] = cleanup_launch(launch_id, environment)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                cleanup_failed = True
                report["cleanup"] = {"status": "failed", "error": str(error)}
                print(f"Container cleanup failed: {error}", file=sys.stderr)
                if report["status"] != "interrupted":
                    report.update(status="failed", exit_code=1)
        try:
            if report is not None and report_path is not None:
                report["elapsed_seconds"] = time.monotonic() - started
                write_report(report_path, report)
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
        if cleanup_failed and report["status"] != "interrupted":
            return 1


if __name__ == "__main__":
    sys.exit(main())
