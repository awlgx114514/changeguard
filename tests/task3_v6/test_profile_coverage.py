from __future__ import annotations

from dataclasses import replace

import pytest

from benchmarks.task3_v6.campaign import CampaignIdentity, CampaignProfileType, CampaignStore
from benchmarks.task3_v6.profile_catalog import (
    CoverageStage,
    ProfileAcceptance,
    ProfileClass,
    catalog_sha256,
    evaluate_profile_coverage,
    load_profile_catalog,
    profile_acceptance_from_campaign,
    profile_catalog_from_dict,
    ProfileCatalogError,
)


def _accepted_profiles(catalog, stage: CoverageStage):
    return tuple(
        ProfileAcceptance(
            campaign_id=f"synthetic-{entry.profile_id}",
            profile_id=entry.profile_id,
            profile_class=entry.profile_class,
            catalog_version=catalog.version,
            catalog_hash=catalog.sha256,
            stage=stage,
            admission=(
                "PREFREEZE_ACCEPTED"
                if stage is CoverageStage.PREFREEZE
                else "PASS"
            ),
            source_reference=f"synthetic-test:{entry.profile_id}",
        )
        for entry in catalog.profiles
        if entry.required
    )


def test_catalog_contains_the_frozen_profile_universe():
    catalog = load_profile_catalog()

    assert catalog.version == "task3-profile-catalog-v1"
    assert [entry.profile_id for entry in catalog.profiles if entry.profile_class is ProfileClass.CORRECT] == [
        "KNOWN_FIX",
        "CA_DIFFERENT_TRANSPORT_HELPER",
        "CA_LEGAL_CLEANUP_ORDERING",
        "CA_LATE_CACHE_UPDATE",
        "CA_ASYNC_CLOSED_STREAM_PROBE",
        "CA_BUFFERING_SPOOLING",
    ]
    assert [entry.profile_id for entry in catalog.profiles if entry.profile_class is ProfileClass.REQUIRED_WRONG] == [
        "W1",
        "W2",
        "W3",
        "W4",
        "W7",
        "DESTRUCTIVE_WAIT",
        "WAIT_FOR_DESCENDANT_EOF",
    ]
    assert [entry.profile_id for entry in catalog.profiles if entry.profile_class is ProfileClass.EXCLUDED_LIMITATION] == [
        "W5",
        "W6",
    ]
    assert sum(entry.required for entry in catalog.profiles) == 13
    assert all(
        entry.expected_admission_kind == "ALL_FORMAL_RUNS_PASS"
        for entry in catalog.profiles
        if entry.profile_class is ProfileClass.CORRECT
    )
    assert all(
        entry.expected_admission_kind == "ALL_FORMAL_RUNS_CONFIRMED_REJECTION"
        for entry in catalog.profiles
        if entry.profile_class is ProfileClass.REQUIRED_WRONG
    )
    assert all(
        not entry.required
        and not entry.formal_sample_eligible
        and entry.expected_admission_kind == "EXCLUDED_FROM_FORMAL_ADMISSION"
        for entry in catalog.profiles
        if entry.profile_class is ProfileClass.EXCLUDED_LIMITATION
    )


def test_one_correct_campaign_does_not_complete_global_coverage():
    catalog = load_profile_catalog()
    record = ProfileAcceptance(
        campaign_id="known-fix-formal",
        profile_id="KNOWN_FIX",
        profile_class=ProfileClass.CORRECT,
        catalog_version=catalog.version,
        catalog_hash=catalog.sha256,
        stage=CoverageStage.FORMAL,
        admission="PASS",
        source_reference="synthetic-test:known-fix",
    )

    result = evaluate_profile_coverage(catalog, (record,), CoverageStage.FORMAL)

    assert result.coverage_status == "INCOMPLETE"
    assert result.required_correct_completed == 1
    assert result.required_correct_total == 6
    assert result.required_wrong_completed == 0
    assert result.required_wrong_total == 7


