"""Release evidence is green only with complete functional and actual resource proof."""
import json
from pathlib import Path
import tempfile
import unittest

import offline
from tools.check_rehearsal_evidence import check, POLICIES, SUITES


def resource(pids):
    return dict(status="measured", resource_limits_verified=True,
                scope="container_limits_verified", limit_mismatches=[],
                memory_limit_bytes=2 * 1024 ** 3, cpu_quota_usec=200000,
                cpu_period_usec=100000, cpuset_cpus="0-1", peak_memory_bytes=1000,
                cpu_usage_usec=100, requested_limits={"pids": str(pids)},
                raw_observed={"files": {"pids.max": str(pids)}})


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.launch = dict(cpu_set="0,1", child_exit_code=2, resource_controls={"status": "available"})
        self.general = dict(checks=[dict(id=name, status="passed", tests_run=1,
                            records=[dict(id=name, status="passed")]) for name in SUITES],
                            acceptance={"status": "not_checked"})
        self.rehearsal = dict(
            datasets=[dict(id=f"internal-{i:02d}", status="passed", independent={"resources": resource(128)}) for i in range(1,21)],
            pairs=[dict(id=f"pair-{i:02d}", status="passed", independent={"resources": resource(128)}) for i in range(1,21)],
            episodes=[dict(id=f"{p}/internal-{i:02d}", policy=p, status="passed",
                           elapsed_seconds=1, resources=resource(128) if p != "oracle" else None)
                      for p in POLICIES for i in range(1,21)])
        self.controller = resource(256)

    def run_check(self):
        for path, data in [("launch.json", self.launch), ("verify/report.json", self.general),
                           ("verify/rehearsal/report.json", self.rehearsal),
                           ("controller-resources.json", self.controller)]:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data))
        return check(self.root)

    def test_complete_internal_rehearsal_does_not_claim_full_acceptance(self):
        result = self.run_check()
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("not certified", result["scope"])

    def test_skip_or_missing_evidence_cannot_pass(self):
        self.general["checks"][0]["records"][0]["status"] = "not_checked"
        self.assertEqual(self.run_check()["exit_code"], 2)
        self.general = {}; self.rehearsal = {}; self.controller = {}
        self.assertEqual(self.run_check()["exit_code"], 2)

    def test_parent_cgroup_and_unsupported_daemon_cannot_pass(self):
        self.controller["resource_limits_verified"] = False
        self.assertEqual(self.run_check()["exit_code"], 2)
        self.controller = resource(256)
        self.launch["resource_controls"]["status"] = "not_checked"
        self.assertEqual(self.run_check()["exit_code"], 2)
        self.launch["resource_controls"]["status"] = "available"
        self.controller["memory_limit_bytes"] *= 8
        self.assertEqual(self.run_check()["exit_code"], 1)

    def test_all_independent_verifier_runs_require_actual_resource_evidence(self):
        self.rehearsal["datasets"][0]["independent"] = {}
        self.assertEqual(self.run_check()["exit_code"], 2)
        self.rehearsal["datasets"][0]["independent"] = {"resources": resource(128)}
        self.rehearsal["pairs"][-1]["independent"]["resources"]["cpu_quota_usec"] = 400000
        self.assertEqual(self.run_check()["exit_code"], 1)

    def test_actual_pid_limits_accept_producer_metadata_shapes(self):
        # Controller emits string requests; independent verifier has no request metadata.
        self.assertEqual(self.controller["requested_limits"]["pids"], "256")
        for row in self.rehearsal["datasets"] + self.rehearsal["pairs"]:
            row["independent"]["resources"].pop("requested_limits")
        result = self.run_check()
        self.assertEqual(result["exit_code"], 0, result)

    def test_actual_pid_limit_is_required_and_cannot_be_replaced_by_requested_value(self):
        self.controller["raw_observed"]["files"].pop("pids.max")
        self.assertEqual(self.run_check()["exit_code"], 2)
        self.controller["raw_observed"]["files"]["pids.max"] = "512"
        self.assertEqual(self.run_check()["exit_code"], 1)
        self.controller = resource(256)
        self.rehearsal["pairs"][-1]["independent"]["resources"]["raw_observed"]["files"]["pids.max"] = "max"
        self.assertEqual(self.run_check()["exit_code"], 1)

    def test_slow_attempt_fails(self):
        self.rehearsal["episodes"][0]["elapsed_seconds"] = 60.01
        self.assertEqual(self.run_check()["exit_code"], 1)

    def test_resource_preflight_is_only_capability_evidence(self):
        info = dict(CgroupDriver="systemd", CgroupVersion="2", **{name: True for name in
                    ("MemoryLimit", "SwapLimit", "CpuCfsPeriod", "CpuCfsQuota", "CPUSet", "PidsLimit")})
        self.assertEqual(offline.resource_preflight(info)["status"], "available")
        info["CgroupDriver"] = "none"
        self.assertEqual(offline.resource_preflight(info)["status"], "not_checked")
        self.assertEqual(offline.resource_preflight({})["status"], "not_checked")
