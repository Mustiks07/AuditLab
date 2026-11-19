"""Kernel counter units, unavailable telemetry, and cleanup despite probe failure."""
import json
import subprocess
import unittest
from unittest.mock import patch

from auditlab.runner.isolation import DockerPolicy, SubprocessPolicy, _parse_cgroup_measurements


class ResourceMeasurements(unittest.TestCase):
    def test_cgroup_counters_keep_units_and_large_exact_integers(self):
        measured = _parse_cgroup_measurements({"values": {
            "memory.peak": "9007199254740993", "memory.max": "2147483648",
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
        policy.measurements = {"image_id": "sha256:fixture", "requested_limits": {"memory": "2g"}}
        payload = {"values": {"memory.peak": "12345", "memory.max": "2147483648",
                   "cpu.stat": "usage_usec 400\nuser_usec 300\nsystem_usec 100",
                   "cpu.max": "200000 100000", "cpuset.cpus.effective": "0-1"}}
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload).encode())
        with patch("auditlab.runner.isolation.subprocess.run", return_value=response):
            policy._collect_measurements()
        self.assertEqual(policy.measurements["status"], "measured")
        self.assertEqual(policy.measurements["peak_memory_bytes"], 12345)
        self.assertEqual(policy.measurements["image_id"], "sha256:fixture")
        self.assertEqual(policy.measurements["requested_limits"], {"memory": "2g"})


if __name__ == "__main__":
    unittest.main()
