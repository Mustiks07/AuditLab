"""Kernel counter units, unavailable telemetry, and cleanup despite probe failure."""
import json
import subprocess
import os
import tempfile
from pathlib import Path
import importlib.util
import unittest
from unittest.mock import patch

from auditlab.runner.isolation import DockerPolicy, SubprocessPolicy, _parse_cgroup_measurements, _verify_cgroup_scope


class ResourceMeasurements(unittest.TestCase):
    def test_cgroup_counters_keep_units_and_large_exact_integers(self):
        measured = _parse_cgroup_measurements({"values": {
            "memory.peak": "9007199254740993", "memory.max": "2147483648", "memory.swap.max": "0",
            "cpu.stat": "usage_usec 1234567\nuser_usec 1000000\nsystem_usec 234567\nnr_throttled 2",
            "cpu.max": "200000 100000", "cpuset.cpus.effective": "0-1"}})
        self.assertEqual(measured["status"], "measured")
        self.assertEqual(measured["peak_memory_bytes"], 9007199254740993)
        self.assertEqual(measured["cpu_usage_usec"], 1234567)
        self.assertEqual(measured["memory_limit_bytes"], 2 * 1024**3)
        self.assertEqual(measured["cpu_quota_usec"] / measured["cpu_period_usec"], 2)
        self.assertEqual(measured["cpuset_cpus"], "0-1")

    def test_old_kernel_or_partial_counters_are_not_reported_as_zero_peak(self):
        absent = _parse_cgroup_measurements({"values": {}, "unavailable": {
            "memory.peak": "FileNotFoundError", "cpu.stat": "FileNotFoundError"}})
        self.assertEqual(absent["status"], "not_checked")
        self.assertIsNone(absent["peak_memory_bytes"])
        self.assertIsNone(absent["cpu_usage_usec"])
        partial = _parse_cgroup_measurements({"values": {
            "memory.peak": "bad", "memory.current": "123", "cpu.stat": "usage_usec 20"}})
        self.assertEqual(partial["status"], "partial")
        self.assertIsNone(partial["peak_memory_bytes"], "current memory is not a measured peak")
        self.assertEqual(partial["cpu_usage_usec"], 20)

    def test_timed_out_probe_does_not_prevent_container_or_process_cleanup(self):
        policy = object.__new__(DockerPolicy)
        policy._closed = False
        policy.container_name = "unit-test-only"
        policy._docker_env = {}
        policy.measurements = {"status": "not_checked", "peak_memory_bytes": None,
                               "cpu_usage_usec": None, "reason": "not_collected"}
        with patch("auditlab.runner.isolation.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["docker", "exec"], 2)), \
                patch.object(policy, "_remove_container") as remove, \
                patch.object(SubprocessPolicy, "close") as close:
            policy.close()
        remove.assert_called_once()
        close.assert_called_once()
        self.assertEqual(policy.measurements["status"], "not_checked")
        self.assertIsNone(policy.measurements["peak_memory_bytes"])
        self.assertEqual(policy.measurements["reason"], "measurement_failed:TimeoutExpired")

    def test_probe_preserves_image_provenance_and_requested_limits(self):
        policy = object.__new__(DockerPolicy)
        policy.container_name = "unit-test-only"
        policy._docker_env = {}
        policy.measurements = {"image_id": "sha256:fixture", "requested_limits": {"memory": "2g", "cpus": "2", "cpuset": "0,1", "pids": 128}}
        payload = {"values": {"memory.peak": "12345", "memory.max": "2147483648", "memory.swap.max": "0",
                   "cpu.stat": "usage_usec 400\nuser_usec 300\nsystem_usec 100",
                   "cpu.max": "200000 100000", "cpuset.cpus.effective": "0-1", "pids.max": "128"},
                   "affinity": [0, 1], "cgroup": "0::/"}
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload).encode())
        with patch("auditlab.runner.isolation.subprocess.run", return_value=response):
            policy._collect_measurements()
        self.assertEqual(policy.measurements["status"], "measured")
        self.assertEqual(policy.measurements["peak_memory_bytes"], 12345)
        self.assertEqual(policy.measurements["image_id"], "sha256:fixture")
        self.assertEqual(policy.measurements["requested_limits"], {"memory": "2g", "cpus": "2", "cpuset": "0,1", "pids": 128})

    def test_parent_cgroup_is_not_policy_memory_or_cpu_measurement(self):
        parent = {"values": {"memory.peak": "15032385536", "memory.max": "17179869184",
                  "cpu.max": "400000 100000", "cpu.stat": "usage_usec 999999999",
                  "cpuset.cpus.effective": "0-4", "pids.max": "max"},
                  "affinity": [0, 1, 2, 3, 4], "cgroup": "0::/"}
        requested = {"memory": "2g", "cpus": "2", "cpuset": "0,1", "pids": 128}
        result = _verify_cgroup_scope(_parse_cgroup_measurements(parent), parent, requested)
        self.assertEqual(result["status"], "not_checked")
        self.assertFalse(result["resource_limits_verified"])
        self.assertEqual(result["scope"], "unverified_visible_cgroup")
        self.assertIsNone(result["peak_memory_bytes"])
        self.assertIsNone(result["cpu_usage_seconds"])
        self.assertIsNone(result["cpu_usage_usec"])
        self.assertEqual(result["raw_observed"]["peak_memory_bytes"], 15032385536)
        self.assertEqual(len(result["limit_mismatches"]), 6)

    def test_missing_or_nonzero_swap_limit_is_not_a_verified_memory_bound(self):
        payload = {"values": {"memory.peak": "123", "memory.max": "2147483648",
                   "cpu.max": "200000 100000", "cpu.stat": "usage_usec 10",
                   "cpuset.cpus.effective": "0-1", "pids.max": "128"}, "affinity": [0, 1]}
        requested = {"memory": "2g", "cpus": "2", "cpuset": "0,1", "pids": 128}
        for swap in (None, "max", "2147483648"):
            with self.subTest(swap=swap):
                if swap is None:
                    payload["values"].pop("memory.swap.max", None)
                else:
                    payload["values"]["memory.swap.max"] = swap
                result = _verify_cgroup_scope(_parse_cgroup_measurements(payload), payload, requested)
                self.assertEqual(result["status"], "not_checked")
                self.assertIsNone(result["peak_memory_bytes"])
                self.assertFalse(result["resource_limits_verified"])
                self.assertEqual(len(result["limit_mismatches"]), 1)
                self.assertIn("memory.swap.max", result["limit_mismatches"][0])

    def test_matching_limits_but_wrong_process_affinity_are_unverified(self):
        payload = {"values": {"memory.peak": "123", "memory.max": "2147483648", "memory.swap.max": "0",
                   "cpu.max": "200000 100000", "cpu.stat": "usage_usec 10",
                   "cpuset.cpus.effective": "0-1", "pids.max": "128"}, "affinity": [0, 1, 2]}
        result = _verify_cgroup_scope(_parse_cgroup_measurements(payload), payload,
                  {"memory": "2g", "cpus": "2", "cpuset": "0,1", "pids": 128})
        self.assertEqual(result["status"], "not_checked")
        self.assertIsNone(result["peak_memory_bytes"])
        self.assertIn("process affinity", result["limit_mismatches"][0])

    def test_controller_reports_parent_as_raw_only_and_valid_child_as_measured(self):
        path = Path(__file__).resolve().parents[2] / "tools/container_verify.py"
        spec = importlib.util.spec_from_file_location("container_verify_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {"memory.peak": "15032385536", "memory.max": "17179869184",
                     "cpu.max": "400000 100000", "cpu.stat": "usage_usec 500", "pids.max": "max",
                     "cpuset.cpus.effective": "0-4"}
            for name, value in files.items():
                (root / name).write_text(value)
            with patch.object(module, "CGROUP", root), patch.dict(os.environ, {"AUDITLAB_CPUSET": "0,1"}), \
                    patch.object(os, "sched_getaffinity", return_value={0, 1, 2, 3, 4}):
                result = module.controller_resources()
            self.assertEqual(result["status"], "not_checked")
            self.assertIsNone(result["memory.peak"])
            self.assertIsNone(result["peak_memory_bytes"])
            self.assertIsNone(result["cpu.stat"])
            self.assertEqual(result["raw_observed"]["files"]["memory.peak"], "15032385536")
            files.update({"memory.peak": "123456", "memory.max": "2147483648", "memory.swap.max": "0",
                          "cpu.max": "200000 100000", "cpuset.cpus.effective": "0-1", "pids.max": "256"})
            for name, value in files.items():
                (root / name).write_text(value)
            with patch.object(module, "CGROUP", root), patch.dict(os.environ, {"AUDITLAB_CPUSET": "0,1"}), \
                    patch.object(os, "sched_getaffinity", return_value={0, 1}):
                result = module.controller_resources()
            self.assertEqual(result["status"], "measured")
            self.assertTrue(result["resource_limits_verified"])
            self.assertEqual(result["peak_memory_bytes"], 123456)
            self.assertEqual(result["cpu_usage_seconds"], .0005)


if __name__ == "__main__":
    unittest.main()
