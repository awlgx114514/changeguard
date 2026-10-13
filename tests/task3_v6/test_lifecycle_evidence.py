from __future__ import annotations

import subprocess
import sys
import time

import pytest

from benchmarks.task3_v6.campaign import (
    CampaignIdentity,
    CampaignProfileType,
    CampaignStore,
    CampaignStateError,
    Verdict,
)
from benchmarks.task3_v6.measurement import (
    CompletionIdentity,
    CompletionKind,
    LifecycleFact,
    MeasurementProfile,
    ObservationAuthority,
    SharedCompletionRecord,
    TimedInteraction,
)


def setup_store(tmp_path, child=None):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    identity = CampaignIdentity(
        campaign_id="campaign-lifecycle",
        semantic_spec_identity="semantics-sha",
        benchmark_contract_identity="benchmark-sha",
        scoring_contract_identity="scoring-sha",
        calibration_campaign_identity="calibration-sha",
        deadline_formula_identity="deadline-sha",
        accepted_asset_identities=("asset-sha",),
        environment_profile_identity="environment-sha",
        formal_profile_identity="formal-profile-sha",
        profile_type=CampaignProfileType.WRONG,
    )
    store.register(identity)
    profile = MeasurementProfile(
        identity.formal_profile_identity,
        "QueryPerformanceCounter",
        1_000_000,
        1_000_000_000,
        identity.deadline_formula_identity,
    )
    store.register_measurement_profile(identity.campaign_id, profile)
    store.register_asset_lifecycle_defect(
        identity.campaign_id,
        asset_identity="asset-sha",
        defect_id="cleanup-resource-release",
        interaction_type="public_close",
        required_fact=LifecycleFact.DIRECT_CHILD_LIVE_CONFIRMED,
    )
    claim = store.claim_slot(
        campaign_id=identity.campaign_id,
        slot_id=0,
        run_id="run-lifecycle",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )
    store.start_execution(
        claim,
        fixture_identity="fixture-sha",
        launch_plan_identity="plan-sha",
        launch_identity="launch-run-lifecycle",
        execution_owner_identity="supervisor-a",
    )
    completion_identity = CompletionIdentity.create(
        campaign_id=claim.campaign_id,
        slot_id=claim.slot_id,
        run_id=claim.run_id,
        case_id="process-close",
        interaction_id="interaction-close",
        interaction_type="public_close",
        interaction_generation=1,
        measurement_observation_owner_identity="supervisor-a",
        terminal_decision_identity="decision-run-lifecycle",
    )
    authority = ObservationAuthority(tmp_path / "observation.sqlite3")
    completion = SharedCompletionRecord.create(completion_identity, profile, authority)
    if child is not None:
        store.bind_direct_child(claim, completion_identity, child)
    completion.publisher_begin()
    completion.publish(
        TimedInteraction(
            "completed",
            "result-run-lifecycle",
            CompletionKind.RETURNED,
            10,
            20,
            profile.counter_frequency,
            profile.clock_identity,
        )
    )
    completion.terminal_snapshot()
    return store, claim, completion_identity, authority, completion


