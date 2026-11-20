# Structural shortcut probe

## Pre-registered plan

Recorded before execution on `daf1fc64df37651e14801e0286b9741b30820d35` (generator 1.5, config profile 1.1). The experiment derives two fixed predictors by reading code. There is no fitted model and no seed search.

- Development seed block: integers 945605100 through 945605107 inclusive.
- Held-out seed block: integers 945606200 through 945606223 inclusive. No adjustment to thresholds after generating either block.
- Every seed is used for each of the same eight variants: clean with and without archives, duplicate count 1 with and without archives, duplicate count 2 with archives, standalone allocation count 1, standalone FX count 1, and a clean allocation/FX interaction. Every variant enables the previously added next-day clean control. Currency order, time zone, month and target size vary by seed index and agree across variants at each seed.
- Predictor A counts repeated invoice documents for the same logical invoice ID without reading any postings. Measure `groups >= 2` as a guess about company-level duplicate presence, and `max(groups - 1, 0)` as a guess about count in the fixed-archive cohort. In this baseline, a group counts once regardless of its number of physical documents.
- Predictor B examines only invoices/payments and the published currency exponent. An invoice-linked base-currency payment exactly one day after issuance with an amount that is not a multiple of 1,000 major units is guessed to be a payment-allocation fault. Measure target-level TP, FP and FN. Separately pre-register the stronger exploratory variant `B or (base currency + exactly one day + nonzero receipt minute)`, looking for a public timestamp shortcut after the previous next-day control.
- Truth is used only after public features and predictions for the episode have been computed, and solely to label evaluation. The duplicated document group is neither the evaluator's evidence nor a financial finding itself. A one-day delay or unrounded amount is commercially normal. Neither predictor runs the evaluator, builds a contest submission or provides proof suitable for grading.
- Confirm with one clean/fault comparison plus independent held-out counts. Report the intentionally balanced synthetic cohort and class-conditional statistics; do not extrapolate to distribution or contest scores.

## Baseline result

Exact archived source `daf1fc64df37651e14801e0286b9741b30820d35` was run with Windows and Python 3.12.14 using installed tzdata 2026.6. The pre-registration file before this results section was appended had SHA-256 `1203196122d3b02fb6fa7e338b96b181d09e4c2557f87cdf923eb8797be7d391`. This is a local seal.

| Public-document predictor | 64 development books | 192 held-out books |
|---|---|---|
| A: at least two archive groups, duplicate company TP/FP/FN | 16/0/8 | 48/0/24 |
| A: group count minus one, exact requested duplicate count where archive enabled | 48/48 | 144/144 |
| B: amount, allocation payment TP/FP/FN | 7/0/1 | 19/0/5 |
| B or minute, allocation payment TP/FP/FN | 8/0/0 | 23/0/1 |
| Clean/one-fault pairs with the same archive group count | 0/8 | 0/24 |
| Clean/one-fault pairs with the same next-day (amount, receipt, issuance) multiset | 0/8 | 1/24 |

A has a direct mechanism. The clean archive option calls `add_duplicate(enabled=False)` once. Each requested duplicate calls the same source-document builder again and posts the second recognition. Thus primary documents alone can reveal an episode's duplicate count in this known profile, while primary documents alone cannot distinguish the two accounting states of a given group. The no-archive and one-error variant explains the 24 misses for the threshold of two.

B has a separate direct mechanism. The clean next-day control uses `_add_paid_invoices`, starts at hour precision and rounds the principal to multiples of 1,000 major units. Standalone allocation `Company.allocation_issue` uses a multiple of 100 and independently samples minute precision. At held-out seed 945606200, KZT, the clean next-day payment is 44,000 KZT, received 10.11.2026 at 17:00. The affected next-day payment is 67,700 KZT, received 20.11.2026 at 17:27. The exact sum and minute are commercially normal in either state. These metadata-only classifiers read neither ledger rows nor the settlement index.

To reproduce, copy `structure-probe.py` under the same relative docs path into a checkout at the baseline SHA. From that checkout with the project dependencies:

