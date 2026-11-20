"""Release evidence is green only with complete functional and actual resource proof."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

import offline
from tools.check_rehearsal_evidence import check, POLICIES, SUITES


def resource(pids):
    return dict(status="measured", resource_limits_verified=True,
                scope="container_limits_verified", limit_mismatches=[],
                memory_limit_bytes=1536 * 1024 ** 2, cpu_quota_usec=200000,
                cpu_period_usec=100000, cpuset_cpus="0-1", peak_memory_bytes=1000,
                cpu_usage_usec=100, requested_limits={"pids": str(pids)},
                raw_observed={"files": {"pids.max": str(pids)}})


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.launch = dict(source_revision="a" * 40, cpu_set="0,1", child_exit_code=2, controller_memory_limit="1536m", resource_controls={"status": "available"})
        self.general = dict(commit="a" * 40, checks=[dict(id=name, status="passed", tests_run=1,
                            records=[dict(id=name, status="passed")]) for name in SUITES],
                            acceptance={"status": "not_checked"})
        self.rehearsal = dict(policy_memory_limit_bytes=1536 * 1024 ** 2,
            datasets=[dict(id=f"internal-{i:02d}", status="passed", independent={"resources": resource(128)}) for i in range(1,21)],
            pairs=[dict(id=f"pair-{i:02d}", status="passed", independent={"resources": resource(128)}) for i in range(1,21)],
            episodes=[dict(id=f"{p}/internal-{i:02d}", policy=p, status="passed",
                           elapsed_seconds=1, resources=resource(128) if p != "oracle" else None)
                      for p in POLICIES for i in range(1,21)])
        self.controller = resource(256)

    def run_check(self, **kwargs):
        for path, data in [("launch.json", self.launch), ("verify/report.json", self.general),
                           ("verify/rehearsal/report.json", self.rehearsal),
                           ("controller-resources.json", self.controller)]:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data))
        return check(self.root, **kwargs)

    def test_unrelated_revision_cannot_pass(self):
        self.assertEqual(self.run_check(expected_revision="a" * 40)["exit_code"], 0)
        self.assertEqual(self.run_check(expected_revision="b" * 40)["exit_code"], 1)
        self.general["commit"] = "b" * 40
        self.assertEqual(self.run_check()["exit_code"], 1)

    def test_containerd_identity_is_bound_to_bundle_and_rehearsal(self):
        source, actual = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        self.launch["images"] = {r: dict(id=source, oci_manifest_digest=actual) for r in ("runtime", "policy", "verifier")}
        self.launch["loaded_images"] = {r: dict(id=actual, source_id=source) for r in self.launch["images"]}
        self.rehearsal.update(policy_image=actual, verifier_image=actual)
        self.assertEqual(self.run_check()["exit_code"], 0)
        self.rehearsal["policy_image"] = source
        self.assertEqual(self.run_check()["exit_code"], 1)
        self.rehearsal["policy_image"] = actual
        self.launch["loaded_images"]["runtime"]["id"] = "sha256:" + "c" * 64
        self.assertEqual(self.run_check()["exit_code"], 1)

    def test_bundle_hash_and_image_identity_are_checked(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        data = b"fixture"
        files = {}
        for name in offline.FILES:
            (bundle / name).write_bytes(data)
            files[name] = dict(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        images = {role: dict(id="sha256:" + "a" * 64, os="linux")
                  for role in ("runtime", "verifier", "policy")}
        manifest = dict(format_version="1.0", files=files, images=images,
                        source_revision="a" * 40, payload_size_bytes=4 * len(data))
        (bundle / "manifest.json").write_text(json.dumps(manifest))
        self.launch["images"] = images
        self.assertEqual(self.run_check(bundle=bundle)["exit_code"], 0)
        (bundle / "images.tar").write_bytes(b"changed")  # Same size, different digest.
        self.assertEqual(self.run_check(bundle=bundle)["exit_code"], 1)
        (bundle / "images.tar").write_bytes(data)
        self.launch["images"] = {}
        self.assertEqual(self.run_check(bundle=bundle)["exit_code"], 1)

    def test_complete_internal_rehearsal_does_not_claim_full_acceptance(self):
        result = self.run_check()
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("not certified", result["scope"])

    def test_container_budget_fits_decimal_four_gb_without_certifying_host_ram(self):
        result = self.run_check()
        self.assertEqual(result['memory_budget']['combined_container_limit_bytes'], 3 * 1024 ** 3)
        self.assertGreater(result['memory_budget']['decimal_4gb_headroom_bytes'], 0)
        self.assertEqual(result['memory_budget']['whole_host_status'], 'not_checked')
        self.controller['memory_limit_bytes'] = 2 * 1024 ** 3
        self.assertEqual(self.run_check()['exit_code'], 1)

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
