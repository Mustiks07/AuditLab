"""Reviewed internal SPEC §14 evidence requirements, not an organizer certificate.

Exact executed test IDs are required. A green suite name, test source on disk, or
old receipt cannot substitute for a test result in the current verification run.
"""
import re

GROUPS = {
    "contract_and_isolation": ["API-01", "API-02", "API-03", "API-04", "API-05", "ISO-01", "ISO-02", "ISO-03"],
    "data_and_truth": ["DAT-01", "DAT-02", "DAT-03", "DAT-04", "DAT-05", "VER-01"],
    "reward": ["MAT-01", "MAT-02", "MAT-03", "MAT-04"],
    "reproducibility": ["REP-01", "REP-02", "REP-03"],
    "engineering": ["OPS-01", "OPS-02", "UI-01"],
}


def cases(suite, owner, *names):
    return [(suite, owner + ".test_" + name) for name in names]


REQUIRED = {
    "API-01": cases("runner", "test_acceptance_api.AcceptanceApi", "api01_concurrent_real_environments_and_reset_do_not_cross")
        + cases("runner", "test_public_schemas.RealPolicies", "public_policies_and_oracle_in_both_modes"),
    "API-02": cases("simulator", "test_environment.PublicSessionTests", "pages_are_stable_complete_and_bound_to_table_and_episode", "bad_requests_have_stable_errors_without_echoing_input")
        + cases("runner", "test_env.Lifecycle", "remaining_steps_counts_every_inspection_once")
        + cases("runner", "test_env.Termination", "tool_with_bad_args_costs_a_step"),
    "API-03": cases("runner", "test_acceptance_api.AcceptanceApi", "api03_submit_steps_1_99_100_real_session_and_evaluator")
        + cases("runner", "test_env.Termination", "hundredth_inspection_is_truncation_with_empty_answer", "hundredth_inspection_failing_is_still_truncation", "repeat_after_finish_changes_nothing")
        + cases("runner", "test_env.Timeout", "deadline_before_action_truncates_with_accepted_count", "time_exactly_at_limit_and_custom_limit")
        + cases("runner", "test_finalization.DepthBoundary", "issue_23_example_both_modes_direct_api", "budget_and_timeout_finalize_once_with_hostile_last_action"),
    "API-04": cases("simulator", "test_environment.PublicSessionTests", "bad_requests_have_stable_errors_without_echoing_input", "calculator_uses_bounded_decimal_arithmetic")
        + cases("runner", "test_env.Leaks", "session_exception_and_extra_fields_do_not_leak")
        + cases("runner", "test_public_schemas.SchemaAndGolden", "every_reachable_error_code_is_exercised_and_messages_match_the_code"),
    "API-05": cases("runner", "test_acceptance_api.AcceptanceApi", "api05_external_stdlib_adapter_all_actions_and_golden_files")
        + cases("runner", "test_public_schemas.ExternalAdapter", "adapter_source_imports_only_the_standard_library", "isolated_adapter_validates_the_live_protocol_in_both_modes")
        + cases("runner", "test_public_schemas.SchemaAndGolden", "golden_examples_are_reproduced_by_the_real_components", "valid_examples_pass_and_each_invalid_one_fails_only_because_of_its_defect"),
    "ISO-01": cases("runner", "test_docker_boundary.RealDockerBoundary", "canary_files_network_and_trusted_code_are_unavailable")
        + cases("runner", "test_isolation_export.Isolation", "child_gets_clean_env_and_empty_cwd", "docker_profile_has_no_network_volumes_or_caps")
        + cases("runner", "test_transport_limits.TransportRegressions", "closed_cli_refuses_unsafe_backend"),
    "ISO-02": cases("runner", "test_env.Leaks", "iso02_no_closed_marker_in_any_agent_channel")
        + cases("runner", "test_public_schemas.ClosedMode", "closed_data_injected_at_any_depth_of_real_messages_is_rejected")
        + cases("runner", "test_public_schemas.Exports", "fresh_train_export_and_closed_trusted_trajectory")
        + cases("demo", "test_console.Isolation", "web_side_never_returns_truth_or_closed_runs", "web_modules_do_not_touch_truth"),
    "ISO-03": cases("runner", "test_env.Leaks", "iso03_same_public_snapshot_different_truth_same_messages")
        + cases("runner", "test_public_schemas.ClosedMode", "same_public_different_truth_gives_identical_valid_messages"),
    "DAT-01": cases("simulator", "test_source_book.SourceBookTests", "source_book_precedes_all_eight_interaction_states", "source_book_contains_clean_replacement_for_each_defect", "balanced_but_incorrect_source_is_rejected_before_application")
        + cases("simulator", "test_generator_15.Generator15Tests", "full_clean_book_hashes_transition_and_private_boundary", "missing_transition_change_is_rejected_even_with_updated_hash")
        + cases("verifier", "test_verifier.ControlPairTests", "pair_matches_committed_manual_answer")
        + cases("verifier", "test_public_edge_cases.PublicFinancialEdgeTests", "same_document_shortcut_can_mean_clean_or_unallocated"),
    "DAT-02": cases("simulator", "test_scenarios.ScenarioTests", "both_interaction_matrices")
        + cases("simulator", "test_profile_11.Profile11Tests", "interaction_four_states_share_sources_and_independent_effects")
        + cases("simulator", "test_generator_15.Generator15Tests", "all_interaction_toggles_share_identical_complete_clean_book")
        + cases("verifier", "test_verifier.SingleClassTests", "payment_allocation_and_clean_tranches_and_refund", "fx_conversion", "period_cutoff_next_month_payment_alone_is_clean", "duplicate_invoice")
        + cases("verifier", "test_verifier.InteractionTests", "interaction_a_four_states", "interaction_b_four_states"),
    "DAT-03": cases("verifier", "test_public_edge_cases.PublicFinancialEdgeTests", "repeated_dst_hour_and_half_up_in_zero_two_three_decimal_bases")
        + cases("verifier", "test_verifier.SingleClassTests", "currency_exponents_and_rounding_point", "payment_allocation_and_clean_tranches_and_refund", "fx_rate_date_is_company_local_date")
        + cases("verifier", "test_verifier.BoundaryTests", "allocation_boundary_local_and_utc", "invoice_cutoff_boundary")
        + cases("simulator", "test_generator_15.Generator15Tests", "observable_fx_across_forty_seeds_and_three_minor_unit_exponents"),
    "DAT-04": cases("integration", "test_components.ComponentPairTests", "public_session_reads_pair_evidence", "generated_auditor_score_trajectory_and_verifier", "runner_control_pair_submit_after_four_inspections")
        + cases("simulator", "test_policies.PublicPolicyTests", "largest_supported_snapshot_fits_reconcile_budget")
        + cases("runner", "test_public_schemas.RealSnapshots", "every_record_of_every_shipped_snapshot"),
    "DAT-05": cases("integration", "test_control_pair.PublicControlPairTests", "boundary_order_and_timezone", "public_index_proves_completeness_on_each_side")
        + cases("simulator", "test_scenarios.ScenarioTests", "archive_copies_need_two_recognitions_to_be_a_duplicate")
        + cases("simulator", "test_generation.GenerationTests", "clean_history_has_refunds_split_payments_and_equal_amounts")
        + cases("verifier", "test_verifier.SingleClassTests", "duplicate_invoice", "payment_allocation_and_clean_tranches_and_refund"),
    "MAT-01": cases("integration", "test_components.ComponentPairTests", "evaluator_six_numerical_examples")
        + cases("evaluator", "test_scoring.SectionNineTable", "alternative_complete_set", "foreign_document_replaces_required_evidence", "extra_evidence_is_harmless"),
    "MAT-02": cases("evaluator", "test_scoring.MatchingOptimality", "greedy_first_choice_loses_a_match", "permutation_stable")
        + cases("evaluator", "test_scoring.SectionNineTable", "duplicate_is_false_positive", "permuted_duplicate_is_false_positive")
        + cases("evaluator", "test_scoring.SemanticAndStructural", "flag_all_scores_by_quality"),
    "MAT-03": cases("evaluator", "test_scoring.SemanticAndStructural", "one_unit_off_is_fp", "wrong_currency_and_lowercase_are_fp", "structural_failures_give_minus_one", "size_limits_boundaries")
        + cases("runner", "test_env.Termination", "structurally_bad_submit_ends_with_minus_one", "recognised_submit_with_broken_body_terminates_minus_one"),
    "MAT-04": cases("evaluator", "test_scoring.SectionNineTable", "mat04_costs")
        + cases("integration", "test_components.ComponentPairTests", "runner_control_pair_submit_after_four_inspections")
        + cases("runner", "test_env.Termination", "hundredth_inspection_is_truncation_with_empty_answer")
        + cases("runner", "test_golden_exports.GoldenExports", "layout_status_and_scores"),
    "REP-01": cases("runner", "test_env.Lifecycle", "rep01_same_actions_same_trajectory")
        + cases("simulator", "test_profile_11.Profile11Tests", "seed_changes_case_nominals_and_dates_without_breaking_replay")
        + cases("simulator", "test_datasets.DatasetTests", "fresh_build_reproduces_every_shipped_file")
        + cases("integration", "test_rehearsal.RehearsalTests", "closed_replay_detects_changed_trusted_reward_without_public_change"),
    "REP-02": cases("integration", "test_rehearsal.RehearsalTests", "real_oracle_closed_trajectory_and_transformed_pair")
        + cases("metamorphic", "test_transforms.TransformTests", "rename_and_permute_preserve_answer"),
    "REP-03": cases("simulator", "test_families.EconomicsFamilyTests", "published_internal_split_reports_known_twins_only_inside_their_split", "transformed_twin_crosses_split_and_late_variant_stays_in_family")
        + cases("simulator", "test_transformed_policy.TransformedPolicyTests", "reconcile_rediscovers_evidence_on_open_train_and_dev_pairs", "reconcile_rediscovers_evidence_on_frozen_internal_pairs")
        + cases("simulator", "test_generated_pair.GeneratedPairTests", "same_seed_snapshots_bind_cursors_to_public_content"),
    "VER-01": cases("verifier", "test_crosscheck.CrosscheckTests", "controlled_truth_distortions_are_detected_by_kind", "counterexample_is_reduced_on_a_copy")
        + cases("integration", "test_evaluator_mutation.EvaluatorMutationTests", "manual_counterexample_kills_double_credit_mutant"),
    "OPS-01": cases("integration", "test_offline_bundle.OfflineBundleTests", "check_mode_needs_no_docker_and_checks_every_payload", "runtime_is_networkless_and_uses_only_result_and_trusted_socket_mounts")
        + cases("integration", "test_rehearsal_evidence.EvidenceTests", "unrelated_revision_cannot_pass", "skip_or_missing_evidence_cannot_pass"),
    "OPS-02": [],
    "UI-01": cases("demo", "test_console.LabEndToEnd", "samples_live_change_rescore_replay")
        + cases("demo", "test_console.Api", "empty_state", "api_equals_cli_and_pair_flow")
        + cases("integration", "test_cli.ReleaseCliTests", "replay_checks_real_export_and_rejects_unknown_run"),
}

