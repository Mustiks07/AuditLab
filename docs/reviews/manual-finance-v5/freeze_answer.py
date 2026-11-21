"""Write the v5 answer before truth: automated (audit_calc) and manual (hand) parts kept apart.

usage: python freeze_answer.py WORKDIR OUT_JSON
"""
import json, sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))
from audit_calc import audit  # noqa: E402

work, out = Path(sys.argv[1]), Path(sys.argv[2])
key = lambda f, a="claimed_amount_minor": (f["type"], tuple(sorted(f["affected_ids"])), f[a], f["currency"], tuple(sorted(f["evidence_ids"])))

# Hand results, written from the public rows (see REPORT.md, "Ручной расчёт"). Amounts in minor units.
MANUAL = {
    "G05-boundary-before": {"expected": [], "work": "5000000 - 3000000 - 2000000 (2026-11-30T23:59+05) = 0"},
    "G06-boundary-at": {"expected": [("payment_allocation", 2000000, "KZT")], "work": "settlement at exactly 2026-12-01T00:00+05 excluded: 5000000 - 3000000 = 2000000"},
    "G07-partial-allocation-refund": {"expected": [("payment_allocation", 2000000, "KZT")], "work": "deposit 800000 refunded 11-08; next-day payment 4100000 settled; 254.00 USD x 503.25 = 127825.50 KZT = 12782550 = recorded; PDC23: 5000000 - 3000000 = 2000000 (rest applied 12-01 00:01)"},
    "G08-clean-refunds": {"expected": [], "work": "4 x 5300000 from different suppliers, one recognition each; I7418 two documents one recognition; split 2120000 + 3180000; refund 200000; 75.00 USD x 505.50 = 37912.50 = 3791250 = recorded"},
    "H-JPY-from-USD-clean-8753": {"expected": [("fx_conversion", 10, "JPY")], "work": "210.00 x 6.05 = 1270.5 -> 1271 = recorded (half-even 1270); 43.00 x 3.55 = 152.65 -> 153, recorded 163 -> 10"},
    "H-JPY-from-USD-error-8706": {"expected": [("fx_conversion", 27, "JPY")], "work": "110.00 x 6.55 = 720.5 -> 721, recorded 748 -> 27 (half-even 28); 114.00 x 6.55 = 746.7 -> 747 = recorded"},
    "H-KZT-from-JPY-clean-8702": {"expected": [("fx_conversion", 36, "KZT")], "work": "299 x 2.615 = 781.885 -> 78188.5 -> 78189 = recorded; 73 x 2.585 = 188.705 -> 18870.5 -> 18871, recorded 18907 -> 36 (half-even 37)"},
    "H-KZT-from-JPY-error-8701": {"expected": [("fx_conversion", 25, "KZT")], "work": "51 x 2.615 = 133.365 -> 13336.5 -> 13337, recorded 13362 -> 25 (half-even 26); 135 x 2.550 = 344.25 = 34425 = recorded"},
    "H-KZT-from-KWD-clean-8708": {"expected": [("fx_conversion", 9900, "KZT")], "work": "37.010 x 1606.5 = 59456.565 -> 5945656.5 -> 5945657 = recorded; 198.010 x 1609.5 = 318697.095 -> 31869709.5 -> 31869710, recorded 31879610 -> 9900"},
    "H-KZT-from-KWD-error-8702": {"expected": [("fx_conversion", 3600, "KZT")], "work": "72.010 x 1608.5 = 115828.085 -> 11582808.5 -> 11582809, recorded 11586409 -> 3600 (half-even 3601); 298.010 x 1611.5 = 480243.115 -> 48024311.5 -> 48024312 = recorded"},
    "G02-max-12-issues-kzt": {"spot": [("duplicate_invoice", 2986900, "KZT"), ("fx_conversion", 5600, "KZT"), ("payment_allocation", 3360, "USD"), ("period_cutoff", 2309000, "KZT")],
                              "work": "first finding of each type by hand: dup I42E8B two docs two journals; P15115 224.00 x 505.50 = 113232 KZT, recorded 11328800 -> 5600; PEAE01 8400 - 5040 = 3360; I22D6F issued 11-30 recognised 12-01"},
    "G03-max-12-issues-kwd-berlin": {"spot": [("duplicate_invoice", 11743000, "KWD"), ("fx_conversion", 56750, "KWD"), ("payment_allocation", 31840000, "KWD"), ("period_cutoff", 27093000, "KWD")],
                                     "work": "dup I64C0F two docs two journals; invoice I8B32C 227.00 x 2.80 = 635.600 KWD, recorded 692350 -> 56750; P724A7 79600000 - 47760000 = 31840000; I4909B issued 03-31 recognised 04-01"},
}

answer = {"base_main": "daf1fc64df37651e14801e0286b9741b30820d35", "generator": "1.5",
          "note": "Public-only. automated = audit_calc.py; manual = hand work from rows. Fixtures X* are hand-made, not generator output.",
          "generator_cases": {}, "fixtures": {}, "manual_vs_automated": []}
for d in sorted((work / "public").iterdir()):
    r = audit(d / "public")
    entry = {"episode_id": r["episode_id"], "automated": {"findings": r["findings"], "notes": r["notes"], "arithmetic": r["arithmetic"]}}
    if d.name in MANUAL:
        entry["manual"] = MANUAL[d.name]
        auto = sorted((f["type"], f["claimed_amount_minor"], f["currency"]) for f in r["findings"])
        if "expected" in MANUAL[d.name]:
            same = auto == sorted(MANUAL[d.name]["expected"])
        else:
            same = all(x in auto for x in MANUAL[d.name]["spot"])
        answer["manual_vs_automated"].append({"case": d.name, "agree": same})
    answer["generator_cases"][d.name] = entry
for d in sorted((work / "fixtures").iterdir()):
    r = audit(d / "public")
    hand = json.loads((d / "hand-truth.json").read_text(encoding="utf-8"))
    same = sorted(key(f) for f in r["findings"]) == sorted(key(i | {"evidence_ids": i["evidence_sets"][0]}, "expected_amount_minor") for i in hand["issues"])
    answer["fixtures"][d.name] = {"episode_id": r["episode_id"], "manual_hand_truth": hand, "automated": {"findings": r["findings"], "notes": r["notes"], "arithmetic": r["arithmetic"]}}
    answer["manual_vs_automated"].append({"case": d.name, "agree": same})
out.write_text(json.dumps(answer, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
for row in answer["manual_vs_automated"]:
    print(row)
print("generator findings:", sum(len(v["automated"]["findings"]) for v in answer["generator_cases"].values()),
      "in", len(answer["generator_cases"]), "companies; fixture findings:", sum(len(v["automated"]["findings"]) for v in answer["fixtures"].values()))