def test_w2_prefreeze_acceptance_does_not_complete_global_coverage():
    catalog = load_profile_catalog()
    record = ProfileAcceptance(
        campaign_id="w2-prefreeze",
        profile_id="W2",
        profile_class=ProfileClass.REQUIRED_WRONG,
        catalog_version=catalog.version,
        catalog_hash=catalog.sha256,
        stage=CoverageStage.PREFREEZE,
        admission="PREFREEZE_ACCEPTED",
        source_reference="synthetic-test:w2-prefreeze",
    )

    result = evaluate_profile_coverage(catalog, (record,), CoverageStage.PREFREEZE)

    assert result.coverage_status == "INCOMPLETE"
    assert result.required_wrong_completed == 1
    assert result.required_wrong_total == 7
    assert result.profile_results["W2"].status == "PREFREEZE_ACCEPTED"


def test_unbound_legacy_w2_prefreeze_evidence_is_not_backfilled():
    catalog = load_profile_catalog()
    legacy = ProfileAcceptance(
        campaign_id="w2-legacy-unbound",
        profile_id="W2",
        profile_class=ProfileClass.REQUIRED_WRONG,
        catalog_version=None,
        catalog_hash=None,
        stage=CoverageStage.PREFREEZE,
        admission="PREFREEZE_ACCEPTED",
        source_reference="legacy:w2-prefreeze",
    )

    result = evaluate_profile_coverage(catalog, (legacy,), CoverageStage.PREFREEZE)

    assert result.coverage_status == "INCOMPLETE"
    assert result.required_wrong_completed == 0
    assert result.profile_results["W2"].status == "CATALOG_BINDING_MISMATCH"


def test_identical_duplicate_acceptance_contributes_once():
    catalog = load_profile_catalog()
    records = _accepted_profiles(catalog, CoverageStage.FORMAL)
    w2 = next(record for record in records if record.profile_id == "W2")

    result = evaluate_profile_coverage(
        catalog,
        (*records, w2),
        CoverageStage.FORMAL,
    )

    assert result.coverage_status == "PASS"
    assert result.required_wrong_completed == 7
    assert result.profile_results["W2"].status == "FORMAL_ACCEPTED"


def test_conflicting_profile_acceptances_are_visible_and_incomplete():
    catalog = load_profile_catalog()
    records = _accepted_profiles(catalog, CoverageStage.FORMAL)
    accepted = next(record for record in records if record.profile_id == "W2")
    failed = replace(
        accepted,
        campaign_id="w2-conflicting-campaign",
        admission="INCOMPLETE",
        source_reference="catalog-bound:w2-conflicting",
    )

    result = evaluate_profile_coverage(
        catalog,
        (*records, failed),
        CoverageStage.FORMAL,
    )

    w2_result = result.profile_results["W2"]
    assert result.coverage_status == "INCOMPLETE"
    assert result.required_wrong_completed == 6
    assert w2_result.status == "COVERAGE_CONFLICT"
    assert w2_result.conflicting_acceptances == (failed, accepted)


def test_wrong_catalog_hash_duplicate_does_not_change_current_coverage():
    catalog = load_profile_catalog()
    records = _accepted_profiles(catalog, CoverageStage.FORMAL)
    current = next(record for record in records if record.profile_id == "W2")
    stale = replace(
        current,
        catalog_hash="0" * 64,
        admission="INCOMPLETE",
        source_reference="stale-catalog:w2",
    )

    result = evaluate_profile_coverage(
        catalog,
        (*records, stale),
        CoverageStage.FORMAL,
    )

    assert result.coverage_status == "PASS"
    assert result.required_wrong_completed == 7
    assert result.profile_results["W2"].status == "FORMAL_ACCEPTED"
    assert result.profile_results["W2"].source_reference == current.source_reference