# A passed partial test set cannot close an explicitly uncovered requirement.
GAPS = {
    "OPS-01": "Final offline launch, controller limits and bundle receipt must be checked after this process exits; clean-host/whole-host memory evidence is separate.",
    "OPS-02": "Independent human receiver must verify the exact candidate archive on a clean host.",
}


def _collection(rows, expected):
    ids = [row.get("id") for row in rows]
    if len(ids) != len(set(ids)) or set(ids) - set(expected):
        return "failed", "duplicate or unexpected evidence IDs"
    if any(row.get("status") == "failed" for row in rows):
        return "failed", "executed check failed"
    if set(ids) != set(expected) or any(row.get("status") != "passed" for row in rows):
        return "not_checked", "missing or incomplete evidence"
    return "passed", ""


def _source_book_bound(row):
    receipt = row.get("source_book") or {}
    hashes = row.get("snapshot_sha256") or {}
    source, target = receipt.get("source_files_sha256", {}), receipt.get("target_files_sha256", {})
    names = {"manifest.json", "policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv"}
    return (receipt.get("status") == "passed" and receipt.get("source_findings") == 0
            and set(source) == set(target) == names
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in [*source.values(), *target.values()])
            and all(hashes.get("private/source_book/" + name) == source[name] for name in names)
            and all(hashes.get("public/" + name) == target[name] for name in names)
            and all(receipt.get(field) and hashes.get("private/" + name) == receipt[field] for name, field in (
                ("source_transition.json", "transition_sha256"), ("provenance.json", "provenance_sha256"),
                ("truth.json", "truth_sha256"))))


