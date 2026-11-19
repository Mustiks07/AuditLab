"""Hand-calculated FX checks across IANA daylight-saving changes, independent of generator."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from cases import Case, dump
from auditlab.verifier import verify_report
from auditlab.verifier.loader import SnapshotError, _company_timezone

ENGINES = ("sqlite", "duckdb") if importlib.util.find_spec("duckdb") else ("sqlite",)


def write_fx_case(root, season, currency, amount, rate, expected, error):
    # March starts at -05:00 but the payment date is -04:00. November is the reverse.
    if season == "spring":
        start, end = "2026-03-01T00:00:00-05:00", "2026-04-01T00:00:00-04:00"
        event = "2026-03-14T00:01:00-04:00"
        wrong_day = "2026-03-13"
    else:
        start, end = "2026-11-01T00:00:00-04:00", "2026-12-01T00:00:00-05:00"
        event = "2026-11-14T23:30:00-05:00"
        wrong_day = "2026-11-15"
    case = Case("DST")
    case.invoice("D1", "I1", event, amount, currency)
    case.journal(event, ("AR", amount, 0), ("REVENUE", 0, amount), currency=currency, invoice="I1")
    case.journal(event, ("FX_VALUATION", expected + error, 0),
                 ("FX_OFFSET", 0, expected + error), invoice="I1")
    case.rate(event[:10], currency, "KZT", rate)
    # An incorrect fixed offset finds a valid but wrong adjacent-day quote.
    case.rate(wrong_day, currency, "KZT", "1")
    public = case.write(root)
    manifest = json.loads((public / "manifest.json").read_text())
    manifest.update(timezone="America/New_York", period_start=start, period_end=end)
    dump(public / "manifest.json", manifest)
    policy = json.loads((public / "policy.json").read_text())
    policy["allocation"]["deadline"] = end
    dump(public / "policy.json", policy)
    return public


def rewrite_all_instants_as_utc(public):
    # Include manifest boundaries: a representation offset is not the company's timezone.
    pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")
    for path in public.iterdir():
        text = path.read_text(encoding="utf-8")
        text = pattern.sub(lambda m: datetime.fromisoformat(m[0]).astimezone(timezone.utc).isoformat(), text)
        path.write_text(text, encoding="utf-8")


class TimezoneTests(unittest.TestCase):
    def test_spring_and_autumn_fx_with_zero_two_and_three_decimal_currencies(self):
        # Manual ROUND_HALF_UP amounts in KZT minor units:
        # JPY: 101 * 2.505 * 100 = 25300.5 -> 25301
        # USD: (10000 / 100) * 500 * 100 = 5000000
        # KWD: (1001 / 1000) * 1600.5 * 100 = 160210.05 -> 160210
        amounts = (("JPY", 101, "2.505", 25301),
                   ("USD", 10000, "500", 5000000),
                   ("KWD", 1001, "1600.5", 160210))
        for season in ("spring", "autumn"):
            for currency, amount, rate, expected in amounts:
                for error in (0, 1000):
                    with self.subTest(season=season, currency=currency, error=error), \
                            tempfile.TemporaryDirectory() as temporary:
                        public = write_fx_case(Path(temporary), season, currency, amount, rate, expected, error)
                        for utc in (False, True):
                            if utc:
                                rewrite_all_instants_as_utc(public)
                            for engine in ENGINES:
                                with self.subTest(utc=utc, engine=engine):
                                    report = verify_report(public, engine)
                                    self.assertEqual(report["timezone"], "America/New_York")
                                    self.assertTrue(report["timezone_database"].startswith("tzdata:"))
                                    self.assertEqual(report["notes"], [])
                                    findings = report["findings"]
                                    self.assertEqual(len(findings), int(bool(error)))
                                    if error:
                                        self.assertEqual(findings[0]["type"], "fx_conversion")
                                        self.assertEqual(findings[0]["claimed_amount_minor"], error)
                                        self.assertEqual(findings[0]["currency"], "KZT")

    def test_unknown_or_missing_timezone_fails_closed(self):
        for name in (None, "", "Mars/Olympus", "../UTC", "/etc/passwd", "America/../UTC"):
            with self.subTest(name=name), self.assertRaises(SnapshotError):
                _company_timezone(name)

    def test_missing_database_does_not_fall_back_to_host_rules(self):
        with patch("auditlab.verifier.loader.files", side_effect=ModuleNotFoundError("tzdata")):
            with self.assertRaisesRegex(SnapshotError, "pinned tzdata"):
                _company_timezone("America/New_York")


if __name__ == "__main__":
    unittest.main()
