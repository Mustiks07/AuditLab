"""Select new cases from public/ only: exact-half valuations and events inside DST transition hours."""
import json, sys, shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 2 else None; out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
sys.path[:0] = [str(repo) if repo else ".", "."]
from auditlab.generator import generate_episode
from audit_calc import audit, load
Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}
def cfg(**o):
    c = {"config_version": "1.1", "company_size": {"ledger_rows_target": 0, "documents_target": 0}, "base_currency": "KZT",
         "currencies": ["KZT", "USD"], "timezone": "Asia/Almaty", "target_period": "2026-11", "issue_counts": dict(Z),
         "interaction": None, "clean_distractors": ["refund", "split_payment"]}
    c.update(o); return c
FAMILIES = {
  "half-KZT-KWD": cfg(currencies=["KZT", "KWD"], issue_counts={**Z, "fx_conversion": 1}),
  "half-KWD-USD": cfg(base_currency="KWD", currencies=["KWD", "USD"], issue_counts={**Z, "fx_conversion": 1}),
  "half-JPY-KWD": cfg(base_currency="JPY", currencies=["JPY", "KWD"], issue_counts={**Z, "fx_conversion": 1}),
  "half-KWD-JPY": cfg(base_currency="KWD", currencies=["KWD", "JPY"], issue_counts={**Z, "fx_conversion": 1}),
  "half-KZT-JPY": cfg(currencies=["KZT", "JPY"], issue_counts={**Z, "fx_conversion": 1}),
  "half-JPY-USD": cfg(base_currency="JPY", currencies=["JPY", "USD"], issue_counts={**Z, "fx_conversion": 1}),
  "dst-NY-2026-11": cfg(base_currency="USD", currencies=["USD", "EUR"], timezone="America/New_York", target_period="2026-11", issue_counts={**Z, "fx_conversion": 1, "payment_allocation": 1, "period_cutoff": 1}),
  "dst-NY-2026-10": cfg(base_currency="USD", currencies=["USD", "EUR"], timezone="America/New_York", target_period="2026-10", issue_counts={**Z, "fx_conversion": 1, "period_cutoff": 1}),
  "dst-BER-2026-10": cfg(base_currency="EUR", currencies=["EUR", "USD"], timezone="Europe/Berlin", target_period="2026-10", issue_counts={**Z, "fx_conversion": 1, "payment_allocation": 1, "period_cutoff": 1}),
  "dst-BER-2026-03": cfg(base_currency="EUR", currencies=["EUR", "USD"], timezone="Europe/Berlin", target_period="2026-03", issue_counts={**Z, "fx_conversion": 1, "payment_allocation": 1, "period_cutoff": 1}),
}
TRANSITIONS = {"America/New_York": ["2026-03-08", "2026-11-01"], "Europe/Berlin": ["2026-03-29", "2026-10-25"]}
def transition_hours(zone_name):
    z = ZoneInfo(zone_name); spans = []
    for day in TRANSITIONS.get(zone_name, []):
        t = datetime.fromisoformat(day + "T00:00:00+00:00") - timedelta(hours=12)
        prev = t.astimezone(z).utcoffset()
        for _ in range(48 * 60):
            t += timedelta(minutes=1); off = t.astimezone(z).utcoffset()
            if off != prev:
                lo, hi = t - timedelta(hours=1), t + timedelta(hours=1)   # UTC instants within 1h of the switch
                spans.append((lo, hi, prev, off)); break
    return spans
def main():
  found = []
  for fam, config in FAMILIES.items():
      spans = transition_hours(config["timezone"])
      for seed in range(int(sys.argv[3]), int(sys.argv[4])):
          ep = out / "scan" / f"{fam}-{seed}"
          if ep.exists(): shutil.rmtree(ep)
          generate_episode(seed, config, ep)
          pub = ep / "public"
          r = audit(pub)
          halves = [a for a in r["arithmetic"] if "EXACT HALF" in a]
          m, pol, ledger, invs, pays, fx = load(pub)
          stamps = [x["timestamp"] for x in ledger] + [d["issued_at"] for d in invs] + [p["timestamp"] for p in pays]
          in_dst = sorted({s for s in stamps for lo, hi, a, b in spans if lo <= datetime.fromisoformat(s) < hi})
          bad_offset = sorted({s for s in stamps if datetime.fromisoformat(s).utcoffset() not in (timedelta(0), datetime.fromisoformat(s).astimezone(ZoneInfo(m["timezone"])).utcoffset())})
          if halves or in_dst or bad_offset:
              found.append({"family": fam, "seed": seed, "halves": halves, "dst_window": in_dst, "offset_mismatch": bad_offset, "findings": len(r["findings"])})
          shutil.rmtree(ep / "private")  # never read; removed so the scan cannot touch truth
  for f in found: print(json.dumps(f)[:400])
  print("hits", len(found))

if __name__ == "__main__":
    main()