def matrix(report=None, expected_revision=None):
    report = report or {}
    revision = report.get("commit")
    provenance_ok = (isinstance(expected_revision, str) and re.fullmatch(r"[0-9a-f]{40}", expected_revision)
                     and revision == expected_revision and report.get("end_commit") == revision
                     and report.get("dirty") is not True and report.get("end_dirty") is not True
                     and bool(report.get("run_id")))
    suites = {}
    for suite in report.get("checks", []):
        suites.setdefault(suite.get("id"), []).append(suite)
    result = []
    for criterion, groups in GROUPS.items():
        for group in groups:
            evidence, missing, failures = [], [], []
            for suite_name, test_id in REQUIRED[group]:
                containers = suites.get(suite_name, [])
                if len(containers) != 1:
                    missing.append(f"{suite_name}/{test_id}: no unique suite receipt")
                    continue
                suite = containers[0]
                if (suite.get("evidence_run_id") != report.get("run_id") or
                        suite.get("evidence_revision") != revision or not provenance_ok):
                    missing.append(f"{suite_name}/{test_id}: stale or unbound execution")
                    continue
                records = [row for row in suite.get("records", []) if row.get("id") == test_id]
                failed_subtests = [row for row in suite.get("records", []) if
                    str(row.get("id", "")).startswith(test_id + " (") and row.get("status") == "failed"]
                skipped_subtests = [row for row in suite.get("records", []) if
                    str(row.get("id", "")).startswith(test_id + " (") and row.get("status") != "passed"]
                if failed_subtests or any(row.get("status") == "failed" for row in records):
                    failures.append(f"{suite_name}/{test_id}: failed")
                elif skipped_subtests or len(records) != 1 or records[0].get("status") != "passed" or suite.get("tests_run", 0) <= 0:
                    missing.append(f"{suite_name}/{test_id}: missing, skipped or duplicate")
                else:
                    evidence.append(f"{suite_name}/{test_id}")
            if not provenance_ok:
                missing.append("Current clean source revision and run provenance are required.")
            rehearsal = report.get("rehearsal") or {}
            if group in {"DAT-01", "DAT-04", "REP-01", "REP-02"}:
                key = "pairs" if group == "REP-02" else "datasets"
                expected = {f"pair-{i:02d}" if key == "pairs" else f"internal-{i:02d}" for i in range(1, 21)}
                rows = rehearsal.get(key, [])
                status, reason = _collection(rows, expected)
                if rehearsal.get("evidence_revision") != revision or rehearsal.get("evidence_run_id") != report.get("run_id"):
                    status, reason = "not_checked", "stale or unbound rehearsal receipt"
                if status == "passed":
                    valid = all((_source_book_bound(row) if group == "DAT-01" else
                                 row.get("addressability", {}).get("status") == "passed" if group == "DAT-04" else
                                 row.get("regeneration_identical") is True if group == "REP-01" else
                                 row.get("oracle_score_agrees") is True and row.get("transformed_answer_agrees") is True
                                 and row.get("replay", {}).get("status") == "passed") for row in rows)
                    if not valid:
                        status, reason = "not_checked", "required detailed receipt is absent or incomplete"
                if status != "passed":
                    (failures if status == "failed" else missing).append(f"rehearsal.{key}: {reason}")
                else:
                    evidence.append(f"rehearsal.{key}: 20 detailed passed receipts")
            if group in GAPS:
                missing.append(GAPS[group])
            result.append({"id": group, "criterion": criterion,
                "status": "failed" if failures else "not_checked" if missing else "passed",
                "scope": "internal SPEC §14; not organizer acceptance",
                "required_tests": [f"{suite}/{test}" for suite, test in REQUIRED[group]],
                "evidence": evidence, "missing": missing, "failures": failures,
                "detail": "; ".join(failures + missing) if failures or missing else "All explicitly required internal checks executed successfully on this source revision."})
    return result
