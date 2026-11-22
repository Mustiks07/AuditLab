"""Instruction variants for the A/B/C comparison (Issue #42, agent-improvement v2). Frozen before any result.

    A  the original auditor instruction (agent.SYSTEM_PROMPT), nothing added
    B  A + a GENERAL improvement: a careful audit method any accountant would apply, written without
       looking at AuditLab's observed failures
    C  A + a PROCEDURE and one worked EXAMPLE derived from AuditLab's observed weakness (missed partial
       payment allocations), built from public policy text and one TRAIN episode only

B and C add a comparable amount of context (their lengths are checked by a test); model, tools, limits,
temperature-like settings and budgets are identical across arms. Changing these texts is prompt
engineering, never "training": no weight changes. Provenance of C is recorded in PROVENANCE.
Tuning is allowed only on train/dev; the final families come from Claude Pro 2 via the integrator.
"""
from __future__ import annotations

import hashlib

PROMPT_SET_VERSION = "agent-improvement-v2-prompts-1.0"
PROMPT_VARIANTS = ("A", "B", "C")

GENERAL_METHOD = """

Working method. Before acting, read the policy rules in the first observation and decide which records can reveal each of the four error types. Work systematically rather than sampling: list every page of the tables you rely on, and do not stop after the first page. For every document, find the ledger rows that recognise or settle it and check that amounts, dates, currencies and the reporting boundary agree with the policy. Do every comparison with the calculator, writing the exact expression you checked. Keep a short running list of what you have verified and what is still open. An empty submission is itself a claim that the books are clean, so only submit no findings after you have checked each error type against the records; equally, never report a finding you could not verify with records you read in this episode. Use the remaining action budget deliberately: reading a few more records costs little, while a missed or false finding costs much more. Before submitting, re-read your findings: each must name the exact affected ids, the integer amount of the discrepancy in its currency, and every record needed to verify it. Treat dates carefully: compare timestamps with their offsets against the period boundary stated in the manifest and policy, and remember that a boundary marked exclusive does not belong to the period. Never compare amounts in different currencies without converting them by the rate and date the policy prescribes. Many unusual-looking items are legitimate, such as repeated equal amounts, refunds, archive copies or payments split into parts; report an item only when the records prove that a rule was broken, not merely because it looks unusual."""

AUDITLAB_PROCEDURE = """

Procedure for designated payments (a frequent, easily missed error here). The allocation policy requires each designated payment to be applied in full before period_end (exclusive). Every payment document has a settlement_index with "before" (the boundary) and "unapplied_row_ids" (all UNAPPLIED movements for that payment strictly before the boundary). For EACH payment: read the document, read every listed UNAPPLIED row, and compute credits minus debits with the calculator. A remaining balance above zero is a payment_allocation finding: affected_ids = payment/<payment_id> and invoice/<invoice id from invoice_refs>, claimed_amount_minor = the remaining balance, currency = the payment currency, evidence = payments/<doc_id>, every listed UNAPPLIED row, and policy/allocation. A balance of zero is clean (a split settlement, or a refund that debits UNAPPLIED and credits CASH); a settlement dated after the boundary is not in the index and leaves the balance outstanding.
Worked example (a training company): payment P96A1B85844AC58B4A3FFE303, document D61607F06DDCB27AE2B336871, 5000 minor USD for invoice I29AB36E1E93E6BD7ED4FF574; settlement_index before 2026-12-01T00:00:00+05:00 lists R1736F63CA7ECDA537425D652 (UNAPPLIED credit 5000) and R67E102E91216CD61CA315E3D (UNAPPLIED debit 3000). calculator "5000-3000" = 2000, so: payment_allocation, affected payment/P96A1B85844AC58B4A3FFE303 and invoice/I29AB36E1E93E6BD7ED4FF574, 2000 USD, evidence payments/D61607F06DDCB27AE2B336871, ledger/R1736F63CA7ECDA537425D652, ledger/R67E102E91216CD61CA315E3D, policy/allocation."""

ADDITIONS = {"A": "", "B": GENERAL_METHOD, "C": AUDITLAB_PROCEDURE}

PROVENANCE = {
    "A": "agent.SYSTEM_PROMPT of PROMPT_VERSION llm-auditor-prompt-1.0, unchanged",
    "B": "written from general audit practice; no AuditLab failure, trajectory or truth was consulted",
    "C": {"observed_weakness": [
              "live Gemini 3.1 Flash-Lite pilot, dev family 922101: empty submission on a partial allocation of "
              "3 024 000 minor KZT (one completed attempt; docs/research/gemini)",
              "GPT Pro 1 local curriculum (docs/research/financial-curriculum): the random policy missed a "
              "payment_allocation 3/3 before and after reduction on a train family"],
          "rule_source": "public policy.allocation text and settlement_index field of the public payments table",
          "worked_example": "datasets/open/train/E39A14EA0FB98F9BC3E42C3738182D142 (train split): public records only; "
                            "its truth was read only to confirm the example's amount (2000 USD)",
          "not_used": "dev family 922101 values, any holdout, any final family"},
}


def addition(prompt_variant: str) -> str:
    if prompt_variant not in PROMPT_VARIANTS:
        raise ValueError(f"prompt_variant must be one of {PROMPT_VARIANTS}")
    return ADDITIONS[prompt_variant]


def prompt_set_record() -> dict:
    """Hashes and sizes of the frozen variants, for protocol.json."""
    return {"version": PROMPT_SET_VERSION,
            "variants": {name: {"addition_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                "addition_chars": len(text), "addition_words": len(text.split()),
                                "provenance": PROVENANCE[name]} for name, text in ADDITIONS.items()}}
