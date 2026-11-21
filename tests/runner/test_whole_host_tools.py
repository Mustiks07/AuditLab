"""OPS-01 whole-host review tooling (docs/reviews/whole-host-resources): summary and verdict rules.

The verdict is tested on copies of the real RC3 review in docs/reviews/whole-host-resources/logs,
including the negative controls from the PR #32 review.

Run: python -m unittest discover -s tests/runner -p "test_whole_host_tools.py" -v
The summarizer and judge are checked on synthetic samples with known answers; the collector is run
for real on Linux (it reads /proc and cgroup v2 only). None of this measures the release itself.
"""
import importlib.util
import json
import shutil
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
        self.assertEqual(result["daemons"]["dockerd"]["max_single_process_hwm_bytes"], 120_000_000)
        self.assertEqual(result["daemons"]["dockerd"]["sampled_peak_rss_sum_bytes"], 90_000_000)
        container = result["containers"]["aaaaaaaaaaaa"]
        self.assertEqual(container["samples_without_limit"], 0)
        self.assertEqual((container["last_seen_continuous_peak_bytes"], container["limit_bytes"]), (1_400_000_000, 1610612736))
        self.assertEqual(result["phases"][0]["samples"], 1)
        self.assertEqual(result["phases"][0]["sampled_peak_used_excluding_reclaimable"]["bytes"], 2_700_000_000)
        self.assertEqual((result["mem_total_within_limit"], result["sampled_peak_within_limit"]), (True, True))
        self.assertEqual(result["gaps_s"], {"median": 0.5, "max": 0.5})
        blob = json.dumps(result)
        # No host or cgroup figure is a sum of separate peaks (container peaks are never added up).
        self.assertFalse([key for key in result["host"] if "sum" in key])
        self.assertFalse([key for row in result["named_cgroups"].values() for key in row if "sum" in key])
        self.assertFalse([key for key in result if "sum" in key])
        self.assertIn("not a continuous maximum", blob)
        self.assertIn("| Показатель | Байт |", summarize_tool.markdown(result))

    def test_limit_written_after_the_first_reading_is_not_reported_as_unlimited(self):
        early = {"bbbbbbbbbbbb": {"current": 3_000_000, "peak": 3_000_000, "max": None, "events": {"oom_kill": 0}}}
        later = {"bbbbbbbbbbbb": {"current": 20_000_000, "peak": 25_000_000, "max": 1610612736, "events": {"oom_kill": 0}}}
        result = summarize_tool.summarize(META, [sample(0.0, 3e9, 3e9, containers=early),
                                                 sample(0.5, 3e9, 3e9, containers=later)], [])
        row = result["containers"]["bbbbbbbbbbbb"]
        self.assertEqual((row["limit_bytes"], row["samples_without_limit"], row["samples"]), (1610612736, 1, 2))

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


RUN = TOOLS / "logs" / "2026-11-20-wsl2-rc3"
SHA = "b7a1857a51bf47bb36e42699988b38b4d897fa1f"


def rewrite_json(path: Path, mutate):
    value = json.loads(path.read_text(encoding="utf-8"))
    result = mutate(value)
    if isinstance(result, dict):  # in-place mutators may return anything else (e.g. a list of None)
        value = result
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def rewrite_samples(run: Path, mutate):
    """Change the RAW samples and regenerate summary.json from them (consistent, not a stale summary)."""
    path = run / "memory" / "samples.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = mutate(rows)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert summarize_tool.main([str(run / "memory"), "--phases", str(run / "phases.jsonl"),
                                "--out", str(run / "memory" / "summary.json")]) == 0


def report_path(run: Path) -> Path:
    return next((run / "bundle" / "results").glob("verify-*")) / "verify" / "report.json"