def test_wrong_profile_class_duplicate_does_not_change_current_coverage():
    catalog = load_profile_catalog()
    records = _accepted_profiles(catalog, CoverageStage.FORMAL)
    current = next(record for record in records if record.profile_id == "W2")
    wrong_class = replace(
        current,
        campaign_id="w2-wrong-class-duplicate",
        profile_class=ProfileClass.CORRECT,
        source_reference="wrong-class:w2",
    )

    result = evaluate_profile_coverage(
        catalog,
        (*records, wrong_class),
        CoverageStage.FORMAL,
    )

    assert result.coverage_status == "PASS"
    assert result.required_wrong_completed == 7
    assert result.profile_results["W2"].status == "FORMAL_ACCEPTED"
    assert result.profile_results["W2"].source_reference == current.source_reference


def test_pass_acceptances_for_excluded_profiles_do_not_affect_required_coverage():
    catalog = load_profile_catalog()
    required_records = _accepted_profiles(catalog, CoverageStage.FORMAL)
    excluded_records = tuple(
        ProfileAcceptance(
            campaign_id=f"excluded-{profile_id}",
            profile_id=profile_id,
            profile_class=ProfileClass.EXCLUDED_LIMITATION,
            catalog_version=catalog.version,
            catalog_hash=catalog.sha256,
            stage=CoverageStage.FORMAL,
            admission="PASS",
            source_reference=f"excluded-acceptance:{profile_id}",
        )
        for profile_id in ("W5", "W6")
    )

    result = evaluate_profile_coverage(
        catalog,
        (*required_records, *excluded_records, *excluded_records),
        CoverageStage.FORMAL,
    )

    assert result.coverage_status == "PASS"
    assert result.required_correct_total == 6
    assert result.required_wrong_total == 7
    assert result.required_correct_completed == 6
    assert result.required_wrong_completed == 7
    assert result.excluded_total == 2
    assert all(
        result.profile_results[profile_id].status == "EXCLUDED_FROM_FORMAL_ADMISSION"
        for profile_id in ("W5", "W6")
    )


def test_complete_synthetic_profile_acceptances_pass_each_requested_stage():
    catalog = load_profile_catalog()

    prefreeze = evaluate_profile_coverage(
        catalog,
        _accepted_profiles(catalog, CoverageStage.PREFREEZE),
        CoverageStage.PREFREEZE,
    )
    formal = evaluate_profile_coverage(
        catalog,
        _accepted_profiles(catalog, CoverageStage.FORMAL),
        CoverageStage.FORMAL,
    )

    assert prefreeze.coverage_status == "PASS"
    assert formal.coverage_status == "PASS"
    assert prefreeze.required_correct_completed == 6
    assert prefreeze.required_wrong_completed == 7
    assert formal.required_correct_completed == 6
    assert formal.required_wrong_completed == 7


def test_missing_correct_alternative_is_reported_by_profile_id():
    catalog = load_profile_catalog()
    records = tuple(
        record
        for record in _accepted_profiles(catalog, CoverageStage.FORMAL)
        if record.profile_id != "CA_LATE_CACHE_UPDATE"
    )

    result = evaluate_profile_coverage(catalog, records, CoverageStage.FORMAL)

    assert result.coverage_status == "INCOMPLETE"
    assert result.required_correct_missing == ("CA_LATE_CACHE_UPDATE",)


def test_excluded_limitations_do_not_block_required_coverage():
    catalog = load_profile_catalog()

    result = evaluate_profile_coverage(
        catalog,
        _accepted_profiles(catalog, CoverageStage.FORMAL),
        CoverageStage.FORMAL,
    )

    assert result.coverage_status == "PASS"
    assert result.excluded_total == 2
    assert result.profile_results["W5"].status == "EXCLUDED_FROM_FORMAL_ADMISSION"
    assert result.profile_results["W6"].status == "EXCLUDED_FROM_FORMAL_ADMISSION"


def test_unknown_profile_is_reported_without_increasing_coverage():
    catalog = load_profile_catalog()
    unknown = ProfileAcceptance(
        campaign_id="unknown-campaign",
        profile_id="W99",
        profile_class=ProfileClass.REQUIRED_WRONG,
        catalog_version=catalog.version,
        catalog_hash=catalog.sha256,
        stage=CoverageStage.FORMAL,
        admission="PASS",
        source_reference="synthetic-test:unknown",
    )

    result = evaluate_profile_coverage(catalog, (unknown,), CoverageStage.FORMAL)

    assert result.coverage_status == "INCOMPLETE"
    assert result.profile_results["W99"].status == "UNKNOWN_PROFILE"
    assert result.required_wrong_completed == 0