def test_supervisor_wait_timeout_is_persisted_and_attributed_from_real_child(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    store, claim, identity, authority, completion = setup_store(tmp_path, child)
    try:
        evidence = store.record_observation(claim, identity, authority, process=child)

        assert len(evidence.lifecycle_evidence_ids) == 1
        lifecycle = store.get_lifecycle_evidence(evidence.lifecycle_evidence_ids[0])
        assert lifecycle.lifecycle_fact is LifecycleFact.DIRECT_CHILD_LIVE_CONFIRMED
        assert lifecycle.os_query_raw_result == 258
        assert lifecycle.child_pid == child.pid

        restored = CampaignStore(store.database_path)
        assert restored.get_lifecycle_evidence(lifecycle.evidence_id) == lifecycle
        decision = restored.terminalize(claim, "decision-lifecycle")

        assert decision.verdict is Verdict.FAIL
        assert decision.fail_attribution == "confirmed_rejection"
        assert decision.component_results[-1]["component_id"].endswith("direct_child_lifecycle")
        assert decision.component_results[-1]["verdict"] == Verdict.FAIL.value
        assert decision.attribution_result["valid"] is True
    finally:
        child.kill()
        child.wait(timeout=5)
        completion.close()


def test_terminal_child_raw_wait_result_is_only_reduced_claim_pass(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(0.2)
    store, claim, identity, authority, completion = setup_store(tmp_path, child)
    try:
        evidence = store.record_observation(claim, identity, authority, process=child)
        lifecycle = store.get_lifecycle_evidence(evidence.lifecycle_evidence_ids[0])
        decision = store.terminalize(claim, "decision-lifecycle")

        assert lifecycle.os_query_raw_result == 0
        assert lifecycle.lifecycle_fact is LifecycleFact.NO_LEAK_OBSERVED_AT_EXTERNAL_CHECK
        assert decision.component_results[-1]["verdict"] == Verdict.PASS.value
        assert decision.verdict is Verdict.PASS
    finally:
        completion.close()


def test_asset_identity_without_lifecycle_evidence_cannot_create_child_failure(tmp_path):
    store, claim, identity, authority, completion = setup_store(tmp_path)
    try:
        evidence = store.record_observation(claim, identity, authority)
        decision = store.terminalize(claim, "decision-lifecycle")

        assert evidence.lifecycle_evidence_ids == ()
        assert all(
            item["verdict"] != Verdict.FAIL.value
            for item in decision.component_results
            if item["component_id"].endswith("direct_child_lifecycle")
        )
        assert decision.fail_attribution != "confirmed_rejection"
    finally:
        completion.close()


def test_worker_constructed_lifecycle_claim_is_not_accepted_as_evidence(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    store, claim, identity, authority, completion = setup_store(tmp_path, child)
    try:
        with pytest.raises(TypeError):
            store.record_observation(
                claim,
                identity,
                authority,
                process=child,
                worker_lifecycle_claim={
                    "child_pid": child.pid,
                    "lifecycle_fact": "DIRECT_CHILD_LIVE_CONFIRMED",
                    "os_query_raw_result": 258,
                },
            )
    finally:
        child.kill()
        child.wait(timeout=5)
        completion.close()


def test_wrong_direct_child_binding_is_invalid_and_cannot_be_attributed(tmp_path):
    registered_child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    actual_child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    store, claim, identity, authority, completion = setup_store(tmp_path, registered_child)
    try:
        evidence = store.record_observation(claim, identity, authority, process=actual_child)
        lifecycle = store.get_lifecycle_evidence(evidence.lifecycle_evidence_ids[0])
        decision = store.terminalize(claim, "decision-wrong-child")

        assert lifecycle.os_query_raw_result == 258
        assert lifecycle.child_binding_status == "INVALID"
        assert lifecycle.lifecycle_fact is LifecycleFact.OS_QUERY_INVALID
        assert decision.verdict is Verdict.INVALID
        assert decision.fail_attribution is None
    finally:
        registered_child.kill()
        registered_child.wait(timeout=5)
        actual_child.kill()
        actual_child.wait(timeout=5)
        completion.close()


def test_cleanup_before_first_external_check_records_invalid_order(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    store, claim, identity, authority, completion = setup_store(tmp_path, child)
    try:
        store.mark_direct_child_cleanup(claim, identity)
        evidence = store.record_observation(claim, identity, authority, process=child)
        lifecycle = store.get_lifecycle_evidence(evidence.lifecycle_evidence_ids[0])
        decision = store.terminalize(claim, "decision-invalid-order")

        assert lifecycle.os_query_raw_result is None
        assert lifecycle.os_query_operation == "NOT_PERFORMED"
        assert lifecycle.query_order_status == "INVALID_CLEANUP_PRECEDED_QUERY"
        assert decision.verdict is Verdict.INVALID
        assert decision.fail_attribution is None
    finally:
        child.kill()
        child.wait(timeout=5)
        completion.close()


def test_lifecycle_evidence_reference_cannot_cross_run_or_generation(tmp_path):
    from dataclasses import replace

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    store, claim, identity, authority, completion = setup_store(tmp_path, child)
    try:
        evidence = store.record_observation(claim, identity, authority, process=child)
        lifecycle = store.get_lifecycle_evidence(evidence.lifecycle_evidence_ids[0])

        second_claim = store.claim_slot(
            campaign_id=claim.campaign_id,
            slot_id=1,
            run_id="run-other",
            asset_identity=claim.asset_identity,
            environment_identity=claim.environment_identity,
            claim_metadata={},
        )
        store.start_execution(
            second_claim,
            fixture_identity="fixture-sha",
            launch_plan_identity="plan-sha",
            launch_identity="launch-run-other",
            execution_owner_identity="supervisor-a",
        )
        with pytest.raises(CampaignStateError):
            store._record_run_evidence(second_claim, evidence, lifecycle)

        wrong_generation = replace(
            evidence,
            interaction_generation=2,
            lifecycle_evidence_ids=(lifecycle.evidence_id,),
            evidence_id="wrong-generation-evidence",
        )
        with pytest.raises(CampaignStateError):
            store._record_run_evidence(claim, wrong_generation, lifecycle)
    finally:
        child.kill()
        child.wait(timeout=5)
        completion.close()


def test_wait_failed_is_invalid_not_a_target_behavior_failure(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    store, claim, identity, authority, completion = setup_store(tmp_path, child)
    try:
        key = (claim.campaign_id, claim.slot_id, claim.run_id, identity.interaction_id, 1)
        handle = store._direct_child_handles[key]
        handle._kernel32.CloseHandle(handle.handle)
        evidence = store.record_observation(claim, identity, authority, process=child)
        lifecycle = store.get_lifecycle_evidence(evidence.lifecycle_evidence_ids[0])
        decision = store.terminalize(claim, "decision-wait-failed")

        assert lifecycle.os_query_raw_result == 0xFFFFFFFF
        assert lifecycle.lifecycle_fact is LifecycleFact.OS_QUERY_INVALID
        assert decision.verdict is Verdict.INVALID
        assert decision.fail_attribution is None
    finally:
        child.kill()
        child.wait(timeout=5)
        completion.close()


def test_bound_child_without_query_is_not_proven(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    store, claim, identity, authority, completion = setup_store(tmp_path, child)
    try:
        evidence = store.record_observation(claim, identity, authority)
        decision = store.terminalize(claim, "decision-not-proven")

        assert evidence.lifecycle_required is True
        assert evidence.lifecycle_evidence_ids == ()
        assert decision.component_results[-1]["verdict"] == Verdict.NOT_PROVEN.value
        assert decision.fail_attribution is None
    finally:
        child.kill()
        child.wait(timeout=5)
        completion.close()
