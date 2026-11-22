"""Bind independent Claude Pro 2 validation to the exact snapshot bytes."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys

from auditlab.release.transforms import read_json, write_json
from auditlab.release.rehearsal import file_hashes
from .export import snapshot_sha256


REVIEWER_SHA = "079bd8c46ec1e82c18378bf162b883eddace4745"
ASSURANCE_SHA = "85ac6c81ff9b39710e9753c7a5bc38fe2897ae82"
REQUIRED_CHECKS = ("formats", "no_contradictions", "source_book_consistent", "mutation_explained",
                   "answer_matches_truth", "evidence_available", "witness_trajectory")
_VALIDATE = """
import json
import sys
from pathlib import Path
import judge_adapter
from review.agent_generalization.validator import validate_candidate
snapshot, output = map(Path, sys.argv[1:])
print(json.dumps(validate_candidate(snapshot, adapter=judge_adapter, work=output), allow_nan=False))
"""
_ASSURE = """
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import auditlab.generator  # the reviewed generator, as a black box for replay
import judge_adapter
sys.path.pop(0)
from review.agent_generalization.validator import validate_candidate
snapshot, output = map(Path, sys.argv[1:3])
print(json.dumps(validate_candidate(snapshot, adapter=judge_adapter, work=output), allow_nan=False))
"""


class IndependentValidator:
    def __init__(self, checkout: Path, required_sha: str = REVIEWER_SHA):
        self.checkout = checkout.resolve()
        output = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.checkout, text=True)
        self.source_sha = output.strip()
        if self.source_sha != required_sha:
            raise ValueError("Reviewer checkout does not match the pinned independent source SHA")
        if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"],
                                   cwd=self.checkout, text=True).strip():
            raise ValueError("Reviewer checkout has modified tracked files")

    def __call__(self, snapshot: Path, output: Path) -> dict:
        snapshot, output = snapshot.resolve(), output.resolve()
        output.mkdir(parents=True, exist_ok=False)
        digest = snapshot_sha256(snapshot / "public")
        receipt = {"schema_version": "1.0", "artifact_id": f"validation-{digest[:24]}",
                   "source_sha": self.source_sha, "protocol_id": "agent-generalization-1.1",
                   "snapshot_sha256": digest}
        try:
            process = subprocess.run(self.command(snapshot, output),
                                     cwd=self.checkout, check=True, capture_output=True, encoding="utf-8", timeout=90)
            result = json.loads(process.stdout)
            receipt.update(result)
            receipt["snapshot_sha256"] = digest
            self._bind(receipt, snapshot, output)
        except (subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError) as error:
            receipt.update(status="not_checked", reason=f"reviewer did not return a usable result: {type(error).__name__}")
            if isinstance(error, subprocess.CalledProcessError):
                (output / "reviewer.stderr.txt").write_text(error.stderr or "", encoding="utf-8")
        write_json(output / "validation.json", receipt)
        return receipt

    def command(self, snapshot, output):
        return [sys.executable, "-c", _VALIDATE, str(snapshot), str(output)]

    def _bound(self, receipt, snapshot, output):
        answer = output / "answer_before_truth.json"
        episode_id = read_json(snapshot / "public/manifest.json")["episode_id"]
        return (receipt["source_sha"] == self.source_sha and receipt["episode_id"] == episode_id
                and sha256(answer.read_bytes()).hexdigest() == receipt["answer_sha256_before_truth"]
                and receipt["snapshot_sha256"] == snapshot_sha256(snapshot / "public"))

    def _bind(self, receipt, snapshot, output):
        if not self._bound(receipt, snapshot, output):
            receipt.update(status="not_checked", reason="reviewer receipt or answer did not bind to this snapshot")
            return
        checks = receipt["checks"]
        incomplete = [name for name in REQUIRED_CHECKS if checks.get(name, {}).get("status") != "passed"]
        witness = checks.get("witness_trajectory", {})
        actions, elapsed = witness.get("submit_action"), witness.get("elapsed_s")
        if receipt["status"] == "passed" and (incomplete or type(actions) is not int or not 1 <= actions <= 100
                                              or type(elapsed) not in (int, float) or not 0 <= elapsed < 60):
            receipt.update(status="not_checked", reason="missing required independent check or bounded public witness")
        elif receipt["status"] != "passed":
            receipt["reason"] = "reviewer checks with no pass: " + ", ".join(incomplete)
        else:
            receipt["reason"] = "independent arithmetic, source transition and public witness passed"


class AssuranceValidator(IndependentValidator):
    """Require Claude 2's independent public solver and exact snapshot replay."""

    def __init__(self, checkout, generator_repo=None):
        super().__init__(checkout, ASSURANCE_SHA)
        self.generator_repo = Path(generator_repo or Path(__file__).resolve().parents[3]).resolve()

    def command(self, snapshot, output):
        return [sys.executable, "-c", _ASSURE, str(snapshot), str(output), str(self.generator_repo)]

    def _bind(self, receipt, snapshot, output):
        receipt["reviewer_status"] = receipt["status"]
        binding = {side: file_hashes(snapshot / side) for side in ("public", "private")}
        if not self._bound(receipt, snapshot, output) or receipt.get("binding") != binding:
            receipt.update(status="not_checked", reason="independent answer or snapshot binding failed")
            return
        checks = receipt["checks"]
        required = (*REQUIRED_CHECKS, "mutation_declared", "independent_solver", "reproducible_generation")
        incomplete = {name for name in required if checks.get(name, {}).get("status") != "passed"}
        failed = {name for name, check in checks.items() if check.get("status") == "failed"}
        provenance = read_json(snapshot / "private/provenance.json")
        if (("transform" in provenance or "reduction" in provenance)
                and checks.get("reproducible_generation", {}).get("status") == "not_checked"):
            incomplete.discard("reproducible_generation")
        solver = checks.get("independent_solver", {})
        actions, elapsed = solver.get("actions"), solver.get("elapsed_s")
        if (type(actions) is not int or not 1 <= actions <= 100
                or type(elapsed) not in (int, float) or not 0 <= elapsed < 60):
            incomplete.add("independent_solver")
        if failed or incomplete:
            receipt.update(status="failed" if failed else "not_checked",
                           reason="independent checks without a pass: " + ", ".join(sorted(failed | incomplete)))
        else:
            receipt.update(status="passed", reason="independent public solver, arithmetic and source transition passed")
