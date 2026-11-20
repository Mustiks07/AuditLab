# Generator 1.5: observable FX faults and a reviewable original book

The failing configuration was seed `0`, profile `1.1`, base JPY, currencies
`["JPY", "EUR"]`, interaction `allocation_fx_same_payment`, no distractors,
and one `fx_conversion`. EUR 61 valued at rates 1.21 and 1.22 rounds to the same
JPY 74. Version 1.4 rejected this valid requested configuration with
`unintended_issue_counts`, rather than producing the requested fault.

Version 1.5 selects the earliest later date whose quote yields a different
rounded valuation, bounded by the existing deterministic 31-day schedule. It
selects this date irrespective of the enabled defect, preserving identical
source books across interaction toggles. There are no retries or new inputs.
The next-day rate and all public data remain unchanged when already observable.

Profile 1.1 also writes the complete validated source book to
`private/source_book/`. Its six files use the public snapshot formats, so an
independent reader can recompute the clean financial answer without generator
code. `private/source_transition.json` records each added, removed or changed
ledger/document object and links it to the provenance mutation indexes.
Provenance binds all source/target file hashes and the transition hash.

`tools/check_source_books.py` validates those bindings, replays the transition
and checks it accounts for every change, then runs the independent SQL verifier
on separate copies of clean and final files. It opens the final truth only after
both answers have been calculated. A missing original returns `not_checked`;
a damaged hash or incomplete transition fails. This is a trusted review tool,
not an untrusted-process isolation test. Hashes bind bytes, not authorship.

## Measured checks

On Python 3.11.14 with the pinned requirements:

- **43/43 simulator tests**, including 240 new FX cases: 40 seeds × 3 base
  currencies (JPY/USD/KWD, exponents 0/2/3) × both interaction types. Ordinary
  `reconcile` and independent SQL agreed with every FX answer.
- **24/24 interaction states** (3 bases × 2 interactions × 4 toggles) shared
  byte-identical complete clean books; every clean book yielded no SQL findings
  or notes.
- **20/20 frozen internal original books**, through 2,000 ledger rows and 200
  documents, passed hashes, lossless transition replay and independent SQL
  checks. Every original was clean; final answers matched truth. The exact
  file hashes and checker/generator implementation hashes are in
  [the receipt](generator-1.5-source-books.json).
- **160/160 published train/dev files** remain byte-identical on regeneration.
  Their legacy generator path/version remains 1.1.
- **140/140 public/truth files** across the 20 frozen internal episodes remain
  byte-identical to generator 1.4 at `a301b16b825c5575393d94f92ac1707f42e292cb`.
  New private provenance and source-book files intentionally differ.
- Mutating an original file is rejected by hash validation; omitting a transition
  change is rejected even after updating its hash.

Reproduce the simulator tests:

```sh
python -m unittest discover -s tests/simulator -v
```

Generate the fixed internal selection and check its originals (use a fresh
`artifacts/source-books` path):

```sh
python - <<'PY'
from pathlib import Path
from auditlab.generator import generate_episode
from auditlab.release.rehearsal import selection_manifest
for row in selection_manifest()["episodes"]:
    generate_episode(row["seed"], row["config"], Path("artifacts/source-books") / row["id"])
PY
python tools/check_source_books.py artifacts/source-books/internal-* --output artifacts/source-books/report.json
```

These are internal correctness results. They do not replace the independent
recipient's offline test, container isolation/resource checks, full release
rehearsal or the organizers' unpublished tests. Actual DST transition-hour tests
are tracked separately: the generator's current varied event times are daytime.
