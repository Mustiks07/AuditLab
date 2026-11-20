"""Public-only: how close do generated events get to DST transitions? Also KWD exact halves."""
import sys, shutil, collections
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
repo = Path(sys.argv[1]).resolve(); out = Path(sys.argv[2]); sys.path[:0] = [str(repo), "."]
from auditlab.generator import generate_episode
from audit_calc import audit, load
from scan import cfg, Z, TRANSITIONS
FAM = {
  ("America/New_York", "2026-11"): cfg(base_currency="USD", currencies=["USD", "EUR"], timezone="America/New_York", target_period="2026-11", issue_counts={**Z, "fx_conversion": 2, "payment_allocation": 2, "period_cutoff": 2, "duplicate_invoice": 1}),
  ("America/New_York", "2026-03"): cfg(base_currency="USD", currencies=["USD", "EUR"], timezone="America/New_York", target_period="2026-03", issue_counts={**Z, "fx_conversion": 2, "payment_allocation": 2, "period_cutoff": 2}),
  ("Europe/Berlin", "2026-10"): cfg(base_currency="EUR", currencies=["EUR", "USD"], timezone="Europe/Berlin", target_period="2026-10", issue_counts={**Z, "fx_conversion": 2, "payment_allocation": 2, "period_cutoff": 2}),
  ("Europe/Berlin", "2026-03"): cfg(base_currency="EUR", currencies=["EUR", "USD"], timezone="Europe/Berlin", target_period="2026-03", issue_counts={**Z, "fx_conversion": 2, "payment_allocation": 2, "period_cutoff": 2}),
}
lo, hi = int(sys.argv[3]), int(sys.argv[4])
for (zone_name, period), config in FAM.items():
    z = ZoneInfo(zone_name); days = set(TRANSITIONS[zone_name]); nearest = None; on_day = collections.Counter()
    for seed in range(lo, hi):
        ep = out / "dst" / f"{zone_name.replace('/', '_')}-{period}-{seed}"
        if ep.exists(): shutil.rmtree(ep)
        generate_episode(seed, config, ep); shutil.rmtree(ep / "private")
        m, pol, ledger, invs, pays, fx = load(ep / "public")
        for s in [x["timestamp"] for x in ledger] + [d["issued_at"] for d in invs] + [p["timestamp"] for p in pays]:
            t = datetime.fromisoformat(s); loc = t.astimezone(z)
            if loc.date().isoformat() in days:
                on_day[loc.strftime("%H:%M")] += 1
                for d in days:
                    # distance to the local wall-clock 02:00 of the transition day
                    sw = datetime.fromisoformat(d + "T02:00").replace(tzinfo=z)
                    gap = abs((t - sw).total_seconds()) / 60
                    nearest = gap if nearest is None else min(nearest, gap)
        shutil.rmtree(ep)
    print(zone_name, period, "events on transition day (local HH:MM -> count):", dict(sorted(on_day.items())), "nearest to 02:00 (min):", nearest)