class Verdict(unittest.TestCase):
    """Negative controls on copies of the real RC3 review (docs/reviews/whole-host-resources/logs)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / "run"
        shutil.copytree(RUN, self.run_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def verdict(self):
        return judge_tool.judge(self.run_dir, SHA)

    def test_real_run_stays_confirmed(self):
        result = self.verdict()
        self.assertEqual((result["verdict"], result["failed"], result["not_checked"]), ("confirmed", [], []))
        self.assertEqual(result["facts"]["memory"]["samples"], 1215)
        self.assertEqual(len(result["facts"]["acceptance"]), 24)

    # --- the four controls from the PR #32 review ---------------------------------------------------
    def test_review_control_1_emptied_summary_is_not_confirmed(self):
        def empty(s):
            s.update(samples=0, duration_s=0, containers={})
            s["gaps_s"]["max"] = None
            for key in ("sampled_peak_used_excluding_reclaimable", "sampled_peak_used_including_cache",
                        "sampled_min_mem_available", "sampled_peak_swap_used"):
                s["host"][key] = None
            s["oom"] = {key: None for key in s["oom"]}
        rewrite_json(self.run_dir / "memory" / "summary.json", empty)
        result = self.verdict()
        self.assertEqual(result["verdict"], "failed")
        self.assertTrue(any("disagrees with the raw samples" in text for text in result["failed"]))
        (self.run_dir / "memory" / "samples.jsonl").unlink()  # and without raw samples: nothing to judge
        self.assertEqual(self.verdict()["verdict"], "not_checked")

    def test_review_control_2_memory_above_the_profile(self):
        def big(s):
            s["host"]["mem_total_bytes"] = 16_000_000_000
            s["host"]["sampled_peak_used_excluding_reclaimable"]["bytes"] = 8_000_000_000
        rewrite_json(self.run_dir / "memory" / "summary.json", big)
        self.assertEqual(self.verdict()["verdict"], "failed")  # stale summary vs raw samples
        rewrite_samples(self.run_dir, lambda rows: [
            {**row, "meminfo": {**row["meminfo"], "MemTotal": 16_000_000_000}} for row in rows])
        result = self.verdict()  # consistent raw data, but above the limit and unlike qualification.json
        self.assertEqual(result["verdict"], "failed")
        self.assertTrue(any("exceeds the 4000000000 B profile" in text for text in result["failed"]))
        self.assertTrue(any("differs from qualification" in text for text in result["failed"]))

    def test_review_control_3_incomplete_acceptance_matrix(self):
        rewrite_json(report_path(self.run_dir), lambda r: r.update(acceptance=[{"id": "OPS-02", "status": "not_checked"}]))
        result = self.verdict()
        self.assertEqual(result["verdict"], "not_checked")
        self.assertTrue(any("missing from report.json" in text and "API-01" in text for text in result["not_checked"]))

    def test_review_control_4_evidence_without_the_positive_status(self):
        (self.run_dir / "evidence-summary.json").write_text('{"status": "not_checked"}', encoding="utf-8")
        result = self.verdict()
        self.assertEqual(result["verdict"], "not_checked")
        self.assertTrue(any("evidence summary fields missing" in text for text in result["not_checked"]))

    def test_recording_that_stops_before_the_rehearsal_is_not_checked(self):
        meta = json.loads((self.run_dir / "memory" / "meta.json").read_text(encoding="utf-8"))
        rehearsal = next(json.loads(line) for line in (self.run_dir / "phases.jsonl").read_text().splitlines()
                         if '"rehearsal"' in line)
        cut = rehearsal["start_mono"] - meta["monotonic_start"]
        rewrite_samples(self.run_dir, lambda rows: [row for row in rows if row["t"] < cut])
        result = self.verdict()
        self.assertEqual(result["verdict"], "not_checked")
        self.assertTrue(any("does not cover every step" in text and "rehearsal" in text for text in result["not_checked"]))

    def test_gap_inside_a_step_is_not_checked(self):
        meta = json.loads((self.run_dir / "memory" / "meta.json").read_text(encoding="utf-8"))
        rehearsal = next(json.loads(line) for line in (self.run_dir / "phases.jsonl").read_text().splitlines()
                         if '"rehearsal"' in line)
        middle = (rehearsal["start_mono"] + rehearsal["end_mono"]) / 2 - meta["monotonic_start"]
        rewrite_samples(self.run_dir, lambda rows: [row for row in rows if not middle - 30 < row["t"] < middle + 30])
        self.assertEqual(self.verdict()["verdict"], "not_checked")

    # --- other proven violations and missing data --------------------------------------------------
    def test_oom_kill_in_raw_samples_fails(self):
        def kill(rows):
            rows[-1] = {**rows[-1], "vmstat": {**rows[-1]["vmstat"], "oom_kill": rows[-1]["vmstat"]["oom_kill"] + 1}}
            return rows
        rewrite_samples(self.run_dir, kill)
        result = self.verdict()
        self.assertEqual(result["verdict"], "failed")
        self.assertTrue(any("OOM kills recorded" in text for text in result["failed"]))

    def test_qualification_flag_is_not_trusted(self):
        rewrite_json(self.run_dir / "qualification.json", lambda q: q["facts"].update(affinity_cpus=8))
        self.assertEqual(self.verdict()["verdict"], "failed")  # flag says qualified, facts say 8 CPUs
        rewrite_json(self.run_dir / "qualification.json", lambda q: q.update(qualified=False))
        self.assertEqual(self.verdict()["verdict"], "not_checked")

    def test_other_defects_fail(self):
        cases = {
            "image_pull": lambda run: (run / "docker-events.log").write_text(
                (run / "docker-events.log").read_text(encoding="utf-8") + '{"Type":"image","Action":"pull"}\n',
                encoding="utf-8"),
            "extra_open_group": lambda run: rewrite_json(report_path(run), lambda r: [
                row.update(status="not_checked") for row in r["acceptance"] if row["id"] == "ISO-01"]),
            "failed_group": lambda run: rewrite_json(report_path(run), lambda r: [
                row.update(status="failed") for row in r["acceptance"] if row["id"] == "MAT-01"]),
            "wrong_source": lambda run: rewrite_json(run / "evidence-summary.json",
                                                     lambda e: e.update(source_revision="0" * 40)),
            "rehearsal_exit_1": lambda run: (run / "phases.jsonl").write_text(
                (run / "phases.jsonl").read_text().replace('"phase":"rehearsal","exit_code":2',
                                                           '"phase":"rehearsal","exit_code":1')),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                shutil.rmtree(self.run_dir)
                shutil.copytree(RUN, self.run_dir)
                mutate(self.run_dir)
                self.assertEqual(self.verdict()["verdict"], "failed", name)

    def test_missing_step_is_not_checked(self):
        path = self.run_dir / "phases.jsonl"
        path.write_text("".join(line + "\n" for line in path.read_text().splitlines() if '"replay_check"' not in line))
        self.assertEqual(self.verdict()["verdict"], "not_checked")

    def test_docker_events_multiline_and_json(self):
        text_format = ("1795000000 container exec_start: python3 -c \"import json\n"
                       "print(' image pull ')\" abc controller img\n"
                       "1795000001 network connect abc none\n")
        self.assertEqual(judge_tool.docker_events(text_format), [("container", "exec_start:"), ("network", "connect")])
        json_format = '{"Type":"image","Action":"pull","Actor":{"ID":"x"}}\n{"Type":"container","Action":"start"}\n'
        self.assertEqual(judge_tool.docker_events(json_format), [("image", "pull"), ("container", "start")])


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
