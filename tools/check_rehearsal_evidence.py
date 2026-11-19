"""Check internal rehearsal evidence; never certifies the 24 acceptance groups or a contest score."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from offline import verify_bundle

SUITES = {"foundation", "integration", "simulator", "evaluator", "runner", "verifier", "metamorphic", "demo"}
POLICIES = {"random", "reconcile", "flag_all", "oracle"}
GIB = 1024 ** 3


def cpus(value):
    result = set()
    for part in str(value).split(","):
        bounds = [int(n) for n in part.split("-")]
        if len(bounds) > 2 or bounds[-1] < bounds[0] or bounds[-1] - bounds[0] > 65536:
            raise ValueError("invalid CPU range")
        result.update(range(bounds[0], bounds[-1] + 1))
    return result


def check(root, bundle=None, expected_revision=None):
    failures, missing = [], []
    summary = {"scope": "Internal functional rehearsal and resource evidence only; 24 acceptance groups and official score not certified"}
    root = Path(root).resolve()
    if not (root / "launch.json").is_file():
        choices = [p for p in root.glob("verify-*") if (p / "launch.json").is_file()]
        if len(choices) != 1:
            return {**summary, "status": "not_checked", "exit_code": 2,
                    "failures": [], "missing": ["supply one unique attempt directory or a parent containing exactly one"]}
        root = choices[0]
    summary["result_root"] = str(root)

    def read(relative):
        path = root / relative
        if not path.is_file():
            missing.append(str(relative)); return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected JSON object")
            return data
        except (OSError, ValueError) as error:
            failures.append(f"invalid {relative}: {error}"); return {}

    def require(condition, reason):
        if not condition: failures.append(reason)

    def passed(rows, expected, label):
        if len(rows) != expected:
            missing.append(f"{label}: expected {expected} rows, found {len(rows)}")
        if len({row.get("id") for row in rows}) != len(rows):
            failures.append(f"{label}: duplicate IDs")
        for row in rows:
            if row.get("status") == "failed": failures.append(f"{label}/{row.get('id')}: failed")
            elif row.get("status") != "passed": missing.append(f"{label}/{row.get('id')}: not passed")

    def resources(data, label, expected_cpus, pids):
        if data.get("status") != "measured" or data.get("resource_limits_verified") is not True:
            missing.append(f"{label}: container resource limits not independently verified"); return
        require(data.get("memory_limit_bytes") == 2 * GIB, f"{label}: memory limit differs from 2 GiB")
        quota, period = data.get("cpu_quota_usec"), data.get("cpu_period_usec")
        require(isinstance(quota, (int, float)) and isinstance(period, (int, float)) and
                period > 0 and quota == 2 * period, f"{label}: CPU quota differs from 2")
        try:
            require(cpus(data.get("cpuset_cpus")) == expected_cpus, f"{label}: cpuset differs")
        except (TypeError, ValueError): failures.append(f"{label}: invalid cpuset")
        peak = data.get("peak_memory_bytes")
        require(isinstance(peak, int) and not isinstance(peak, bool) and 0 <= peak <= 2 * GIB,
                f"{label}: missing or excessive memory peak")
        require(data.get("scope") == "container_limits_verified", f"{label}: unverified cgroup scope")
        require(not data.get("limit_mismatches"), f"{label}: resource limit mismatches")
        usage = data.get("cpu_usage_usec")
        require(isinstance(usage, (int, float)) and not isinstance(usage, bool)
                and math.isfinite(usage) and usage >= 0, f"{label}: CPU usage unavailable")
        # Shared measurement validates swap and affinity as well.
        actual_pids = (data.get("raw_observed") or {}).get("files", {}).get("pids.max")
        if actual_pids is None:
            missing.append(f"{label}: actual kernel PID limit unavailable")
        else:
            require(str(actual_pids).strip() == str(pids), f"{label}: actual PID limit differs")

    launch = read("launch.json")
    general = read("verify/report.json")
    rehearsal = read("verify/rehearsal/report.json")
    controller = read("controller-resources.json")
    if "loaded_images" in launch:
        loaded, original = launch["loaded_images"], launch.get("images", {})
        require(set(loaded) == set(original) == {"runtime", "verifier", "policy"}, "loaded image roles differ")
        for role, image in loaded.items():
            source = original.get(role, {})
            require(image.get("source_id") == source.get("id") and image.get("id") is not None and
                    image.get("id") in (source.get("id"), source.get("oci_manifest_digest")),
                    f"{role}: loaded image is not an immutable bundle identity")
        for role in ("policy", "verifier"):
            require(rehearsal.get(role + "_image") == loaded.get(role, {}).get("id"),
                    f"{role}: rehearsal image differs from loaded image")
    revision = launch.get("source_revision")
    summary["source_revision"] = revision
    for label, value in (("launch", revision), ("test report", general.get("commit"))):
        if value is None:
            missing.append(f"{label}: source revision unavailable")
        else:
            require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None,
                    f"{label}: invalid source revision")
    if revision is not None and general.get("commit") is not None:
        require(general["commit"] == revision, "test report/launch revision differs")
    if expected_revision is not None:
        require(isinstance(expected_revision, str) and re.fullmatch(r"[0-9a-f]{40}", expected_revision) is not None,
                "invalid expected revision")
        require(revision == expected_revision, "evidence belongs to another source revision")
    if launch.get("resource_controls", {}).get("status") != "available":
        missing.append("Docker daemon resource-control preflight unavailable")
    if launch.get("child_exit_code", launch.get("exit_code")) is None:
        missing.append("verification exit code unavailable")
    elif launch.get("child_exit_code", launch.get("exit_code")) not in (0, 2):
        failures.append("verification process failed or did not finish")
    try:
        selected = cpus(launch.get("cpu_set"))
        require(len(selected) == 2, "exactly two shared CPUs required")
    except (TypeError, ValueError):
        selected = set(); missing.append("selected cpuset unavailable")
    checks = general.get("checks", [])
    passed(checks, len(SUITES), "suites")
    if checks and not {row.get("id") for row in checks} <= SUITES:
        failures.append("unexpected functional suite IDs")
    for row in checks:
        records = row.get("records", [])
        if row.get("tests_run", 0) <= 0 or not records:
            missing.append(f"suite {row.get('id')}: no executed test evidence")
        for record in records:
            if record.get("status") == "failed": failures.append(f"test {record.get('id')}: failed")
            elif record.get("status") != "passed": missing.append(f"test {record.get('id')}: skipped or not checked")
    datasets, attempts, pairs = (rehearsal.get(key, []) for key in ("datasets", "episodes", "pairs"))
    for rows, count, label in ((datasets, 20, "datasets"), (attempts, 80, "attempts"), (pairs, 20, "pairs")):
        passed(rows, count, label)
    expected_episodes = {f"internal-{i:02d}" for i in range(1, 21)}
    require({r.get("id") for r in datasets} <= expected_episodes, "unexpected dataset IDs")
    require({r.get("id") for r in attempts} <= {f"{p}/{e}" for p in POLICIES for e in expected_episodes}, "unexpected policy/episode IDs")
    require({r.get("id") for r in pairs} <= {f"pair-{i:02d}" for i in range(1, 21)}, "unexpected pair IDs")
    resources(controller, "controller", selected, 256)
    for label, rows in (("dataset", datasets), ("pair", pairs)):
        for row in rows:
            independent = row.get("independent") or {}
            resources(independent.get("resources") or {},
                      f"{label}/{row.get('id')}/verifier", selected, 128)
    ordinary = [row for row in attempts if row.get("policy") in POLICIES - {"oracle"}]
    if len(ordinary) != 60:
        missing.append("expected 60 ordinary container runs")
    for row in attempts:
        require(str(row.get("id", "")).split("/")[0] == row.get("policy"), "attempt policy differs from ID")
    for row in attempts:
        elapsed = row.get("elapsed_seconds")
        require(isinstance(elapsed, (int, float)) and math.isfinite(elapsed) and 0 <= elapsed <= 60,
                f"{row.get('id')}: missing or >60s elapsed time")
    for row in ordinary:
        resources(row.get("resources") or {}, row.get("id"), selected, 128)
    if bundle is not None:
        bundle = Path(bundle)
        try:
            manifest = verify_bundle(bundle)
            total = sum((bundle / name).stat().st_size for name in manifest["files"])
            summary["payload_size_bytes"] = total
            require(total == manifest.get("payload_size_bytes"), "bundle payload size differs from manifest")
            require(total < 1_000_000_000, "bundle payload exceeds decimal 1 GB")
            require(manifest.get("source_revision") == launch.get("source_revision"), "bundle/report revision differs")
            require(manifest["images"] == launch.get("images"), "bundle/launch images differ")
        except OSError as error:
            missing.append(f"bundle evidence unavailable: {error}")
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            failures.append(f"invalid bundle evidence: {error}")
    summary.update(datasets=len(datasets), attempts=len(attempts), pairs=len(pairs), ordinary_resource_runs=len(ordinary), verifier_resource_runs=len(datasets) + len(pairs),
                   failures=failures, missing=missing)
    code = 1 if failures else 2 if missing else 0
    summary.update(status="failed" if code == 1 else "not_checked" if code == 2 else "internal_rehearsal_passed", exit_code=code)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_root", type=Path, nargs="?")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--expected-revision", help="full Git SHA obtained independently of the evidence")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result_root = args.results or args.result_root
    if result_root is None:
        parser.error("provide --results DIR or a positional result directory")
    summary = check(result_root, args.bundle, args.expected_revision)
    text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
