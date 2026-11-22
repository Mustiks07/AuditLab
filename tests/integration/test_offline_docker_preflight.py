"""Regression: without a usable Docker daemon the launcher names the cause and the next step.

Found by the RC4 receiver walk (docs/reviews/jury-compatibility-v3): the published launcher printed
``Command '[docker, --host, ..., info, ...]' returned non-zero exit status 1`` or
``[Errno 2] No such file or directory: 'docker'``, and ``image`` claimed the image was not loaded
when the daemon was simply not running. A failed preflight must never look like acceptance.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import offline
from test_offline_bundle import manifest_for

NOT_RUNNING = ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
               "Is the docker daemon running?")
DENIED = ("permission denied while trying to connect to the Docker daemon socket at "
          "unix:///var/run/docker.sock: Get \"http://%2Fvar%2Frun%2Fdocker.sock/v1.47/info\": "
          "dial unix /var/run/docker.sock: connect: permission denied")


def docker_info(returncode=1, stderr="", missing=False):
    def run(command, **kwargs):
        if command[:2] == ["docker", "--host"]:
            if missing:
                raise FileNotFoundError(2, "No such file or directory", "docker")
            return subprocess.CompletedProcess(command, returncode, "", stderr)
        raise AssertionError(f"no Docker command may follow a failed preflight: {command}")
    return run


class DockerPreflight(unittest.TestCase):
    def launch(self, root, run, command="verify"):
        out, err = io.StringIO(), io.StringIO()
        with patch("offline.subprocess.run", side_effect=run), redirect_stdout(out), redirect_stderr(err):
            args = [command, "--bundle", str(root)]
            if command == "verify":
                args += ["--output-root", str(root / "results")]
            code = offline.main(args)
        reports = list((root / "results").glob("*/launch.json"))
        return code, err.getvalue(), json.loads(reports[0].read_text()) if reports else None

    def test_each_cause_has_a_code_message_and_next_step_and_is_not_acceptance(self):
        cases = [
            ("docker_daemon_unreachable", docker_info(stderr=NOT_RUNNING), "systemctl start docker"),
            ("docker_socket_permission", docker_info(stderr=DENIED), "socket's group"),
            ("docker_cli_missing", docker_info(missing=True), "Install Docker Engine"),
            ("docker_daemon_error", docker_info(stderr="unexpected failure"), "fix the reported problem"),
        ]
        for code_name, run, hint in cases:
            with self.subTest(code_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest_for(root)
                code, stderr, report = self.launch(root, run)
                self.assertEqual(code, 1)
                self.assertIn("Docker is not available", stderr)
                self.assertIn(hint, stderr)
                self.assertIn("this is not an acceptance result", stderr)
                self.assertNotIn("returned non-zero exit status", stderr)
                self.assertEqual((report["status"], report["exit_code"], report["error_code"]), ("failed", 1, code_name))
                self.assertEqual(report["phase"], "docker_preflight")
                self.assertIn("policy_isolation", report["not_checked"])
                self.assertNotIn("passed", json.dumps(report))

    def test_image_command_reports_the_daemon_not_a_missing_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_for(root)
            code, stderr, _ = self.launch(root, docker_info(stderr=NOT_RUNNING), command="image")
            self.assertEqual(code, 1)
            self.assertIn("no Docker daemon answers", stderr)
            self.assertNotIn("bundle image is not loaded", stderr)


if __name__ == "__main__":
    unittest.main()
