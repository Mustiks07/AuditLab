#!/usr/bin/env bash
# OPS-01 whole-host review of the published RC3 offline bundle (docs/demo/HANDOFF.md, sections 1-8).
#
#   bash run_review.sh --work DIR [--download | --zip ZIP --sums SUMS] [--interval 0.5]
#                      [--baseline-seconds 30] [--python python3.11] [--allow-unqualified]
#
# Runs on the machine under test (Linux x86_64, Python 3.11, local Docker Engine, cgroup v2).
# It installs nothing, builds nothing, pulls nothing, and never removes images or containers it
# did not start. Everything goes to DIR/review-<UTC>/:
#   environment/     OS, Python, Docker, cgroup, image store, CPU/RAM/swap as reported
#   qualification.json  2 CPU, MemTotal <= 4 000 000 000 B, no swap, ... (unqualified -> exit 3)
#   preload-images.json project images absent before loading (present -> exit 4, nothing removed)
#   steps/*.out, phases.jsonl  each HANDOFF step: command, exit code, monotonic start/end
#   memory/          collect_host_memory.py samples for the whole run; summary.json, table.md
#   docker-events.log  docker events during the run (no image pull expected)
#   result.json      verdict: confirmed / failed / not_checked, with the reasons
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="v0.1.0-rc.3"
SOURCE_SHA="b7a1857a51bf47bb36e42699988b38b4d897fa1f"
ZIP_SHA256="a5363412f4509b7ae39aa21aa9eee27154e2af5357347f7d02d7dc31ddb52b81"
ZIP_NAME="AuditLab-${TAG}-offline.zip"
SUMS_NAME="AuditLab-${TAG}-SHA256SUMS.txt"
BASE_URL="https://github.com/Mustiks07/AuditLab/releases/download/${TAG}"
LIMIT_BYTES=4000000000
PY=python3.11
DOCKER=(docker --host unix:///var/run/docker.sock)

WORK="" ZIP="" SUMS="" DOWNLOAD=0 INTERVAL=0.5 BASELINE=30 ALLOW_UNQUALIFIED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --work) WORK="$2"; shift 2 ;;
    --zip) ZIP="$2"; shift 2 ;;
    --sums) SUMS="$2"; shift 2 ;;
    --download) DOWNLOAD=1; shift ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --baseline-seconds) BASELINE="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;   # only to dry-run this tooling; the profile requires 3.11
    --allow-unqualified) ALLOW_UNQUALIFIED=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
[ -n "$WORK" ] || { echo "--work DIR is required" >&2; exit 64; }
command -v "$PY" >/dev/null || { echo "$PY not found; python3.11 is required (HANDOFF section 1)" >&2; exit 64; }

mkdir -p "$WORK" || exit 64
RUN="$(cd "$WORK" && pwd)/review-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN/environment" "$RUN/steps"
exec > >(tee -a "$RUN/console.log") 2>&1
echo "review directory: $RUN"
mono() { "$PY" -c 'import time; print(time.monotonic())'; }

# ------------------------------------------------------------------ 1. environment
env_cmd() { local name="$1"; shift; { echo "\$ $*"; "$@"; echo "exit=$?"; } > "$RUN/environment/$name.txt" 2>&1; }
env_cmd uname uname -srm
env_cmd os-release cat /etc/os-release
env_cmd nproc nproc
env_cmd affinity taskset -pc $$
env_cmd meminfo cat /proc/meminfo
env_cmd free free -b
env_cmd swap swapon --show --bytes
env_cmd virt systemd-detect-virt
env_cmd cgroup stat -fc %T /sys/fs/cgroup
env_cmd cmdline cat /proc/cmdline
env_cmd python "$PY" -VV
env_cmd docker-version "${DOCKER[@]}" version
env_cmd docker-info "${DOCKER[@]}" info
env_cmd docker-store "${DOCKER[@]}" info --format '{{.Driver}} {{.DriverStatus}} cgroup={{.CgroupVersion}} driver={{.CgroupDriver}}'
env_cmd docker-df "${DOCKER[@]}" system df
env_cmd docker-ps "${DOCKER[@]}" ps --all --format '{{.ID}} {{.Image}} {{.Status}} {{.Names}}'
env_cmd docker-images "${DOCKER[@]}" image ls --all --digests
[ -r /proc/sys/kernel/osrelease ] && grep -qi microsoft /proc/sys/kernel/osrelease && env_cmd wsl cat /proc/sys/kernel/osrelease