def test_catalog_content_mutation_changes_canonical_hash():
    catalog = load_profile_catalog()
    mutated = catalog.to_dict()
    mutated["profiles"][0]["required"] = False

    assert catalog_sha256(mutated) != catalog.sha256


def test_removing_a_profile_changes_catalog_hash():
    catalog = load_profile_catalog()
    mutated = catalog.to_dict()
    mutated["profiles"] = mutated["profiles"][:-1]

    assert catalog_sha256(mutated) != catalog.sha256


@pytest.mark.parametrize("change", ("remove", "add"))
def test_catalog_v1_rejects_same_version_profile_universe_change(change):
    catalog = load_profile_catalog()
    mutated = catalog.to_dict()
    if change == "remove":
        mutated["profiles"].pop()
    else:
        additional = dict(mutated["profiles"][0])
        additional["profile_id"] = "UNREGISTERED_PROFILE"
        mutated["profiles"].append(additional)

    with pytest.raises(ProfileCatalogError, match="frozen profile universe"):
        profile_catalog_from_dict(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("profile_id", "KNOWN_FIX_V2"),
        ("profile_class", "REQUIRED_WRONG"),
        ("required", False),
        ("formal_sample_eligible", False),
        ("semantic_registration", "changed semantic registration"),
        ("expected_admission_kind", "EXCLUDED_FROM_FORMAL_ADMISSION"),
    ),
)
def test_each_profile_universe_field_changes_catalog_hash(field, value):
    catalog = load_profile_catalog()
    mutated = catalog.to_dict()
    mutated["profiles"][0][field] = value

    assert catalog_sha256(mutated) != catalog.sha256


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("catalog_version", "task3-profile-catalog-v2"),
        ("semantic_spec_version", "task3-semantics-v2"),
        ("benchmark_contract_version", "task3-benchmark-v7"),
        ("scoring_contract_version", "task3-scoring-v7"),
        ("calibration_campaign_version", "task3-calibration-v7"),
    ),
)
def test_catalog_and_associated_contract_versions_change_catalog_hash(field, value):
    catalog = load_profile_catalog()
    mutated = catalog.to_dict()
    mutated[field] = value

    assert catalog_sha256(mutated) != catalog.sha256


def test_asset_and_acceptance_status_do_not_change_catalog_identity():
    catalog = load_profile_catalog()
    updated_status = catalog.to_dict()
    updated_status["profiles"][0]["registration_status"] = "FORMAL_ACCEPTED"
    updated_status["profiles"][0]["asset_registration"]["status"] = "ACCEPTED"
    updated_status["profiles"][0]["acceptance_artifact_reference"] = "acceptance.json"

    assert catalog_sha256(updated_status) == catalog.sha256


def test_bound_prefreeze_acceptance_supersedes_unbound_legacy_record():
    catalog = load_profile_catalog()
    legacy = ProfileAcceptance(
        campaign_id="w2-legacy-prefreeze",
        profile_id="W2",
        profile_class=ProfileClass.REQUIRED_WRONG,
        catalog_version=None,
        catalog_hash=None,
        stage=CoverageStage.PREFREEZE,
        admission="PREFREEZE_ACCEPTED",
        source_reference="legacy:w2-prefreeze",
    )
    current = ProfileAcceptance(
        campaign_id="w2-current-prefreeze",
        profile_id="W2",
        profile_class=ProfileClass.REQUIRED_WRONG,
        catalog_version=catalog.version,
        catalog_hash=catalog.sha256,
        stage=CoverageStage.PREFREEZE,
        admission="PREFREEZE_ACCEPTED",
        source_reference="catalog-bound:w2-prefreeze",
    )

    result = evaluate_profile_coverage(
        catalog,
        (legacy, current),
        CoverageStage.PREFREEZE,
    )

    assert result.profile_results["W2"].status == "PREFREEZE_ACCEPTED"
    assert result.profile_results["W2"].source_reference == "catalog-bound:w2-prefreeze"


