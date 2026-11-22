"""Generator 1.9 / profile 1.3: adapter default, varied defaults, keyed randomness, seed attack, old bytes."""
from __future__ import annotations

import csv
import io
import json
import logging
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import judge_adapter  # noqa: E402
from auditlab.generator import generate_episode  # noqa: E402
from datasets import build_13  # noqa: E402

KEY = "00112233445566778899aabbccddeeff"
OTHER_KEY = "ffeeddccbbaa99887766554433221100"
# Tree SHA-256 of public/ and private/ for profiles 1.0-1.2, recorded at d745376 before this change.
LEGACY = [
    (3, {}, "bf209d43aa44f2768dabce97b7fc1d63bde12a832aefe7afe67b3fa141b35f1b"),
    (817, {"settlement_timing": "after", "clean_distractors": []},
     "25256c16217786e9560a625096fc845bba173ce47171254cabe3aeea318755e6"),
    (11, {"config_version": "1.1", "issue_counts": {"duplicate_invoice": 1, "payment_allocation": 1,
                                                    "fx_conversion": 1, "period_cutoff": 1}},
     "255ab2c1581ce00ac2fdbd1bd2569e8fe5c9e910dcc898b98761a834155dd355"),
    (12, {"config_version": "1.1", "currencies": ["KZT", "JPY"], "interaction": "cutoff_fx_same_invoice",
          "issue_counts": {"period_cutoff": 1, "fx_conversion": 1},
          "company_size": {"ledger_rows_target": 600, "documents_target": 90}},
     "1404bf6a35b02337a561fe49479d47d31ba164c61b6c3d7a648789716b4b9f11"),
    (13, {"config_version": "1.1", "allocation_mode": "omitted", "issue_counts": {"payment_allocation": 2},
          "clean_distractors": ["split_payment", "next_day_settlement"]},
     "c79131d30d8e22a17917e54f5b40e11229c4257d3910b738342e390904daedd1"),
    (21, {"config_version": "1.2", "issue_counts": {"payment_allocation": 1, "fx_conversion": 1},
          "interaction": "allocation_fx_same_payment"},
     "7e4326922a9f455002d375c555fc13a8f1ccb7a6bd34e25a8701824fd37d1f9c"),
    (22, {"config_version": "1.2", "financial_case": {"kind": "allocation", "amount_minor": 1234567,
                                                       "settlement_offset_minutes": 0},
          "issue_counts": {"payment_allocation": 1}},
     "a40069eb4847d37bc43527152060141f4566ef4b77f55e417d1c4d9f33ed4310"),
    (23, {"config_version": "1.2", "financial_case": {"kind": "invoice_cutoff", "amount_minor": 777700},
          "issue_counts": {}, "timezone": "Asia/Tokyo", "target_period": "2025-02"},
     "8353637bef81a5ffe11e2ff62a6f2a12bcc5e91c0163a1a2b57487b5422a225b"),
]


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.count = 0

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, seed, config, *, adapter=True) -> Path:
        self.count += 1
        target = self.tmp / f"e{self.count}"
        try:
            (judge_adapter.generate_episode if adapter else generate_episode)(seed, config, str(target))
        except ValueError as error:
            if "timezone_data_required" in str(error):
                raise unittest.SkipTest("profile 1.1+ needs the pinned tzdata (run in the runtime image)")
            raise
        return target

    @staticmethod
    def read(episode, name):
        return json.loads((episode / name).read_text(encoding="utf-8"))

    @staticmethod
    def ledger(episode):
        with (episode / "public" / "ledger.csv").open(encoding="utf-8") as stream:
            return list(csv.DictReader(stream))

    @staticmethod
    def public_text(episode):
        return "".join(path.read_text(encoding="utf-8") for path in sorted((episode / "public").iterdir()))


class OldProfilesKeepTheirBytes(Case):
    def test_profiles_10_to_12_are_byte_identical_to_the_base(self):
        for seed, config, expected in LEGACY:
            with self.subTest(seed=seed):
                self.assertEqual(build_13.tree_sha256(self.make(seed, config, adapter=False)), expected)

    def test_explicit_profile_10_through_the_adapter_is_the_old_generator(self):
        through_adapter = self.make(3, {"config_version": "1.0"})
        direct = self.make(3, {}, adapter=False)
        self.assertEqual(self.public_text(through_adapter), self.public_text(direct))
        self.assertEqual(self.read(through_adapter, "private/truth.json"), self.read(direct, "private/truth.json"))