"$PY" - "$RUN" "$LIMIT_BYTES" <<'PY'
import json, os, platform, subprocess, sys
run, limit = sys.argv[1], int(sys.argv[2])
info = {k.strip(): v.split() for k, v in (l.split(":", 1) for l in open("/proc/meminfo") if ":" in l)}
kb = lambda k: int(info[k][0]) * 1024 if k in info else None
def docker(fmt):
    try:
        return subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", "info", "--format", fmt],
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
facts = {"machine": platform.machine(), "mem_total_bytes": kb("MemTotal"), "swap_total_bytes": kb("SwapTotal"),
         "affinity_cpus": len(os.sched_getaffinity(0)), "online_cpus": os.cpu_count(),
         "python": platform.python_version(), "cgroup2": os.path.isfile("/sys/fs/cgroup/cgroup.controllers"),
         "docker_server": docker("{{.ServerVersion}}"), "docker_cgroup": docker("{{.CgroupVersion}}"),
         "wsl": "microsoft" in platform.release().lower()}
checks = {
    "x86_64": facts["machine"] == "x86_64",
    "two_cpus": facts["affinity_cpus"] == 2,
    "mem_total_le_limit": facts["mem_total_bytes"] is not None and facts["mem_total_bytes"] <= limit,
    "swap_disabled": facts["swap_total_bytes"] == 0,
    "python_3_11": facts["python"].startswith("3.11."),
    "cgroup_v2": facts["cgroup2"] and facts["docker_cgroup"] == "2",
    "docker_engine_reachable": bool(facts["docker_server"]),
}
result = {"limit_bytes": limit, "facts": facts, "checks": checks, "qualified": all(checks.values()),
          "note": "MemTotal is the guest kernel view and is below the RAM the hypervisor assigned; record the "
                  "assigned size from the hypervisor configuration separately."}
json.dump(result, open(f"{run}/qualification.json", "w"), indent=2)
print("qualification:", json.dumps(checks))
sys.exit(0 if result["qualified"] else 3)
PY
QUALIFIED=$?
if [ "$QUALIFIED" -ne 0 ] && [ "$ALLOW_UNQUALIFIED" -ne 1 ]; then
  echo "machine does not satisfy the OPS-01 profile: status stays 'not checked' (see qualification.json)"
  exit 3
fi

# ------------------------------------------------------------------ 3. archive and hash
cd "$RUN"
if [ "$DOWNLOAD" -eq 1 ]; then
  curl -fsSL -o "$ZIP_NAME" "$BASE_URL/$ZIP_NAME" && curl -fsSL -o "$SUMS_NAME" "$BASE_URL/$SUMS_NAME" \
    || { echo "download failed"; exit 1; }
  ZIP="$RUN/$ZIP_NAME"; SUMS="$RUN/$SUMS_NAME"
else
  [ -f "$ZIP" ] && [ -f "$SUMS" ] || { echo "--zip and --sums (or --download) are required"; exit 64; }
  cp "$ZIP" "$RUN/$ZIP_NAME" && cp "$SUMS" "$RUN/$SUMS_NAME"
fi
{ sha256sum "$ZIP_NAME"; sha256sum -c "$SUMS_NAME" --ignore-missing; echo "exit=$?"; } > "$RUN/archive-hash.txt" 2>&1
cat "$RUN/archive-hash.txt"
grep -q "^${ZIP_SHA256}  ${ZIP_NAME}\$" "$RUN/archive-hash.txt" && grep -q "^${ZIP_NAME}: OK\$" "$RUN/archive-hash.txt" \
  || { echo "archive hash does not match the published RC3"; exit 1; }
mkdir bundle && unzip -q "$ZIP_NAME" -d bundle || { echo "unzip failed"; exit 1; }
cd bundle

# ------------------------------------------------------------------ project images must be absent before loading
"$PY" - "$RUN" <<'PY'
import json, subprocess, sys
run = sys.argv[1]
manifest = json.load(open("manifest.json"))
rows, present = [], []
for role, image in manifest["images"].items():
    for kind in ("id", "oci_manifest_digest", "tag"):
        ref = image.get(kind)
        if not ref:
            continue
        found = subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", "image", "inspect", ref],
                               capture_output=True, text=True).returncode == 0
        rows.append({"role": role, "kind": kind, "ref": ref, "present": found})
        present += [ref] if found else []
json.dump({"source_revision": manifest.get("source_revision"), "checked": rows, "present": present},
          open(f"{run}/preload-images.json", "w"), indent=2)
print("project images present before loading:", present or "none")
sys.exit(4 if present else 0)
PY
[ $? -eq 0 ] || { echo "project images already exist; nothing was removed. Use a clean host."; exit 4; }

# ------------------------------------------------------------------ measurement + events
"$PY" "$HERE/collect_host_memory.py" --out "$RUN/memory" --interval "$INTERVAL" &
COLLECTOR=$!
"${DOCKER[@]}" events --format '{{.Time}} {{.Type}} {{.Action}} {{.Actor.ID}} {{.Actor.Attributes.name}} {{.Actor.Attributes.image}}' \
  > "$RUN/docker-events.log" 2>&1 &
