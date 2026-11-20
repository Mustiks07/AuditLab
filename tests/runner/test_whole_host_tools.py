"""OPS-01 whole-host review tooling (docs/reviews/whole-host-resources): summary and verdict rules.

Run: python -m unittest discover -s tests/runner -p "test_whole_host_tools.py" -v
The summarizer and judge are checked on synthetic samples with known answers; the collector is run
for real on Linux (it reads /proc and cgroup v2 only). None of this measures the release itself.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "docs" / "reviews" / "whole-host-resources"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


summarize_tool = load("summarize_host_memory")
judge_tool = load("judge_review")
collect_tool = load("collect_host_memory")
GIB = 1024 ** 3


def sample(t, available, free, *, oom=0, docker_peak=None, containers=None, swap_free=0, hwm=0):
    total = 3_900_000_000
    return {"t": t, "utc": f"2026-11-20T00:00:{t:06.3f}+00:00",
            "meminfo": {"MemTotal": total, "MemFree": free, "MemAvailable": available, "SwapTotal": 0,
                        "SwapFree": swap_free},
            "derived": {"used_including_cache": total - free, "used_excluding_reclaimable": total - available,
                        "swap_used": 0 - swap_free if swap_free else 0},
            "vmstat": {"oom_kill": oom}, "psi": {"some": {"avg10": 0.5}, "full": {"avg10": 0.0}},
            "cgroups": {"docker.service": {"current": 100, "peak": docker_peak, "max": None, "swap_current": 0,
                                           "events": {"oom_kill": 0}}},
            "containers": containers or {},
            "processes": {"dockerd": {"count": 1, "rss": 90_000_000, "hwm_max": hwm}}}


META = {"method": "test", "interval_s": 0.5, "kernel": "6.x", "monotonic_start": 1000.0}


class Summary(unittest.TestCase):
    def test_sampled_peaks_continuous_peaks_and_phases_stay_separate(self):
        samples = [sample(0.0, 3_000_000_000, 2_500_000_000, docker_peak=200, hwm=95_000_000),
                   sample(0.5, 1_200_000_000, 300_000_000, docker_peak=800,
                          containers={"aaaaaaaaaaaa": {"current": 1_000_000_000, "peak": 1_400_000_000,
                                                       "max": 1610612736, "events": {"oom_kill": 0}}}),
                   sample(1.0, 2_800_000_000, 2_000_000_000, docker_peak=900, hwm=120_000_000)]
        phases = [{"phase": "rehearsal", "exit_code": 2, "start_t": 0.4, "end_t": 0.6}]
        result = summarize_tool.summarize(META, samples, phases)
        self.assertEqual(result["host"]["sampled_peak_used_excluding_reclaimable"]["bytes"], 2_700_000_000)
        self.assertEqual(result["host"]["sampled_peak_used_including_cache"]["bytes"], 3_600_000_000)
        self.assertEqual(result["host"]["sampled_min_mem_available"]["bytes"], 1_200_000_000)
        self.assertEqual(result["named_cgroups"]["docker.service"]["continuous_peak_bytes"], 900)
        self.assertEqual(result["daemons"]["dockerd"]["continuous_peak_rss_hwm_bytes"], 120_000_000)
        container = result["containers"]["aaaaaaaaaaaa"]
        self.assertEqual((container["last_seen_continuous_peak_bytes"], container["limit_bytes"]), (1_400_000_000, 1610612736))
        self.assertEqual(result["phases"][0]["samples"], 1)
        self.assertEqual(result["phases"][0]["sampled_peak_used_excluding_reclaimable"]["bytes"], 2_700_000_000)
        self.assertEqual((result["mem_total_within_limit"], result["sampled_peak_within_limit"]), (True, True))
        self.assertEqual(result["gaps_s"], {"median": 0.5, "max": 0.5})
        blob = json.dumps(result)
        self.assertNotIn("sum", json.dumps({k: v for k, v in result.items() if k != "limits_of_inference"}))
        self.assertIn("not a continuous maximum", blob)
        self.assertIn("| Показатель | Байт |", summarize_tool.markdown(result))

    def test_oom_counters_and_monotonic_phases(self):
        samples = [sample(0.0, 3e9, 3e9, oom=4), sample(0.5, 3e9, 3e9, oom=5)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "phases.jsonl"
            path.write_text(json.dumps({"phase": "check", "exit_code": 0, "start_mono": 1000.2, "end_mono": 1000.6}) + "\n")
            phases = summarize_tool.load_phases(path, META)
        self.assertAlmostEqual(phases[0]["start_t"], 0.2)
        self.assertEqual(summarize_tool.summarize(META, samples, phases)["oom"]["vmstat_oom_kill_delta"], 1)

    def test_truncated_last_line_is_ignored_and_empty_input_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(json.dumps(META))
            (directory / "samples.jsonl").write_text(json.dumps(sample(0.0, 3e9, 3e9)) + "\n{\"t\": 0.5, \"utc")
            meta, samples = summarize_tool.load_samples(directory)
            self.assertEqual(len(samples), 1)
            (directory / "samples.jsonl").write_text("")
            self.assertEqual(summarize_tool.main([str(directory)]), 1)


def write_review(root: Path, *, qualified=True, codes=None, acceptance=None, evidence=None, pulls=False,
                 oom=0, skip=()):
    codes = codes or {"check": 0, "rehearsal": 2, "image_runtime": 0, "evidence_checker": 0, "launch_cleanup": 0,
                      "demo_start": 0, "demo_health": 0, "demo_runs": 0, "crosscheck_live": 0, "replay_check": 0}
    (root / "steps").mkdir(parents=True)
    checks = {"two_cpus": qualified, "mem_total_le_limit": True, "swap_disabled": True}
    (root / "qualification.json").write_text(json.dumps({"qualified": qualified, "checks": checks}))
    (root / "preload-images.json").write_text(json.dumps({"present": []}))
    outputs = {"check": "source b7a1857a51bf47bb36e42699988b38b4d897fa1f ok", "demo_health": '{"ok":true,"runs":6}',
               "launch_cleanup": ""}
    with (root / "phases.jsonl").open("w") as stream:
        for name, code in codes.items():
            if name in skip:
                continue
            stream.write(json.dumps({"phase": name, "exit_code": code, "start_mono": 1, "end_mono": 2}) + "\n")
            (root / "steps" / f"{name}.out").write_text(f"$ cmd\n{outputs.get(name, '')}")
    report = root / "bundle" / "results" / "verify-1" / "verify"
    report.mkdir(parents=True)
    rows = acceptance or [{"id": "API-01", "status": "passed"}, {"id": "OPS-01", "status": "not_checked"},
                          {"id": "OPS-02", "status": "not_checked"}]
    (report / "report.json").write_text(json.dumps({"acceptance": rows}))
    (root / "evidence-summary.json").write_text(json.dumps(evidence or {"status": "internal_rehearsal_passed",
                                                                          "failures": [], "missing": []}))
    (root / "docker-events.log").write_text("x image pull y\n" if pulls else "x container start y\n")
    (root / "memory").mkdir()
    summary = summarize_tool.summarize(META, [sample(0.0, 3e9, 3e9), sample(0.5, 2e9, 1e9, oom=oom)], [])
    (root / "memory" / "summary.json").write_text(json.dumps(summary))


class Verdict(unittest.TestCase):
    SHA = "b7a1857a51bf47bb36e42699988b38b4d897fa1f"

    def verdict(self, **options):
        with tempfile.TemporaryDirectory() as tmp:
            write_review(Path(tmp), **options)
            return judge_tool.judge(Path(tmp), self.SHA)

    def test_confirmed_only_with_every_documented_code_and_no_oom(self):
        result = self.verdict()
        self.assertEqual((result["verdict"], result["failed"], result["not_checked"]), ("confirmed", [], []))

    def test_unqualified_machine_is_never_confirmed(self):
        self.assertEqual(self.verdict(qualified=False)["verdict"], "not_checked")

    def test_defects_fail(self):
        cases = {
            "oom": {"oom": 1},
            "image_pull": {"pulls": True},
            "evidence_missing": {"evidence": {"status": "not_checked", "failures": [], "missing": ["x"]}},
            "extra_open_group": {"acceptance": [{"id": "OPS-01", "status": "not_checked"},
                                                {"id": "ISO-01", "status": "not_checked"}]},
            "failed_group": {"acceptance": [{"id": "MAT-01", "status": "failed"}]},
            "rehearsal_exit_1": {"codes": {"check": 0, "rehearsal": 1, "image_runtime": 0, "evidence_checker": 0,
                                           "launch_cleanup": 0, "demo_start": 0, "demo_health": 0, "demo_runs": 0,
                                           "crosscheck_live": 0, "replay_check": 0}},
        }
        for name, options in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.verdict(**options)["verdict"], "failed")

    def test_missing_step_is_not_checked(self):
        self.assertEqual(self.verdict(skip=("replay_check",))["verdict"], "not_checked")


@unittest.skipUnless(sys.platform.startswith("linux") and Path("/proc/meminfo").is_file(), "не проверено: нужен Linux")
class LiveCollector(unittest.TestCase):
    def test_collector_samples_the_running_linux_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "memory"
            self.assertEqual(collect_tool.main(["--out", str(out), "--interval", "0.1", "--max-seconds", "0.5"]), 0)
            meta, samples = summarize_tool.load_samples(out)
            self.assertGreaterEqual(len(samples), 3)
            self.assertGreater(samples[0]["meminfo"]["MemTotal"], 0)
            self.assertIn("monotonic_start", meta)
            result = summarize_tool.summarize(meta, samples, [])
            self.assertGreater(result["host"]["sampled_peak_used_excluding_reclaimable"]["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
