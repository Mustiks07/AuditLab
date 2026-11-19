"""Git-specific CI tooling tests; Git is not a dependency of the offline runtime."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.review_evidence import capture


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.commit("initial")
        self.git("switch", "-c", "feature")
        self.head = self.commit("feature")
        self.git("switch", "main")
        self.base = self.commit("base")
        self.git("merge", "--no-ff", "feature", "-m", "synthetic PR merge")
        self.event = {"pull_request": {"head": {"sha": self.head}, "base": {"sha": self.base}}}

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def commit(self, name):
        (self.root / name).write_text(name)
        self.git("add", name)
        self.git("commit", "-m", name)
        return self.git("rev-parse", "HEAD")

    def test_actual_merge_and_shallow_checkout_preserve_parents(self):
        result = capture(self.root, self.event, "pull_request")
        self.assertEqual(result["parents"], [self.base, self.head])
        shallow = self.root.parent / "shallow"
        self.git("clone", "--depth=1", self.root.as_uri(), str(shallow))
        self.assertEqual(capture(shallow, self.event, "pull_request"), result)

    def test_wrong_merge_event_is_rejected(self):
        self.event["pull_request"]["base"]["sha"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "exact base and head"):
            capture(self.root, self.event, "pull_request")

    def test_dirty_checkout_is_rejected(self):
        (self.root / "untracked").write_text("changed")
        with self.assertRaisesRegex(ValueError, "clean"):
            capture(self.root, self.event, "pull_request")

    def test_push_and_dispatch_revisions(self):
        revision = self.git("rev-parse", "HEAD")
        self.assertEqual(capture(self.root, {"after": revision}, "push")["head_sha"], revision)
        self.assertEqual(capture(self.root, {}, "workflow_dispatch")["head_sha"], revision)
        with self.assertRaisesRegex(ValueError, "event revision"):
            capture(self.root, {"after": self.head}, "push")
