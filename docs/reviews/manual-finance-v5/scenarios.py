"""Pre-registered v5 scenario matrix (written before any generation). Real generator only."""
Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}
ALL = ["split_payment", "equal_amount_different_supplier", "refund", "archive_copy", "next_day_settlement"]
FULL = {"ledger_rows_target": 2000, "documents_target": 200}


def cfg(**over):
    base = {"config_version": "1.1", "company_size": {"ledger_rows_target": 0, "documents_target": 0},
            "base_currency": "KZT", "currencies": ["KZT", "USD"], "timezone": "Asia/Almaty",
            "target_period": "2026-11", "issue_counts": dict(Z), "interaction": None, "clean_distractors": []}
    base.update(over)
    return base


MATRIX = {
    "G01-max-clean-kzt": (8601, cfg(company_size=FULL, clean_distractors=ALL)),
    "G02-max-12-issues-kzt": (8602, cfg(company_size=FULL, clean_distractors=ALL, currencies=["KZT", "USD", "JPY", "KWD"],
                                        interaction="allocation_fx_same_payment",
                                        issue_counts={"duplicate_invoice": 3, "payment_allocation": 3, "fx_conversion": 3, "period_cutoff": 3})),
    "G03-max-12-issues-kwd-berlin": (8603, cfg(company_size=FULL, clean_distractors=ALL, base_currency="KWD", currencies=["KWD", "JPY", "USD"],
                                               timezone="Europe/Berlin", target_period="2026-03", interaction="cutoff_fx_same_invoice",
                                               issue_counts={"duplicate_invoice": 2, "payment_allocation": 3, "fx_conversion": 4, "period_cutoff": 3})),
    "G04-max-clean-jpy-newyork": (8604, cfg(company_size=FULL, clean_distractors=ALL, base_currency="JPY", currencies=["JPY", "EUR", "KWD"],
                                            timezone="America/New_York", target_period="2026-11")),
    "G05-boundary-before": (8605, cfg(settlement_timing="before")),
    "G06-boundary-at": (8605, cfg(issue_counts={**Z, "payment_allocation": 1}, settlement_timing="at")),
    "G07-partial-allocation-refund": (8606, cfg(issue_counts={**Z, "payment_allocation": 1}, settlement_timing="after",
                                                clean_distractors=["refund", "next_day_settlement"])),
    "G08-clean-refunds": (8607, cfg(clean_distractors=ALL)),
}
BASES = ["KZT", "USD", "JPY", "KWD", "EUR"]
OTHERS = {"KZT": ["USD", "KWD"], "USD": ["JPY", "EUR"], "JPY": ["USD", "KWD"], "KWD": ["JPY", "USD"], "EUR": ["USD", "JPY"]}
ZONES = [("Asia/Almaty", "2026-11"), ("Europe/Berlin", "2026-10"), ("America/New_York", "2026-03"), ("UTC", "2026-12"), ("Asia/Kathmandu", "2027-01")]
for i, seed in enumerate(range(8611, 8621)):  # unselected block: every seed is used
    base = BASES[seed % 5]; zone, period = ZONES[(seed // 5) % 5]
    counts = {"duplicate_invoice": seed % 3, "payment_allocation": (seed // 3) % 3, "fx_conversion": (seed // 2) % 3, "period_cutoff": (seed // 7) % 3}
    interaction = [None, "allocation_fx_same_payment", "cutoff_fx_same_invoice"][seed % 3]
    if interaction == "allocation_fx_same_payment":
        counts["payment_allocation"] = max(1, counts["payment_allocation"]); counts["fx_conversion"] = max(1, counts["fx_conversion"])
    if interaction == "cutoff_fx_same_invoice":
        counts["period_cutoff"] = max(1, counts["period_cutoff"]); counts["fx_conversion"] = max(1, counts["fx_conversion"])
    MATRIX[f"G{9 + i:02d}-block-{seed}"] = (seed, cfg(company_size={"ledger_rows_target": 400, "documents_target": 65},
                                                      base_currency=base, currencies=[base] + OTHERS[base], timezone=zone,
                                                      target_period=period, interaction=interaction, issue_counts=counts,
                                                      clean_distractors=ALL))
HALF_FAMILIES = {
    "JPY<-USD": cfg(base_currency="JPY", currencies=["JPY", "USD"]),
    "KZT<-JPY": cfg(currencies=["KZT", "JPY"]),
    "KZT<-KWD": cfg(currencies=["KZT", "KWD"]),
    "USD<-KWD": cfg(base_currency="USD", currencies=["USD", "KWD"]),
    "KWD<-JPY": cfg(base_currency="KWD", currencies=["KWD", "JPY"]),
}
for family in HALF_FAMILIES.values():
    family.update(issue_counts={**Z, "fx_conversion": 1}, clean_distractors=["refund", "split_payment"])
HALF_SEEDS = range(8701, 8901)
