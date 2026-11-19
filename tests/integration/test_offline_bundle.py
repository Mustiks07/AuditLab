"""Offline launcher contract: corrupt packages never reach Docker, no implicit pulls or mounts."""
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import offline


def manifest_for(directory):
    files = {}
    for name in offline.FILES:
        content = (name + " payload\n").encode()
        (directory / name).write_bytes(content)
        files[name] = {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
    manifest = {"format_version": "1.0", "source_revision": "a" * 40, "files": files,
                "images": {role: {"id": "sha256:" + character * 64, "os": "linux"}
                           for role, character in (("runtime", "1"), ("verifier", "2"), ("policy", "3"))}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return manifest


class OfflineBundleTests(unittest.TestCase):
    def test_corrupt_payload_is_rejected_before_any_docker_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_for(root)
            (root / "images.tar").write_bytes(b"altered image archive\n")
            with patch("offline.subprocess.run") as docker:
                self.assertEqual(offline.main(["verify", "--bundle", str(root)]), 1)
                docker.assert_not_called()

    def test_check_mode_needs_no_docker_and_checks_every_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_for(root)
            with patch("offline.subprocess.run") as docker:
                self.assertEqual(offline.main(["check", "--bundle", str(root)]), 0)
                docker.assert_not_called()
            (root / "source.tar.gz").write_bytes(b"truncated")
            with self.assertRaises(ValueError):
                offline.verify_bundle(root)

    def test_runtime_is_networkless_and_uses_only_result_and_trusted_socket_mounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = manifest_for(root)
            command = offline.runtime_command(manifest, root, "2,4", "rehearsal", docker_gid=1234)
            self.assertEqual(command[command.index("--group-add") + 1], "1234")
            self.assertEqual(command[command.index("--user") + 1], f"{offline.os.getuid()}:{offline.os.getgid()}")
            for option, expected in (("--pull", "never"), ("--network", "none"), ("--cpus", "2"),
                                     ("--cpuset-cpus", "2,4"), ("--memory", "2g"),
                                     ("--memory-swap", "2g"), ("--cpu-set", "2,4")):
                self.assertEqual(command[command.index(option) + 1], expected)
            mounts = [command[i + 1] for i, item in enumerate(command) if item == "--mount"]
            self.assertEqual(mounts, ["type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
                                      f"type=bind,src={root},dst=/results"])
            self.assertIn(manifest["images"]["runtime"]["id"], command)
            self.assertIn("AUDITLAB_POLICY_IMAGE=" + manifest["images"]["policy"]["id"], command)
            self.assertIn(manifest["images"]["verifier"]["id"], command)
            self.assertIn("AUDITLAB_CPUSET=2,4", command)
            self.assertIn("tools/container_verify.py", command)
            self.assertIn("--read-only", command)
            self.assertNotIn("--privileged", command)

    def test_controller_measurement_preserves_failed_child_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "resources.json"
            wrapper = Path(offline.__file__).resolve().parent / "tools" / "container_verify.py"
            result = subprocess.run([sys.executable, str(wrapper), "--output", str(report), "--",
                                     sys.executable, "-c", "raise SystemExit(7)"], capture_output=True)
            self.assertEqual(result.returncode, 7, result.stderr)
            measured = json.loads(report.read_text())
            self.assertEqual(measured["exit_code"], 7)
            self.assertIn("memory.peak", measured)
            self.assertIn("cpu.stat", measured)
            self.assertIn(measured["status"], ("measured", "not_checked"))

    def test_attempts_never_overwrite_previous_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = offline.new_output_dir(temporary)
            (first / "sentinel").write_text("keep")
            second = offline.new_output_dir(temporary)
            self.assertNotEqual(first, second)
            self.assertEqual((first / "sentinel").read_text(), "keep")

    def test_explicit_cpu_set_is_two_available_cores(self):
        with patch("offline.os.sched_getaffinity", return_value={2, 4, 6}, create=True):
            self.assertEqual(offline.select_cpus(), "2,4")
            self.assertEqual(offline.select_cpus("4,6"), "4,6")
            for invalid in ("0,1", "2", "2,2", "2-6", "2,a"):
                with self.subTest(cpu_set=invalid), self.assertRaises(ValueError):
                    offline.select_cpus(invalid)


if __name__ == "__main__":
    unittest.main()
