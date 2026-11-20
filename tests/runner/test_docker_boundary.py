"""Real release sandbox probes. Opt in with AUDITLAB_POLICY_IMAGE after loading the image.

These must run on the submitted image; a skip is explicitly not an ISO-01 pass.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from auditlab.runner import make_env
from auditlab.runner.isolation import DockerPolicy

IMAGE = os.environ.get("AUDITLAB_POLICY_IMAGE")
SNAPSHOT = Path(__file__).resolve().parents[2] / "fixtures" / "foundation" / "S02"


@unittest.skipUnless(IMAGE, "не проверено: set AUDITLAB_POLICY_IMAGE to the preloaded release image")
class RealDockerBoundary(unittest.TestCase):
    def test_canary_files_network_and_trusted_code_are_unavailable(self):
        policy = DockerPolicy(IMAGE, "random", 7, memory="1536m", cpuset=os.environ.get("AUDITLAB_CPUSET"))
        self.addCleanup(policy.close)
        env = make_env("closed", evaluator="inprocess")
        self.addCleanup(env.close)
        observation, _ = env.reset(0, {"snapshot_dir": str(SNAPSHOT)})
        policy.act(observation)  # wait until the actual policy container is running
        with tempfile.TemporaryDirectory() as private:
            truth = Path(private) / "truth.json"
            truth.write_text('{"secret":"SYNTHETIC_CANARY_ONLY"}', encoding="utf-8")
            probe = r'''
import json, os, socket, sys
from pathlib import Path
p = Path(sys.argv[1])
result = {"uid": os.getuid(), "truth_read": False, "truth_write": False, "root_write": False,
          "evaluator_visible": Path("/app/auditlab/evaluator").exists(),
          "runner_visible": Path("/app/auditlab/runner/env.py").exists(),
          "docker_socket": Path("/var/run/docker.sock").exists(), "network": False}
try: p.read_text(); result["truth_read"] = True
except OSError: pass
try: p.with_name("write-probe").write_text("synthetic only"); result["truth_write"] = True
except OSError: pass
try: Path("/app/write-probe").write_text("synthetic only"); result["root_write"] = True
except OSError: pass
s = socket.socket(); s.settimeout(.2)
try: s.connect(("192.0.2.1", 80)); result["network"] = True
except OSError: pass
finally: s.close()
print(json.dumps(result))
'''
            process = subprocess.run(["docker", "exec", policy.container_name, "python", "-c", probe, str(truth)],
                                     capture_output=True, text=True, timeout=5, check=True)
            result = json.loads(process.stdout)
            self.assertEqual(result.pop("uid"), 65534)
            self.assertFalse(any(result.values()), result)
            self.assertEqual(json.loads(truth.read_text())["secret"], "SYNTHETIC_CANARY_ONLY")
            self.assertFalse(truth.with_name("write-probe").exists())
        inspected = subprocess.run(["docker", "inspect", policy.container_name],
                                   capture_output=True, text=True, timeout=5, check=True)
        container = json.loads(inspected.stdout)[0]
        host = container["HostConfig"]
        self.assertEqual(host["NetworkMode"], "none")
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertFalse(host["Privileged"])
        self.assertFalse(container["Mounts"])
        self.assertEqual(host["Memory"], 1536 * 1024**2)
        self.assertEqual(host["NanoCpus"], 2 * 10**9)
        policy.close()
        gone = subprocess.run(["docker", "inspect", policy.container_name], capture_output=True, timeout=5)
        self.assertNotEqual(gone.returncode, 0)


if __name__ == "__main__":
    unittest.main()