class AdapterDefault(Case):
    def test_empty_config_uses_profile_13_not_the_old_default(self):
        episode = self.make(5, {})
        provenance = self.read(episode, "private/provenance.json")
        self.assertEqual((provenance["generator_version"], provenance["config"]), ("1.9", {"config_version": "1.3"}))
        self.assertEqual(provenance["randomness"]["mode"], "unkeyed")
        manifest = self.read(episode, "public/manifest.json")
        self.assertGreaterEqual(manifest["ledger_rows"], 200)
        self.assertGreater(manifest["document_count"], 20)
        self.assertEqual(manifest["timezone"], provenance["resolved_config"]["timezone"])
        self.assertEqual(judge_adapter.make_env().versions()["default_generator_profile"], "1.3")

    def test_train_reset_without_episode_dir_uses_the_same_profile(self):
        env = judge_adapter.make_env()
        self.addCleanup(env.close)
        try:
            observation, _ = env.reset(5, {"mode": "train", "team_config": {}})
        except ValueError as error:
            if "timezone_data_required" in str(error):
                self.skipTest("needs the pinned tzdata")
            raise
        self.assertEqual(observation["manifest"], self.read(self.make(5, {}), "public/manifest.json"))

    def test_explicit_fields_keep_their_meaning(self):
        config = {"timezone": "Asia/Tokyo", "target_period": "2026-02", "currencies": ["KZT", "USD"],
                  "company_size": {"ledger_rows_target": 333, "documents_target": 70},
                  "clean_distractors": ["refund"], "issue_counts": {"period_cutoff": 2}}
        episode = self.make(8, config)
        manifest = self.read(episode, "public/manifest.json")
        resolved = self.read(episode, "private/provenance.json")["resolved_config"]
        self.assertEqual({key: resolved[key] for key in config}, config)
        self.assertEqual((manifest["timezone"], manifest["period_start"][:7]), ("Asia/Tokyo", "2026-02"))
        self.assertEqual(sorted(manifest["currency_exponents"]), ["KZT", "USD"])
        self.assertGreaterEqual(manifest["ledger_rows"], 333)
        self.assertEqual(Counter(i["type"] for i in self.read(episode, "private/truth.json")["issues"]),
                         Counter({"period_cutoff": 2}))

    def test_legacy_settlement_timing_keeps_its_amounts_in_13(self):
        episode = self.make(817, {"config_version": "1.3", "settlement_timing": "after"})
        [issue] = self.read(episode, "private/truth.json")["issues"]
        self.assertEqual((issue["type"], issue["expected_amount_minor"], issue["currency"]),
                         ("payment_allocation", 2_000_000, "KZT"))