```sh
python docs/reviews/generator-adversarial-v2/structure-probe.py --output artifacts/structure-baseline-results
python docs/reviews/generator-adversarial-v2/structure-probe.py --counterexample-only --output artifacts/structure-baseline-example
```

Both commands need a fresh output path. The latter prints the same first held-out seed's counts and stores its clean and affected metadata in `results.json`. The experiment pairs configurations within seed, so 256 snapshots are 32 seed clusters. The measured grouping does not establish classifier quality on unexposed configurations. The baseline supports a general concern about generated source slots, not a claim that arbitrary document similarity constitutes leakage.

## Public counterexamples on generator 1.6

Re-run the same commands on the review branch with new output paths. At seed 945606200 and the same KZT configuration:

- Clean and one-duplicate companies each show six archive groups. The two documents `invoices/DD6C07F41F28A43446C85559F` and `invoices/D842E7C08DDDE6805970E828E` have the same logical invoice `IFCEAC1518CD0BB56CD6DBC98` and nominal amount 2,144,800 tiyn. The clean ledger has one AR recognition, `ledger/R2C591948E6100FF9BD86E600`, and the faulty ledger adds another distinct one, `ledger/R5FDCF24A89CE0CF832617F4C`, for the same amount. The duplicated recognition is 2,144,800 tiyn (21,448 KZT).
- Clean and one-allocation-fault companies show an identical `payments/D761231F6643FEA796AD18D72` for 5,070,000 tiyn, received 13.11.2026 10:52 UTC+05 exactly one day after the invoice. Both have the same two settlement index references. `ledger/R82F279890CE7EA1E96C62A7F` credits 5,070,000 tiyn. `ledger/R0F6D581420DAC8F76E2A08F3` debits 5,070,000 clean, or 3,042,000 faulty. Credits minus debits are 0 or 2,028,000 tiyn (20,280 KZT).

The pairs expose opposite outcomes using the same metadata heuristic. The direct ledger calculations establish why the outcomes differ. The first held-out cluster was rerun on the later committed generator at `16d8531d3ee49996774a6078f6eb94470c4cfcc7`: all eight books generated, and both feature equalities held.

The complete 256-book rerun on the matched-control 1.6 candidate used the same seeds, configurations and rules. The three generator digests were `f1c22082a3612d8b6b9cba252e2a93c8d8d1c3defbc9789da5bc93a3bd8f0192` for `__init__.py`, `bc09ad5902ffbcd49eee3ab9f12694578a2a547e05dc984b945add638cb38f60` for `core.py`, and `6c10da707a35aebb9ff4cd4a74cb79e4289a1c4967a10b398cc5eebe3dfb3e71` for `scenarios.py`.

A second full 256-book run gave identical counts after broader clean-background dates and amounts and variable future context for the cutoff × FX interaction. That run's `__init__.py` digest was `b9464938fb6dae77bf275425770dbe63fad0ace38e60666e72dda288916884bf`, with the same `core.py` and `scenarios.py`. These two full runs predate the filler-fit and treasury timing changes. The exact later committed check above is the first held-out cluster only.

| Public-document predictor | 64 development books on 1.6 | 192 held-out books on 1.6 |
|---|---|---|
| A: at least two archive groups, duplicate company TP/FP/FN | 16/32/8 | 48/96/24 |
| A: group count minus one, exact duplicate count where archive enabled | 0/48 | 0/144 |
| B: amount, allocation payment TP/FP/FN | 8/328/0 | 19/933/5 |
| B or minute, allocation payment TP/FP/FN | 8/368/0 | 23/1081/1 |
| Equal archive group count for clean/one-fault pairs | 8/8 | 24/24 |
| Equal next-day metadata for clean/one-fault pairs | 8/8 | 24/24 |

The spike in false detections is expected once each episode contains several clean candidate payments. The result shows how the measured feature loses discrimination. New learned combinations and hidden scenarios were outside this experiment. Explicit configurations without archive or allocation distractors can still disclose those selected structures by construction. The reserve treasury count can remain a proxy for commercial posting volume. The later implementation also anchored treasury timestamps independently of the first transfer's planned row count. Ledger-level predictors were outside this probe.
