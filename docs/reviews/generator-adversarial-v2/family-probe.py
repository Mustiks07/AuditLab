"""Test source-document motif transfer and complete-chain collisions on frozen cohorts."""

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from auditlab.generator import GENERATOR_VERSION, generate_episode
from auditlab.release.rehearsal import selection_manifest
from datasets.audit_families import PUBLISHED, fingerprints
from datasets.audit_subhistories import units

CALIBRATION = range(79132001, 79132025)
HOLDOUT = range(79142001, 79142025)
CONTROLS = range(79152001, 79152007)
SIGNED_SEEDS = (range(79172001, 79172025), range(79182001, 79182025), range(79192001, 79192007))
KINDS = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
INTERACTIONS = (
    ("allocation_fx_same_payment", "payment_allocation", "fx_conversion"),
    ("cutoff_fx_same_invoice", "period_cutoff", "fx_conversion"),
)
CURRENCIES = (("KZT", "USD"), ("USD", "EUR"), ("JPY", "KWD"))
SCHEDULES = {
    "calibration": (("2027-01", "Asia/Almaty"), ("2027-02", "UTC"), ("2027-03", "Europe/Berlin")),
    "holdout": (("2028-05", "Asia/Kathmandu"), ("2028-06", "America/New_York"), ("2028-07", "UTC")),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def records(path):
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def timepoint(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def delta_minutes(later, earlier):
    return (later - earlier) // timedelta(minutes=1)


def graph(public, *, signed=False):
    """No posted records, exchange rates, labels, or config are read."""
    metadata = json.loads((public / "manifest.json").read_bytes())
    period_end = timepoint(metadata["period_end"])
    base = metadata["base_currency"]
    invoices, payments = defaultdict(list), defaultdict(list)
    payment_to_invoice = {}
    for invoice in records(public / "invoices.jsonl"):
        invoices[invoice["invoice_id"]].append(invoice)
    for payment in records(public / "payments.jsonl"):
        for invoice_id in payment["invoice_refs"]:
            payments[invoice_id].append(payment)
            payment_to_invoice[payment["payment_id"]] = invoice_id

    result = {}
    for invoice_id, copies in invoices.items():
        descriptions = {(timepoint(row["issued_at"]), timepoint(row["due_at"]), row["currency"],
                         row["total_minor"]) for row in copies}
        assert len(descriptions) == 1, "fixture contains diverging commercial copies"
        issued, due, currency, invoice_amount = descriptions.pop()
        terms = [(delta_minutes(timepoint(row["timestamp"]), issued),
                  row["currency"] == currency,
                  str(Fraction(row["amount_minor"], invoice_amount)))
                 for row in payments[invoice_id]]
        # Leading group is date-normalized. Keep a separate structural fingerprint
        # without money; an exact ratio is useful context but is no reconciliation.
        remaining = delta_minutes(period_end, issued)
        near_end = 0 < remaining < 1440 if signed else remaining < 1440
        context = (len(copies), currency == base, near_end, delta_minutes(due, issued) // 1440)
        result[invoice_id] = {
            "structure": (context, tuple(sorted((lag, same) for lag, same, _ in terms))),
            "relative": (context, tuple(sorted(terms))),
        }
    return result, payment_to_invoice


def labels(episode, payment_to_invoice):
    """Trusted label assignment used for evaluation only."""
    truth = json.loads((episode / "private" / "truth.json").read_bytes())
    result = defaultdict(set)
    for issue in truth["issues"]:
        for ref in issue["affected_ids"]:
            kind, identity = ref.split("/", 1)
            invoice_id = identity if kind == "invoice" else payment_to_invoice.get(identity)
            if invoice_id is not None:
                result[invoice_id].add(issue["type"])
    return result


def config_at(index, cohort):
    base, foreign = CURRENCIES[index % 3]
    period, zone = SCHEDULES[cohort][index % 3]
    return {
        "config_version": "1.1", "base_currency": base, "currencies": [base, foreign],
        "target_period": period, "timezone": zone,
        "company_size": {"ledger_rows_target": 240, "documents_target": 60},
    }


def build_rows(root, seeds=(CALIBRATION, HOLDOUT, CONTROLS)):
    calibration, holdout, controls = seeds
    cohorts = {}
    for cohort, group in (("calibration", calibration), ("holdout", holdout)):
        episodes = {}
        for index, seed in enumerate(group):
            config = config_at(index, cohort)
            variant = index % 8
            if 1 <= variant <= 4:
                config["issue_counts"] = {KINDS[variant - 1]: 1}
            if variant in {5, 6}:
                interaction, first, second = INTERACTIONS[variant - 5]
                config.update(interaction=interaction, issue_counts={first: 1, second: 1})
            if variant == 7:
                config["clean_distractors"] = ["next_day_settlement"]
            label = f"{cohort}-{seed}"
            episode = root / label
            generate_episode(seed, config, episode)
            episodes[label] = episode
        cohorts[cohort] = episodes
    controlled = {}
    for index, seed in enumerate(controls):
        for ((interaction, first, second), (left, right)) in product(INTERACTIONS, product((0, 1), repeat=2)):
            config = config_at(index, "holdout")
            config.update(interaction=interaction, issue_counts={first: left, second: right},
                          clean_distractors=["next_day_settlement"])
            label = f"control-{seed}-{interaction}-{left}{right}"
            episode = root / label
            generate_episode(seed, config, episode)
            controlled[label] = episode
    cohorts["control"] = controlled
    return cohorts


def invariance(episode, target, *, signed):
    original = episode / "public"
    target.mkdir()
    (target / "manifest.json").write_bytes((original / "manifest.json").read_bytes())
    swaps = {}
    rng = random.Random(79160001)
    for name in ("invoices", "payments"):
        rows = records(original / f"{name}.jsonl")
        rng.shuffle(rows)
        for row in rows:
            for key in ("doc_id", "invoice_id", "payment_id", "entity_id"):
                if key in row:
                    swaps.setdefault(row[key], f"renamed-{len(swaps)}")
                    row[key] = swaps[row[key]]
            if "invoice_refs" in row:
                row["invoice_refs"] = [swaps[ref] for ref in row["invoice_refs"]]
        (target / f"{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    left, _ = graph(original, signed=signed)
    right, _ = graph(target, signed=signed)
    assert {swaps[key]: value for key, value in left.items()} == right
    return len(left)


def collect(episodes, *, signed):
    public = {}
    links = {}
    for name, episode in episodes.items():
        public[name], links[name] = graph(episode / "public", signed=signed)
    return public, links


def supervised_predictions(view, calibration, calibration_labels):
    observations = defaultdict(set)
    observed_episodes = defaultdict(set)
    for name, rows in calibration.items():
        for identity, shapes in rows.items():
            key = shapes[view]
            observations[key].add(frozenset(calibration_labels[name].get(identity, ())))
            observed_episodes[key].add(name)
    return {key: next(iter(classes)) for key, classes in observations.items()
            if len(observed_episodes[key]) >= 2 and len(classes) == 1 and next(iter(classes))}


def apply_predictions(view, vocabulary, rows):
    return {name: {identity: vocabulary.get(shapes[view], frozenset()) for identity, shapes in invoices.items()}
            for name, invoices in rows.items()}


def score(predictions, labels_by_episode):
    result = {kind: Counter() for kind in KINDS}
    for name, invoices in predictions.items():
        for identity, predicted in invoices.items():
            actual = labels_by_episode[name].get(identity, set())
            for kind, metric in result.items():
                if kind in predicted:
                    metric["tp" if kind in actual else "fp"] += 1
                elif kind in actual:
                    metric["fn"] += 1
    return {kind: {name: counts[name] for name in ("tp", "fp", "fn")} for kind, counts in result.items()}


def exact_partial(reference, episodes):
    """Count new full-chain collisions only. Report bounded examples with provenance."""
    kind = "posted_counterparty"
    indexed = defaultdict(list)
    reference_units = Counter({split: 0 for split in reference})
    for split, group in reference.items():
        for name, public in group.items():
            for family, _, _, payload in units(public):
                if family == kind:
                    indexed[digest(payload)].append((split, name))
                    reference_units[split] += 1
    counts = Counter({split: 0 for split in reference})
    examples = []
    seen = set()
    test_units = 0
    for name, episode in episodes.items():
        for family, _, _, payload in units(episode / "public"):
            if family != kind:
                continue
            test_units += 1
            key = digest(payload)
            for group, old_name in indexed.get(key, ()):
                occurrence = (group, old_name, name, key)
                if occurrence in seen:
                    continue
                seen.add(occurrence)
                counts[group] += 1
                if len(examples) < 6:
                    examples.append(dict(reference_group=group, reference=old_name, test=name, sha256=key))
    return {"reference_units": dict(reference_units), "test_units": test_units,
            "counts": dict(counts), "examples": examples}


def whole_overlaps(reference, episodes):
    candidate = [fingerprints(episode / "public") for episode in episodes.values()]
    results = {}
    for split, cases in reference.items():
        reference_hashes = [fingerprints(public) for public in cases.values()]
        results[split] = {"pairs": len(reference_hashes) * len(candidate),
                          **{view: sum(old[view] == new[view] for old in reference_hashes for new in candidate)
                             for view in ("family", "book")}}
    return results


def run(root, *, signed):
    cohorts = build_rows(root, SIGNED_SEEDS) if signed else build_rows(root)
    graphs = {}
    links = {}
    labels_by_cohort = {}
    for name, episodes in cohorts.items():
        graphs[name], links[name] = collect(episodes, signed=signed)
    public_seal = digest(graphs)
    for name, episodes in cohorts.items():
        labels_by_cohort[name] = {case: labels(episode, links[name][case]) for case, episode in episodes.items()}
    tested = {}
    for view in ("structure", "relative"):
        vocabulary = supervised_predictions(view, graphs["calibration"], labels_by_cohort["calibration"])
        tested[view] = {"predictive_motifs": [{"key": key, "classes": sorted(classes)}
                                             for key, classes in sorted(vocabulary.items())], "metrics": {
            name: score(apply_predictions(view, vocabulary, graphs[name]), labels_by_cohort[name])
            for name in ("holdout", "control")
        }}
    selection = selection_manifest()
    internal = {}
    for case in selection["episodes"]:
        episode = root / case["id"]
        generate_episode(case["seed"], case["config"], episode)
        internal[case["id"]] = episode / "public"
    published = {split: {episode.name: episode / "public" for episode in sorted((PUBLISHED / split).iterdir())}
                 for split in ("train", "dev")}
    reference = {**published, "internal": internal,
                 "calibration": {name: ep / "public" for name, ep in cohorts["calibration"].items()}}
    return {"generator": GENERATOR_VERSION, "signed": signed, "public_seal": public_seal,
            "cohorts": {name: {"episodes": len(group), "invoice_units": sum(map(len, graphs[name].values()))}
                        for name, group in cohorts.items()},
            "renamed_control_invoices": invariance(next(iter(cohorts["holdout"].values())), root / "renamed",
                                                  signed=signed),
            "motifs": tested,
            "whole_holdout": whole_overlaps(reference, cohorts["holdout"]),
            "exact_partial_holdout": exact_partial(reference, cohorts["holdout"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signed-followup", action="store_true", help="use fresh seeds and signed cutoff distance")
    args = parser.parse_args()
    with TemporaryDirectory() as temporary:
        result = run(Path(temporary), signed=args.signed_followup)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
