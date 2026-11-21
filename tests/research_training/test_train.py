"""train(): dry run, access/budget gates, resume without duplicate paid work, job outcomes.

Network calls go only to the local fake server (TEST DOUBLE); the real stdlib backend code runs.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_openai_server import FakeOpenAIServer  # noqa: E402
from training_fixtures import api_agent, make_attempt, make_curriculum  # noqa: E402

from auditlab.research.training.__main__ import main as cli_main  # noqa: E402
from auditlab.research.training.dataset import build_sft  # noqa: E402
from auditlab.research.training.openai_backend import OpenAIFineTuningBackend  # noqa: E402
from auditlab.research.training.train import TrainingError, train  # noqa: E402

KEY_ENV = "AUDITLAB_TEST_FT_KEY"


class SimulatedCrash(BaseException):
    """Stands in for a process kill; not an Exception, so nothing in train() swallows it."""


class CrashingBackend(OpenAIFineTuningBackend):
    def __init__(self, *, crash: str, **kwargs):
        super().__init__(**kwargs)
        self.crash = crash

    def create_job(self, **kwargs):
        if self.crash == "before_create":
            raise SimulatedCrash()
        job = super().create_job(**kwargs)
        if self.crash == "after_create":
            raise SimulatedCrash()
        return job

    def get_job(self, job_id):
        if self.crash == "while_polling":
            raise SimulatedCrash()
        return super().get_job(job_id)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TrainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = "sk-SENTINEL-" + uuid.uuid4().hex
        os.environ[KEY_ENV] = self.key
        self.server = FakeOpenAIServer(self.key).start()
        refs = []
        for n in range(1, 13):
            make_attempt(self.root / "attempts", n)
            refs.append(f"ep-{n:03d}-train")
        self.manifest = build_sft([self.root / "attempts"], self.root / "dataset")
        self.curriculum = make_curriculum(self.root / "curriculum.json", refs, kind="adaptive")
        self.out = self.root / "run"
        self.clock = FakeClock()

    def tearDown(self):
        self.server.stop()
        os.environ.pop(KEY_ENV, None)
        self.temp.cleanup()

    def config(self, **overrides) -> Path:
        config = {"schema_version": "1.0", "protocol_id": "auditlab-research-600s", "provider": "openai_compatible",
                  "base_url": self.server.base_url, "api_key_env": KEY_ENV, "base_model": "fake-base-2026",
                  "dataset": str(self.root / "dataset"), "hyperparameters": {"n_epochs": 2}, "seed": 7,
                  "price_per_million_training_tokens": 5.0, "budget_usd": 10.0, "confirm_paid": True,
                  "poll_interval_s": 30, "test_double": True}  # the fake server is a test double
        config.update(overrides)
        path = self.root / f"config-{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def run_train(self, config, **kwargs):
        kwargs.setdefault("sleep", self.clock.sleep)
        kwargs.setdefault("clock", self.clock)
        return json.loads(train(config, self.curriculum, self.out, **kwargs).read_text(encoding="utf-8"))

    def assert_no_key_anywhere(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.key, path.read_text(encoding="utf-8", errors="replace"), str(path))

    def test_dry_run_makes_no_http_calls(self):
        record = self.run_train(self.config(), dry_run=True)
        self.assertEqual(record["status"], "dry_run")
        self.assertEqual(self.server.requests, [])
        self.assertEqual(record["kind"], "weights")
        self.assertIsNone(record["result"]["fine_tuned_model"])
        self.assertTrue(record["planned_requests"])
        tokens = self.manifest["token_estimate"]["total"] * 2
        self.assertEqual(record["budget"]["billable_tokens_estimate"], tokens)
        self.assertAlmostEqual(record["budget"]["estimated_usd"], tokens * 5.0 / 1e6)
        self.assertEqual(record["curriculum"]["kind"], "adaptive")
        self.assertEqual(record["dataset"]["sha256"], self.manifest["dataset"]["sha256"])
        self.assertEqual(record["missing_conditions"], [])
        self.assertEqual((record["claimed_as"], record["provider"], record["job_id"], record["result_model"]),
                         ("training", "openai_compatible", None, None))
        self.assertEqual(record["teacher"]["sources"], {"oracle_demonstration": 12})
        self.out = self.root / "run-real-flag"
        self.assertIs(self.run_train(self.config(test_double=False), dry_run=True)["mock"], False)

    def test_missing_conditions_never_start(self):
        cases = {"price_per_million_training_tokens": {"price_per_million_training_tokens": None},
                 "budget_usd": {"budget_usd": 0},
                 "confirm_paid": {"confirm_paid": False},
                 "n_epochs": {"hyperparameters": {"n_epochs": "auto"}}}
        for needle, override in cases.items():
            with self.subTest(needle):
                self.out = self.root / f"run-{needle}"
                record = self.run_train(self.config(**override))
                self.assertEqual(record["status"], "not_started_missing_access")
                self.assertTrue(any(needle in m for m in record["missing_conditions"]), record["missing_conditions"])
                self.assertIsNone(record["job"]["job_id"])
        os.environ.pop(KEY_ENV)
        self.out = self.root / "run-no-key"
        record = self.run_train(self.config())
        self.assertTrue(any(KEY_ENV in m for m in record["missing_conditions"]))
        self.assertEqual(self.server.requests, [])

    def test_budget_exceeded_is_refused(self):
        record = self.run_train(self.config(budget_usd=0.000001))
        self.assertEqual((record["status"], record["refusal"]), ("not_started_missing_access", "budget_exceeded"))
        self.assertEqual(self.server.requests, [])

    def test_provider_refusing_creation_is_missing_access(self):
        self.server.create_error = (403, {"error": {"message": "fine-tuning not available for this organization"}})
        record = self.run_train(self.config())
        self.assertEqual(record["status"], "not_started_missing_access")
        self.assertEqual(record["provider_error"]["kind"], "permission")
        self.assertEqual(self.server.jobs, {})

    def test_success_records_model_checkpoints_and_cost(self):
        self.server.job_statuses = ["queued", "running", "succeeded"]
        record = self.run_train(self.config(), max_wait_s=3600)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["result"]["fine_tuned_model"], "ft:fake-base-2026:auditlab:ftjob-1")
        self.assertEqual(record["result"]["trained_tokens"], 12_000)
        self.assertAlmostEqual(record["budget"]["actual_usd"], 12_000 * 5.0 / 1e6)
        self.assertEqual(record["checkpoints"][0]["step_number"], 10)
        self.assertEqual((record["kind"], record["claimed_as"], record["provider"], record["mock"]),
                         ("weights", "training", "openai_compatible", True))
        self.assertEqual((record["job_id"], record["checkpoint_id"], record["result_model"]),
                         ("ftjob-1", "ftckpt-1", "ft:fake-base-2026:auditlab:ftjob-1"))
        self.assertTrue(record["events"])
        self.assertEqual(record["job"]["training_file_id"], "file-1")
        create = json.loads(next(r for r in self.server.requests if r["path"] == "/v1/fine_tuning/jobs"
                                 and r["method"] == "POST")["body"])
        self.assertEqual(create["method"], {"type": "supervised", "supervised": {"hyperparameters": {"n_epochs": 2}}})
        self.assertEqual((create["seed"], create["model"]), (7, "fake-base-2026"))
        self.assertTrue(create["suffix"].startswith("al-") and len(create["suffix"]) <= 64)
        self.assertEqual(create["metadata"]["auditlab_key"], record["settings"]["job_key"])
        self.assert_no_key_anywhere()

    def backend(self, crash):
        return CrashingBackend(crash=crash, base_url=self.server.base_url, api_key_env=KEY_ENV)

    def test_resume_after_crash_between_upload_and_create(self):
        with self.assertRaises(SimulatedCrash):
            self.run_train(self.config(), backend=self.backend("before_create"))
        self.assertEqual(self.server.count("POST", "/v1/files"), 1)
        self.assertEqual(self.server.jobs, {})
        config = self.config()
        record = self.run_train(config, max_wait_s=600)
        self.assertEqual(self.server.count("POST", "/v1/files"), 1)
        self.assertEqual(self.server.count("POST", "/v1/fine_tuning/jobs"), 1)
        self.assertEqual(self.server.jobs["ftjob-1"]["training_file"], "file-1")
        self.assertEqual(record["status"], "succeeded")

    def test_resume_after_crash_after_create_adopts_job(self):
        with self.assertRaises(SimulatedCrash):
            self.run_train(self.config(), backend=self.backend("after_create"))
        self.assertEqual(len(self.server.jobs), 1)
        record = self.run_train(self.config(), max_wait_s=600)
        self.assertEqual(self.server.count("POST", "/v1/fine_tuning/jobs"), 1)
        self.assertEqual(record["job"]["job_id"], "ftjob-1")
        self.assertEqual(record["status"], "succeeded")

    def test_resume_while_running_polls_same_job(self):
        self.server.job_statuses = ["running"]
        record = self.run_train(self.config())  # max_wait 0: one poll, then returns
        self.assertEqual((record["status"], record["job"]["job_id"]), ("running", "ftjob-1"))
        self.assertEqual(record["result"]["error_reason"], "job not finished")
        for _ in range(2):
            self.assertEqual(self.run_train(self.config(), max_wait_s=120)["status"], "running")
        self.server.job_statuses = ["succeeded"]
        self.server.jobs["ftjob-1"]["_polls"] = 0
        record = self.run_train(self.config(), max_wait_s=120)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(self.server.count("POST", "/v1/fine_tuning/jobs"), 1)
        self.assertEqual(self.server.count("POST", "/v1/files"), 1)
        self.assertEqual(len(self.server.jobs), 1)
        polled = {r["path"] for r in self.server.requests if r["method"] == "GET" and r["path"].count("/") == 4}
        self.assertEqual(polled, {"/v1/fine_tuning/jobs/ftjob-1"})
        # Even a dry run or missing confirmation after a job exists never creates another one.
        self.run_train(self.config(confirm_paid=False))
        self.assertEqual(self.server.count("POST", "/v1/fine_tuning/jobs"), 1)

    def test_failed_job_records_provider_error(self):
        self.server.job_statuses = ["running", "failed"]
        record = self.run_train(self.config(), max_wait_s=600)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["result"]["error"]["code"], "invalid_training_file")
        self.assertIsNone(record["result"]["fine_tuned_model"])
        self.assert_no_key_anywhere()

    def test_wrong_key_and_validation_errors(self):
        os.environ[KEY_ENV] = self.key + "-rotated"  # fake 401 body echoes the Authorization header
        record = self.run_train(self.config())
        self.assertEqual(record["status"], "not_started_missing_access")
        self.assertEqual(record["provider_error"]["kind"], "auth")
        os.environ[KEY_ENV] = self.key
        self.assert_no_key_anywhere()
        with self.assertRaises(TrainingError):
            train(self.config(allowed_splits=["train", "test"]), self.curriculum, self.root / "other")
        with self.assertRaises(TrainingError):
            train(self.config(kind="few_shot"), self.curriculum, self.root / "other")
        partial = make_curriculum(self.root / "partial.json", ["ep-001-train"])
        with self.assertRaises(TrainingError) as caught:
            train(self.config(), partial, self.root / "other")
        self.assertIn("not in the curriculum", str(caught.exception))
        self.assertNotIn(self.key, str(caught.exception))

    def test_teacher_sources_checked_at_training_time(self):
        roots = self.root / "third"
        refs = []
        for n in range(30, 42):
            make_attempt(roots, n, agent=api_agent("anthropic", "claude-opus-5-5"))
            refs.append(f"ep-{n:03d}-train")
        curriculum = make_curriculum(self.root / "cur-third.json", refs)
        ack = "J. Reviewer, 2026-11-21"
        build_sft([roots], self.root / "ds-third", allowed_sources=["third_party_model"], teacher_terms_acknowledged=ack)
        base = {"dataset": str(self.root / "ds-third")}
        with self.assertRaises(TrainingError):  # default allowed sources exclude third_party_model
            train(self.config(**base), curriculum, self.root / "t1", dry_run=True)
        with self.assertRaises(TrainingError):  # allowed but not acknowledged in the training config
            train(self.config(**base, allowed_teacher_sources=["third_party_model"]), curriculum, self.root / "t2",
                  dry_run=True)
        record = json.loads(train(self.config(**base, allowed_teacher_sources=["third_party_model"],
                                              teacher_terms_acknowledged=ack), curriculum, self.root / "t3",
                                  dry_run=True).read_text(encoding="utf-8"))
        self.assertEqual(record["teacher"]["sources"], {"third_party_model": 12})
        self.assertEqual(record["teacher"]["teacher_terms_acknowledged"], ack)
        # "self" must mean the model being trained.
        selfroot = self.root / "selfroot"
        for n in range(50, 62):
            make_attempt(selfroot, n, agent=api_agent("openai_compatible", "student-a"))
        curriculum = make_curriculum(self.root / "cur-self.json", [f"ep-{n:03d}-train" for n in range(50, 62)])
        build_sft([selfroot], self.root / "ds-self", self_models=["student-a"])
        with self.assertRaises(TrainingError):
            train(self.config(dataset=str(self.root / "ds-self"), base_model="student-b"), curriculum,
                  self.root / "t4", dry_run=True)
        record = json.loads(train(self.config(dataset=str(self.root / "ds-self"), base_model="student-a"), curriculum,
                                  self.root / "t5", dry_run=True).read_text(encoding="utf-8"))
        self.assertEqual(record["teacher"]["sources"], {"self": 12})

    def test_cli_dry_run_and_status(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["train", "--config", str(self.config()), "--curriculum", str(self.curriculum),
                             "--out", str(self.out), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["status"], "dry_run")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cli_main(["status", "--out", str(self.out)]), 0)
        self.assertEqual(json.loads(buffer.getvalue())["training"]["status"], "dry_run")
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
