"""Build a self-contained AuditLab bundle from one clean, committed source revision.

Run from the repository root: python tools/build_release.py --output build/release
The build needs network access once; the resulting offline.py launcher never pulls images.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("runtime", "verifier", "policy")


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def captured(command, **kwargs):
    return run(command, text=True, capture_output=True, **kwargs).stdout.strip()


def checksum(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate_image_archive(path, images):
    """Both Docker's legacy catalog and the OCI index must retain all three images.

    Saving multiple anonymous image IDs can produce three Docker entries but only
    one OCI descriptor. Classic storage masks that defect; containerd uses the index.
    """
    expected = {image["id"] for image in images.values()}
    with tarfile.open(path) as archive:
        catalog = json.load(archive.extractfile("manifest.json"))
        for role, image in images.items():
            matches = [row for row in catalog if image["tag"] in (row.get("RepoTags") or [])]
            if len(matches) != 1 or Path(matches[0]["Config"]).stem != image["id"].split(":", 1)[1]:
                raise ValueError(f"saved Docker catalog lost the tagged {role} identity")
        if "index.json" not in archive.getnames():
            return {}  # Older Docker-only archives have no competing OCI index.
        index = json.load(archive.extractfile("index.json"))
        configs = {}
        for descriptor in index["manifests"]:
            digest = descriptor["digest"]
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("invalid saved OCI manifest digest")
            data = archive.extractfile("blobs/sha256/" + digest.split(":", 1)[1]).read()
            if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("saved OCI manifest checksum differs")
            configs[json.loads(data)["config"]["digest"]] = digest
        if set(configs) != expected:
            raise ValueError("saved OCI index does not cover all three image configurations")
        return configs


def excluded(name):
    path = Path(name)
    if not path.parts:
        return False
    if path.parts[0] in {"artifacts", "runs", "build", ".git", ".venv", "venv", "node_modules"}:
        return True
    if any(part in {"__pycache__", ".venv", "node_modules", ".git", ".ssh", ".secrets", "secrets"} for part in path.parts):
        return True
    return path.name == ".env" or path.name.startswith(".env.") or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}


def source_archive(root, revision, destination):
    # Git archive reads the committed tree, never ignored/untracked files in a developer's directory.
    raw = subprocess.run(["git", "archive", "--format=tar", revision], cwd=root, check=True,
                         stdout=subprocess.PIPE).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as original:
        with destination.open("wb") as output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                with tarfile.open(mode="w", fileobj=compressed) as archive:
                    for entry in original:
                        if excluded(entry.name):
                            continue
                        if entry.issym() or entry.islnk():
                            raise ValueError(f"source archive does not accept links: {entry.name}")
                        archive.addfile(entry, original.extractfile(entry) if entry.isfile() else None)


LICENSE_SCRIPT = r'''
import importlib.metadata as md, json, pathlib, platform
packages=[]
for dist in sorted(md.distributions(), key=lambda d: d.metadata.get('Name','').lower()):
    texts=[]
    for path in dist.files or []:
        if any(token in path.name.lower() for token in ('license','copying','notice')):
            source=dist.locate_file(path)
            if source.is_file():
                texts.append({'path':str(path),'text':source.read_text(encoding='utf-8',errors='replace')})
    packages.append({'name':dist.metadata.get('Name'), 'version':dist.version,
                     'license_expression':dist.metadata.get('License-Expression'),
                     'license_metadata':dist.metadata.get('License'), 'texts':texts})
operating_system=[]
for path in sorted(pathlib.Path('/usr/share/doc').glob('*/copyright')):
    if path.is_file():
        operating_system.append({'path':str(path),'text':path.read_text(encoding='utf-8',errors='replace')})
python_license=pathlib.Path('/usr/local/lib/python3.11/LICENSE.txt')
print(json.dumps({'python':platform.python_version(),'packages':packages,'os_copyrights':operating_system,
                  'python_license':python_license.read_text() if python_license.is_file() else None}))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new bundle directory; never overwritten")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    try:
        if sys.version_info[:2] != (3, 11) or sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
            raise ValueError("bundle builder requires Python 3.11 on Linux x86_64")
        if captured(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT):
            raise ValueError("release build requires a clean committed checkout (including build scripts and license)")
        revision = captured(["git", "rev-parse", "HEAD"], cwd=ROOT)
        if output.exists():
            raise FileExistsError(f"bundle destination already exists: {output}")
        output.mkdir(parents=True)
        source_archive(ROOT, revision, output / "source.tar.gz")
        with tempfile.TemporaryDirectory(prefix="auditlab-release-") as temporary:
            context = Path(temporary)
            with tarfile.open(output / "source.tar.gz", "r:gz") as archive:
                archive.extractall(context, filter="data")
            if not (context / "LICENSE").is_file():
                raise ValueError("a committed source LICENSE is required")
            shutil.copyfile(context / "offline.py", output / "offline.py")
            wheels = context / "docker" / "wheels"
            wheels.mkdir(parents=True, exist_ok=True)
            run([sys.executable, "-m", "pip", "download", "--require-hashes", "--only-binary=:all:",
                 "--dest", str(wheels), "-r", str(context / "requirements.lock")])
            images = {}
            inventories = {}
            for role in ROLES:
                tag = f"auditlab-{role}:{revision[:12]}"
                dockerfile = "docker/runtime/Dockerfile" if role == "policy" else "docker/release/Dockerfile"
                command = ["docker", "build", "--network", "none", "--file", str(context / dockerfile), "--tag", tag,
                           "--build-arg", f"SOURCE_REVISION={revision}"]
                if role != "policy":
                    command += ["--target", role]
                command.append(str(context))
                run(command)
                inspected = json.loads(captured(["docker", "image", "inspect", tag, "--format", "{{json .}}"]))
                if inspected["Os"] != "linux":
                    raise ValueError("release images must target Linux")
                images[role] = {"id": inspected["Id"], "tag": tag, "os": inspected["Os"],
                                "architecture": inspected["Architecture"], "uncompressed_size_bytes": inspected["Size"]}
                inventories[role] = json.loads(captured(
                    ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                     "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                     "--entrypoint", "python", inspected["Id"], "-c", LICENSE_SCRIPT]))
            # Tags keep every OCI index entry; saving anonymous IDs can silently keep only one.
            # One archive still deduplicates shared layers.
            run(["docker", "save", "--output", str(output / "images.tar"),
                 *(images[role]["tag"] for role in ROLES)])
            oci_identities = validate_image_archive(output / "images.tar", images)
            for image in images.values():
                if image["id"] in oci_identities:
                    image["oci_manifest_digest"] = oci_identities[image["id"]]
            license_report = {"images": inventories,
                              "scope": "Installed Python distributions, available license texts and Debian copyright files. "
                                       "Docker CLI source license/notice files are included from v29.0.1, verified against Git blob hashes.",
                              "docker_cli_notices": [{"path": str(path.relative_to(context / "docker/release/notices")),
                                                       "text": path.read_text(encoding="utf-8", errors="replace")}
                                                     for path in sorted((context / "docker/release/notices").rglob("*")) if path.is_file()],
                              "source_license": (context / "LICENSE").read_text(encoding="utf-8")}
            (output / "licenses.json").write_text(json.dumps(license_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            files = {name: {"sha256": checksum(output / name), "size_bytes": (output / name).stat().st_size}
                     for name in ("images.tar", "source.tar.gz", "licenses.json", "offline.py")}
            manifest = {"format_version": "1.0", "created_at": datetime.now(timezone.utc).isoformat(),
                        "source_revision": revision, "images": images, "files": files,
                        "python_version": (context / ".python-version").read_text().strip(),
                        "requirements_lock_sha256": checksum(context / "requirements.lock"),
                        "payload_size_bytes": sum(item["size_bytes"] for item in files.values()),
                        "docker_version": captured(["docker", "version", "--format", "{{json .}}"])}
            (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            checksums = {**{name: item["sha256"] for name, item in files.items()},
                         "manifest.json": checksum(output / "manifest.json")}
            (output / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in checksums.items()), encoding="ascii")
        print(f"Built {output}; payload {manifest['payload_size_bytes']} bytes; revision {revision}")
        print("Run: python offline.py verify --suite rehearsal (from the bundle directory)")
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Release build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