EVENTS=$!
DEMO_NAME="auditlab-ops01-demo-$$"
finish() {
  "${DOCKER[@]}" stop "$DEMO_NAME" >/dev/null 2>&1 || true   # only the container this script started
  kill -TERM "$COLLECTOR" 2>/dev/null; wait "$COLLECTOR" 2>/dev/null
  kill -TERM "$EVENTS" 2>/dev/null; wait "$EVENTS" 2>/dev/null
}
trap finish EXIT
sleep 2

phase() {  # phase NAME COMMAND... : record command, exit code and CLOCK_MONOTONIC bounds
  local name="$1"; shift
  local start end rc
  start=$(mono)
  { echo "\$ $*"; } > "$RUN/steps/$name.out"
  "$@" >> "$RUN/steps/$name.out" 2>&1
  rc=$?
  end=$(mono)
  printf '{"phase":"%s","exit_code":%d,"start_mono":%s,"end_mono":%s}\n' "$name" "$rc" "$start" "$end" >> "$RUN/phases.jsonl"
  echo "[$name] exit=$rc ($(awk "BEGIN{printf \"%.1f\", $end-$start}") s)"
  return 0
}

phase baseline_idle sleep "$BASELINE"
phase check "$PY" offline.py check
phase rehearsal "$PY" offline.py verify --suite rehearsal --output-root ./results
phase image_runtime "$PY" offline.py image --role runtime
RESULTS=$(ls -d ./results/verify-* 2>/dev/null | tail -n 1)
mkdir -p source && tar -xzf source.tar.gz -C source
phase evidence_checker "$PY" -I source/tools/check_rehearsal_evidence.py "$RESULTS" --bundle . --output "$RUN/evidence-summary.json"
if [ -n "$RESULTS" ] && [ -f "$RESULTS/launch.json" ]; then
  LAUNCH_ID=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("launch_id",""))' "$RESULTS/launch.json")
  phase launch_cleanup "${DOCKER[@]}" ps --all --filter "label=org.auditlab.launch=$LAUNCH_ID" --format '{{.ID}} {{.Names}}'
fi

RUNTIME_IMAGE=$(tail -n 1 "$RUN/steps/image_runtime.out")
mkdir -p demo-results
phase demo_start "${DOCKER[@]}" run -d --rm --name "$DEMO_NAME" --pull never --user "$(id -u):$(id -g)" --read-only \
  --cap-drop ALL --security-opt no-new-privileges --cpus 2 --memory 2g --tmpfs /tmp:rw,nosuid,size=1g \
  -p 127.0.0.1:8765:8765 --mount "type=bind,src=$(pwd)/demo-results,dst=/results" \
  "$RUNTIME_IMAGE" python run.py demo --host 0.0.0.0 --root /results/demo
wait_health() {
  for _ in $(seq 1 120); do
    if curl -fsS http://127.0.0.1:8765/demo/health; then echo; return 0; fi
    sleep 2
  done
  return 1
}
phase demo_health wait_health
phase demo_runs curl -fsS http://127.0.0.1:8765/demo/runs
RUN_ID=$("$PY" - "$RUN/steps/demo_runs.out" <<'PY'
import json, sys
text = open(sys.argv[1]).read().split("\n", 1)[1]
data = json.loads(text)
runs = data.get("runs", data) if isinstance(data, dict) else data
print(next((r.get("run_id") for r in runs if isinstance(r, dict) and r.get("run_id")), ""))
PY
)
CHECK_ARGS=(run --rm --pull never --network none --user "$(id -u):$(id -g)" --read-only --cap-drop ALL
  --security-opt no-new-privileges --cpus 2 --memory 1536m --memory-swap 1536m --pids-limit 256
  --tmpfs /tmp:rw,size=64m --mount "type=bind,src=$(pwd)/demo-results,dst=/results" "$RUNTIME_IMAGE")
phase crosscheck_live "${DOCKER[@]}" "${CHECK_ARGS[@]}" python -m auditlab.demo --root /results/demo crosscheck --live
if [ -n "$RUN_ID" ]; then
  phase replay_check "${DOCKER[@]}" "${CHECK_ARGS[@]}" python -m auditlab.demo --root /results/demo replay --run "$RUN_ID" --check
fi
phase demo_stop "${DOCKER[@]}" stop "$DEMO_NAME"
sleep 2
finish
trap - EXIT

# ------------------------------------------------------------------ summary and verdict
"$PY" "$HERE/summarize_host_memory.py" "$RUN/memory" --phases "$RUN/phases.jsonl" --limit-bytes "$LIMIT_BYTES" \
  --out "$RUN/memory/summary.json" --markdown "$RUN/memory/table.md"
"$PY" "$HERE/judge_review.py" "$RUN" --source-sha "$SOURCE_SHA"
exit $?