class VariedDefaults(Case):
    def test_seeds_vary_more_than_amounts(self):
        rows = []
        for seed in range(1, 13):
            episode = self.make(seed, {})
            manifest = self.read(episode, "public/manifest.json")
            events = Counter(e["kind"] for e in self.read(episode, "private/provenance.json")["events"])
            rows.append((manifest["timezone"], manifest["period_start"][:7], tuple(sorted(manifest["currency_exponents"])),
                         manifest["ledger_rows"], manifest["document_count"], tuple(sorted(events.items()))))
        self.assertGreaterEqual(len({r[0] for r in rows}), 3)
        self.assertGreaterEqual(len({r[1] for r in rows}), 4)
        self.assertGreaterEqual(len({r[2] for r in rows}), 2)
        self.assertGreaterEqual(len({r[5] for r in rows}), 6)  # different event structure, not only amounts
        self.assertGreater(max(r[3] for r in rows) - min(r[3] for r in rows), 300)

    def test_clean_lookalikes_settle_just_before_the_boundary(self):
        episode = self.make(4, {})
        self.assertEqual(self.read(episode, "private/truth.json")["issues"], [])
        end = datetime.fromisoformat(self.read(episode, "public/manifest.json")["period_end"])
        late = [e for e in self.read(episode, "private/provenance.json")["events"]
                if e["kind"] == "designated_payment" and e.get("settled_at")
                and 0 < (end - datetime.fromisoformat(e["settled_at"])).total_seconds() <= 86_400]
        self.assertGreaterEqual(len(late), 1)

    def test_large_scenario_needs_every_page(self):
        episode = self.make(9, {"company_size": {"ledger_rows_target": 2000, "documents_target": 200},
                                "issue_counts": {"payment_allocation": 1, "duplicate_invoice": 1}})
        env = judge_adapter.make_env()
        self.addCleanup(env.close)
        env.reset(0, {"mode": "train", "episode_dir": str(episode)})
        seen, cursor, pages = [], None, 0
        while True:
            observation = env.step({"name": "list_records", "args": {"table": "ledger", "cursor": cursor, "limit": 50}})[0]
            pages += 1
            seen += [row["row_id"] for row in observation["result"]["records"]]
            cursor = observation["result"]["next_cursor"]
            if cursor is None:
                break
        self.assertEqual((pages, len(seen), len(set(seen))), (40, 2000, 2000))
        self.assertEqual(self.read(episode, "public/manifest.json")["document_count"], 200)
        evidence_rows = {ref.split("/", 1)[1] for issue in self.read(episode, "private/truth.json")["issues"]
                         for ref in issue["evidence_sets"][0] if ref.startswith("ledger/")}
        positions = {seen.index(row) // 50 for row in evidence_rows}
        self.assertTrue(positions - {0}, "evidence rows should not all be on the first page")


class KeyedRandomness(Case):
    def test_same_key_reproduces_and_other_keys_rename_and_reprice(self):
        first, again = self.make(42, {"episode_key": KEY}), self.make(42, {"episode_key": KEY})
        self.assertEqual(build_13.tree_sha256(first), build_13.tree_sha256(again))
        other, plain = self.make(42, {"episode_key": OTHER_KEY}), self.make(42, {})
        ids = lambda e: {r["row_id"] for r in self.ledger(e)}  # noqa: E731
        amounts = lambda e: sorted(int(r["debit_minor"]) for r in self.ledger(e))  # noqa: E731
        self.assertFalse(ids(first) & ids(other) or ids(first) & ids(plain))
        self.assertNotEqual(amounts(first), amounts(other))
        self.assertEqual(self.read(first, "private/provenance.json")["randomness"]["mode"], "keyed")

    def test_invalid_or_misplaced_keys_are_rejected(self):
        for key in ("00112233", KEY.upper(), 7, KEY + "0", "zz" * 16):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "invalid_episode_key"):
                self.make(1, {"episode_key": key})
        with self.assertRaisesRegex(ValueError, "requires config_version 1.3"):
            self.make(1, {"config_version": "1.1", "episode_key": KEY})

    def test_key_never_reaches_public_observations_or_the_judge_log(self):
        episode = self.make(42, {"episode_key": KEY, "issue_counts": {"payment_allocation": 1}})
        texts = [self.public_text(episode)]
        env = judge_adapter.make_env()
        self.addCleanup(env.close)
        observation, info = env.reset(42, {"mode": "train", "episode_dir": str(episode)})
        texts.append(json.dumps([observation, info]))
        for action in ({"name": "list_records", "args": {"table": "policy", "cursor": None, "limit": 50}},
                       {"name": "get_record", "args": {"table": "ledger", "id": KEY}},
                       {"name": "calculator", "args": {"expression": KEY}},
                       {"name": "submit", "args": {"findings": {"episode_id": info["episode_id"], "findings": []}}}):
            texts.append(json.dumps(env.step(action), default=str))
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("auditlab.judge_adapter")
        logger.addHandler(handler)
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        generated = judge_adapter.make_env()
        self.addCleanup(generated.close)
        texts.append(json.dumps(generated.reset(42, {"mode": "train", "team_config": {"episode_key": KEY}})))
        log = stream.getvalue()
        reset_log = [json.loads(line) for line in log.splitlines() if json.loads(line)["event"] == "reset"]
        self.assertEqual(len(reset_log), 1)
        self.assertEqual(reset_log[0]["team_config"]["episode_key"], {"redacted": True})
        self.assertNotIn("seed", reset_log[0])
        for text in (*texts, log):
            self.assertNotIn(KEY, text)
            self.assertNotIn(KEY.upper(), text)

    def test_closed_run_report_does_not_serialize_seed_or_key(self):
        from auditlab.runner import make_env

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            env = make_env("closed", evaluator="inprocess", trusted_dir=out / "receipts")
            self.addCleanup(env.close)
            seed = 982334087137
            _, info = env.reset(seed, {"config_version": "1.3", "episode_key": KEY})
            env.step({"action": "submit", "args": {"episode_id": info["episode_id"], "findings": []}})
            [report] = (out / "receipts").rglob("report.json")
            self.assertNotIn("seed", json.loads(report.read_text(encoding="utf-8")))
            self.assertNotIn(KEY, report.read_text(encoding="utf-8"))

    def test_nonce_renames_without_changing_the_economics(self):
        plain = self.make(6, {"issue_counts": {"fx_conversion": 1}})
        renamed = self.make(6, {"issue_counts": {"fx_conversion": 1}, "snapshot_nonce": "copy-b"})
        self.assertFalse({r["row_id"] for r in self.ledger(plain)} & {r["row_id"] for r in self.ledger(renamed)})
        strip = lambda e: sorted((r["timestamp"], r["account"], r["debit_minor"], r["credit_minor"], r["currency"])  # noqa: E731
                                 for r in self.ledger(e))
        self.assertEqual(strip(plain), strip(renamed))
        amounts = lambda e: [(i["type"], i["expected_amount_minor"]) for i in self.read(e, "private/truth.json")["issues"]]  # noqa: E731
        self.assertEqual(amounts(plain), amounts(renamed))


class OpenSet13(unittest.TestCase):
    def test_recipe_matrix_and_published_bytes(self):
        recipe = build_13.load_recipe()
        for split, entries in recipe["splits"].items():
            self.assertEqual(Counter(e["category"] for e in entries),
                             Counter(clean=2, single=4, interaction=2, boundary=1, mixed=1), split)
        families = [e["family"] for entries in recipe["splits"].values() for e in entries]
        self.assertEqual(len(families), len(set(families)))
        try:
            result = build_13.check(ROOT / "datasets" / "open-1.3")
        except ValueError as error:
            if "timezone_data_required" in str(error):
                self.skipTest("needs the pinned tzdata")
            raise
        self.assertEqual(result, {"status": "passed", "snapshots": 20, "differing": [], "unexpected": []})


if __name__ == "__main__":
    unittest.main()