@pytest.mark.parametrize(
    ("binding_field", "binding_value"),
    (
        ("profile_catalog_hash", "0" * 64),
        ("profile_catalog_version", "task3-profile-catalog-v99"),
    ),
)
def test_campaign_catalog_binding_mismatch_does_not_count(
    tmp_path,
    binding_field,
    binding_value,
):
    catalog = load_profile_catalog()
    catalog_binding = {
        "profile_catalog_version": catalog.version,
        "profile_catalog_hash": catalog.sha256,
    }
    catalog_binding[binding_field] = binding_value
    identity = CampaignIdentity(
        campaign_id=f"wrong-catalog-campaign-{binding_field}",
        semantic_spec_identity="task3-semantics-v1",
        benchmark_contract_identity="task3-benchmark-v6",
        scoring_contract_identity="task3-scoring-v6",
        calibration_campaign_identity="task3-calibration-v6",
        deadline_formula_identity="task3-deadline-formula-v1",
        accepted_asset_identities=("sha256:test-asset",),
        environment_profile_identity="environment-test",
        formal_profile_identity="profile-test",
        profile_type=CampaignProfileType.CORRECT,
        **catalog_binding,
        profile_id="KNOWN_FIX",
    )
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.register(identity)

    record = profile_acceptance_from_campaign(catalog, store, identity.campaign_id)
    result = evaluate_profile_coverage(catalog, (record,), CoverageStage.FORMAL)

    assert result.coverage_status == "INCOMPLETE"
    assert result.profile_results["KNOWN_FIX"].status == "CATALOG_BINDING_MISMATCH"
    assert result.required_correct_completed == 0


def test_campaign_profile_class_mismatch_does_not_count(tmp_path):
    catalog = load_profile_catalog()
    identity = CampaignIdentity(
        campaign_id="w2-wrong-class",
        semantic_spec_identity="task3-semantics-v1",
        benchmark_contract_identity="task3-benchmark-v6",
        scoring_contract_identity="task3-scoring-v6",
        calibration_campaign_identity="task3-calibration-v6",
        deadline_formula_identity="task3-deadline-formula-v1",
        accepted_asset_identities=("sha256:test-asset",),
        environment_profile_identity="environment-test",
        formal_profile_identity="profile-test",
        profile_type=CampaignProfileType.CORRECT,
        profile_catalog_version=catalog.version,
        profile_catalog_hash=catalog.sha256,
        profile_id="W2",
    )
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.register(identity)
    record = profile_acceptance_from_campaign(catalog, store, identity.campaign_id)

    result = evaluate_profile_coverage(catalog, (record,), CoverageStage.FORMAL)

    assert result.coverage_status == "INCOMPLETE"
    assert result.profile_results["W2"].status == "PROFILE_CLASS_MISMATCH"
    assert result.required_wrong_completed == 0


def test_campaign_identity_requires_catalog_binding_fields_together():
    catalog = load_profile_catalog()
    values = {
        "campaign_id": "partial-catalog-binding",
        "semantic_spec_identity": "task3-semantics-v1",
        "benchmark_contract_identity": "task3-benchmark-v6",
        "scoring_contract_identity": "task3-scoring-v6",
        "calibration_campaign_identity": "task3-calibration-v6",
        "deadline_formula_identity": "task3-deadline-formula-v1",
        "accepted_asset_identities": ("sha256:test-asset",),
        "environment_profile_identity": "environment-test",
        "formal_profile_identity": "profile-test",
        "profile_catalog_version": catalog.version,
        "profile_catalog_hash": catalog.sha256,
    }

    with pytest.raises(ValueError, match="profile catalog binding fields must be complete"):
        CampaignIdentity(**values)
