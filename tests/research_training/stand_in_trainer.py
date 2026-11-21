"""TEST DOUBLE: stand-in for `python -m auditlab.research.training.local_lora run`.

It trains nothing. It writes step-numbered fake checkpoints and a result.json through the same helpers
the real trainer uses, so the backend's job state machine can be tested without a GPU. Behaviour comes
from environment variables set by the test: AUDITLAB_STAND_IN_SECONDS, AUDITLAB_STAND_IN_FAIL.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from auditlab.research.training.local_lora import record_checkpoint, write_result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    config = json.loads(Path(parser.parse_args().config).read_text(encoding="utf-8"))
    job_dir, job_id = Path(config["job_dir"]), config["job_id"]
    with open(job_dir / "launches.log", "a", encoding="utf-8") as log:
        log.write(f"{os.getpid()}\n")
    seconds = float(os.environ.get("AUDITLAB_STAND_IN_SECONDS", "0.3"))
    for step in (10, 20, 30):
        time.sleep(seconds / 3)
        path = job_dir / "trainer" / f"checkpoint-{step}"
        path.mkdir(parents=True, exist_ok=True)
        record_checkpoint(job_dir, job_id, step, path, {"loss": round(1.0 / step, 4), "stand_in": True})
    if os.environ.get("AUDITLAB_STAND_IN_FAIL") == "1":
        write_result(job_dir, "failed", exit_code=1, error={"code": "StandInFailure", "message": "scripted failure"})
        return 1
    adapter = job_dir / "adapter"
    adapter.mkdir(exist_ok=True)
    (adapter / "adapter_config.json").write_text(json.dumps({"stand_in": True}), encoding="utf-8")
    record_checkpoint(job_dir, job_id, 30, adapter, {"stand_in": True}, final=True)
    write_result(job_dir, "succeeded", exit_code=0, lora_module_name=config["lora_module_name"], adapter_path=str(adapter))
    return 0


if __name__ == "__main__":
    sys.exit(main())
