"""local_lora backend: preflight gates, dry run, detached job state machine (stand-in trainer = TEST DOUBLE)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from training_fixtures import make_attempt, make_curriculum  # noqa: E402

from auditlab.research.training.dataset import build_sft  # noqa: E402
from auditlab.research.training.local_lora import REQUIRED_MODULES, LocalLoraBackend, to_trl_row  # noqa: E402
from auditlab.research.training.train import train  # noqa: E402

STAND_IN = [sys.executable, str(HERE / "stand_in_trainer.py")]


class LocalLoraTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for n in range(1, 13):
            make_attempt(self.root / "exp", n)
        self.manifest = build_sft([self.root / "exp"], self.root / "dataset")
        self.curriculum = make_curriculum(self.root / "curriculum.json", [f"ep-{n:03d}-train" for n in range(1, 13)])
        self.jobs = self.root / "jobs"
        os.environ["AUDITLAB_STAND_IN_SECONDS"] = "0.3"
        os.environ.pop("AUDITLAB_STAND_IN_FAIL", None)

    def tearDown(self):
        os.environ.pop("AUDITLAB_STAND_IN_SECONDS", None)
        os.environ.pop("AUDITLAB_STAND_IN_FAIL", None)
        self.temp.cleanup()

    def config(self, **overrides) -> Path:
        config = {"schema_version": "1.0", "protocol_id": "auditlab-research-600s", "provider": "local_lora",
                  "base_model": "Qwen/Qwen2.5-0.5B-Instruct", "dataset": str(self.root / "dataset"),
                  "hyperparameters": {"n_epochs": 1, "learning_rate": 1e-4}, "lora": {"r": 8, "alpha": 16},
                  "seed": 3, "jobs_root": str(self.jobs), "lora_module_name": "auditlab-lora-test",
                  "poll_interval_s": 1}
        config.update(overrides)
        path = self.root / f"config-{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def run_train(self, config, out="run", **kwargs) -> dict:
        return json.loads(train(config, self.curriculum, self.root / out, **kwargs).read_text(encoding="utf-8"))

    def launches(self) -> list[str]:
        return [line for p in self.jobs.glob("lora-*/launches.log") for line in p.read_text().splitlines()]

    def test_missing_dependencies_never_start_a_process(self):
        backend = LocalLoraBackend(jobs_root=self.jobs, find_spec=lambda name: None)
        record = json.loads(train(self.config(), self.curriculum, self.root / "run", backend=backend).read_text())
        self.assertEqual(record["status"], "not_started_missing_access")
        for name in REQUIRED_MODULES:
            self.assertTrue(any(repr(name) in m for m in record["missing_conditions"]), name)
        self.assertTrue(any("CUDA" in m for m in record["missing_conditions"]))
        self.assertFalse(self.jobs.exists())
        self.assertEqual((record["provider"], record["mock"], record["job_id"], record["result_model"]),
                         ("local_lora", False, None, None))

    def test_this_machine_reports_missing_access(self):
        if all(importlib.util.find_spec(name) for name in REQUIRED_MODULES):
            self.skipTest("GPU training stack is installed here; covered by the injected-finder test")
        record = self.run_train(self.config())
        self.assertEqual(record["status"], "not_started_missing_access")
        self.assertFalse(self.jobs.exists())

    def test_dry_run_starts_no_process(self):
        record = self.run_train(self.config(trainer_command=STAND_IN), dry_run=True)
        self.assertEqual((record["status"], record["mock"]), ("dry_run", True))
        self.assertFalse(self.jobs.exists())
        launch = record["planned_requests"][1]
        self.assertEqual(launch["command"][:2], STAND_IN)
        self.assertTrue(launch["job_id"].startswith("lora-"))
        real = self.run_train(self.config(), out="dry-real", dry_run=True)
        self.assertEqual(real["status"], "dry_run")
        self.assertIn("facts", real["preflight"])
        self.assertFalse(self.jobs.exists())

    def test_job_state_machine_and_resume_never_relaunches(self):
        os.environ["AUDITLAB_STAND_IN_SECONDS"] = "3"
        config = self.config(trainer_command=STAND_IN)
        first = self.run_train(config)  # max_wait 0: launch, poll once
        self.assertEqual(first["status"], "running")
        job_id = first["job_id"]
        state = json.loads((self.jobs / job_id / "job_state.json").read_text())
        self.assertTrue(state["pid"] and state["log_path"] and state["started_at"])
        self.assertEqual(self.run_train(config)["job_id"], job_id)  # resume while running
        final = self.run_train(config, max_wait_s=60)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual((final["result_model"], final["mock"], final["provider"], final["claimed_as"]),
                         ("auditlab-lora-test", True, "local_lora", "training"))
        self.assertEqual(final["checkpoint_id"], f"{job_id}:final-30")
        self.assertEqual([c["step_number"] for c in final["checkpoints"]], [10, 20, 30, 30])
        self.assertEqual(self.run_train(config)["status"], "succeeded")
        # A second output dir with the same dataset/settings adopts the same job instead of training again.
        self.assertEqual(self.run_train(config, out="other-run", max_wait_s=5)["job_id"], job_id)
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(json.loads((self.jobs / job_id / "job_state.json").read_text())["exit_code"], 0)

    def test_failed_trainer_is_recorded_and_not_relaunched(self):
        os.environ["AUDITLAB_STAND_IN_FAIL"] = "1"
        config = self.config(trainer_command=STAND_IN)
        record = self.run_train(config, max_wait_s=60)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["result"]["error"]["code"], "StandInFailure")
        self.assertIsNone(record["result_model"])
        os.environ.pop("AUDITLAB_STAND_IN_FAIL")
        self.assertEqual(self.run_train(config, max_wait_s=5)["status"], "failed")
        self.assertEqual(len(self.launches()), 1)

    def test_interrupted_launch_is_never_relaunched(self):
        config = self.config(trainer_command=STAND_IN)
        job_id = self.run_train(config, out="plan", dry_run=True)["planned_requests"][1]["job_id"]
        (self.jobs / job_id).mkdir(parents=True)  # crash after taking the launch lock, before job_state.json
        record = self.run_train(config, max_wait_s=2)
        self.assertEqual((record["status"], record["result"]["error"]["code"]), ("failed", "launch_interrupted"))
        self.assertEqual(self.launches(), [])

    def test_trl_row_conversion(self):
        example = json.loads((self.root / "dataset" / "dataset.jsonl").read_text(encoding="utf-8").splitlines()[0])
        row = to_trl_row(example)
        self.assertEqual(set(row), {"messages", "tools"})
        call = row["messages"][2]["tool_calls"][0]
        self.assertEqual(call["function"]["arguments"], {"table": "ledger"})
        self.assertEqual(row["messages"][3]["name"], "query")
        self.assertEqual(row["tools"][0]["function"]["name"], "query")


if __name__ == "__main__":
    unittest.main()
