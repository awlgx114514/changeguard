from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from experiments.task3_v6.campaign import (
    CampaignIdentity,
    CampaignProfileType,
    CampaignStore,
    CampaignStateError,
    SlotState,
    TerminalDecision,
    RunEvidence,
    Verdict,
)
from experiments.task3_v6.measurement import (
    CompletionIdentity,
    CompletionKind,
    MeasurementProfile,
    ObservationAuthority,
    SharedCompletionRecord,
    TimedInteraction,
)


def campaign_identity(
    campaign_id: str = "campaign-a",
    profile_type: CampaignProfileType = CampaignProfileType.CORRECT,
) -> CampaignIdentity:
    return CampaignIdentity(
        campaign_id=campaign_id,
        semantic_spec_identity="semantics-sha",
        benchmark_contract_identity="benchmark-sha",
        scoring_contract_identity="scoring-sha",
        calibration_campaign_identity="calibration-sha",
        deadline_formula_identity="deadline-sha",
        accepted_asset_identities=("asset-sha",),
        environment_profile_identity="environment-sha",
        formal_profile_identity="formal-profile-sha",
        profile_type=profile_type,
    )


def register_campaign(store, identity=None):
    identity = identity or campaign_identity()
    budget_ns = 1 if identity.profile_type is CampaignProfileType.WRONG else 1_000_000_000
    store.register(identity)
    store.register_measurement_profile(
        identity.campaign_id,
        MeasurementProfile(
            identity.formal_profile_identity,
            "QueryPerformanceCounter",
            1_000_000,
            budget_ns,
            identity.deadline_formula_identity,
        ),
    )


def decision(claim, *, verdict=Verdict.PASS, identity="decision-a"):
    return TerminalDecision(
        terminal_decision_identity=identity,
        campaign_id=claim.campaign_id,
        run_id=claim.run_id,
        slot_id=claim.slot_id,
        case_id="wait-case",
        interaction_id="interaction-a",
        interaction_generation=1,
        allocation_identity="allocation-a",
        snapshot_identity="snapshot-a",
        record_commit_identity="commit-a",
        verdict=verdict,
        decision_reason="observed committed completion record",
        evidence_ids=("evidence-a",),
        fail_attribution="unattributed_fail" if verdict is Verdict.FAIL else None,
    )


def add_evidence(
    store,
    claim,
    *,
    state="COMMITTED",
    elapsed_ticks=10,
    generation=1,
    completion_kind=CompletionKind.RETURNED,
):
    profile = store.get_measurement_profile(claim.campaign_id)
    store.start_execution(
        claim,
        fixture_identity="fixture-sha",
        launch_plan_identity="plan-sha",
        launch_identity=f"launch-{claim.run_id}",
        execution_owner_identity="owner-a",
    )
    measurement_identity = CompletionIdentity.create(
        campaign_id=claim.campaign_id,
        slot_id=claim.slot_id,
        run_id=claim.run_id,
        case_id="wait-case",
        interaction_id="interaction-a",
        interaction_type="public_wait",
        interaction_generation=generation,
        measurement_observation_owner_identity="supervisor-a",
        terminal_decision_identity=f"decision-{claim.run_id}",
    )
    authority = ObservationAuthority(
        store.database_path.parent / f"observations-{claim.run_id}-{generation}.sqlite3"
    )
    record = SharedCompletionRecord.create(measurement_identity, profile, authority)
    try:
        if state == "INFRASTRUCTURE_FAILURE":
            record.terminal_snapshot()
        else:
            record.publisher_begin()
            record.publish(
                TimedInteraction(
                    "completed", f"result-{claim.run_id}", completion_kind,
                    10, 10 + elapsed_ticks, profile.counter_frequency, profile.clock_identity,
                    "TaskFailure" if completion_kind is CompletionKind.REGISTERED_EXCEPTION else None,
                )
            )
            record.terminal_snapshot()
        return store.record_observation(claim, measurement_identity, authority)
    finally:
        record.close()


def test_campaign_preregisters_exactly_32_slots_and_rejects_identity_churn(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)

    assert store.slot_count("campaign-a") == 32
    assert store.counts("campaign-a").unclaimed == 32
    assert store.get_slot("campaign-a", 0).claim_timestamp_utc is None
    with pytest.raises(CampaignStateError):
        store.register(campaign_identity("replacement-id"))


def test_slot_claim_and_execution_start_are_each_insert_once(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={"operator": "formal-controller"},
    )

    assert claim.claim_timestamp_utc.endswith("+00:00")
    assert store.get_slot("campaign-a", 0).state is SlotState.CLAIMED
    assert store.get_slot("campaign-a", 0).claim_timestamp_utc == claim.claim_timestamp_utc
    assert store.get_slot("campaign-a", 1).state is SlotState.UNCLAIMED
    with pytest.raises(CampaignStateError):
        store.claim_slot(
            campaign_id="campaign-a",
            slot_id=0,
            run_id="run-b",
            asset_identity="asset-sha",
            environment_identity="environment-sha",
            claim_metadata={},
        )
    start = store.start_execution(
        claim,
        fixture_identity="fixture-sha",
        launch_plan_identity="plan-sha",
        launch_identity="launch-a",
        execution_owner_identity="owner-a",
    )
    assert start.launch_permitted is True
    restored = CampaignStore(tmp_path / "campaign.sqlite3")
    resumed_claim = restored.resume_claim("campaign-a", 0, "run-a")
    assert resumed_claim == claim
    assert restored.get_start_record("campaign-a", 0).launch_permitted is False
    with pytest.raises(CampaignStateError, match="original pending slot claim"):
        restored.resume_claim("campaign-a", 0, "run-replacement")
    store.start_execution(
        claim,
        fixture_identity="fixture-sha",
        launch_plan_identity="plan-sha",
        launch_identity="launch-a",
        execution_owner_identity="owner-a",
    )
    with pytest.raises(CampaignStateError):
        store.start_execution(
            claim,
            fixture_identity="fixture-sha",
            launch_plan_identity="plan-sha",
            launch_identity="launch-b",
            execution_owner_identity="owner-b",
        )


def test_measurement_profile_must_be_registered_before_slot_claim(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    identity = campaign_identity()
    store.register(identity)

    with pytest.raises(CampaignStateError, match="before slot claims"):
        store.claim_slot(
            campaign_id="campaign-a", slot_id=0, run_id="run-a",
            asset_identity="asset-sha", environment_identity="environment-sha",
            claim_metadata={},
        )

    register_campaign(store, identity)
    assert store.claim_slot(
        campaign_id="campaign-a", slot_id=0, run_id="run-a",
        asset_identity="asset-sha", environment_identity="environment-sha",
        claim_metadata={},
    ).run_id == "run-a"


def test_registered_measurement_profile_cannot_be_changed(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    identity = campaign_identity()
    register_campaign(store, identity)

    with pytest.raises(CampaignStateError, match="immutable"):
        store.register_measurement_profile(
            identity.campaign_id,
            MeasurementProfile(
                identity.formal_profile_identity,
                "QueryPerformanceCounter",
                1_000_000,
                2_000_000_000,
                identity.deadline_formula_identity,
            ),
        )


def test_terminal_decision_is_atomic_immutable_and_diagnostics_are_append_only(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )
    add_evidence(store, claim)
    original = store.terminalize(claim, "decision-a")

    assert store.terminalize(claim, "decision-a") == original
    with pytest.raises(CampaignStateError):
        store.terminalize(claim, "decision-b")
    store.append_diagnostic(
        terminal_decision_identity="decision-a",
        event_type="late_notification",
        event_payload={"generation": 1},
    )

    assert store.get_terminal_decision("decision-a") == original
    assert store.diagnostic_count("decision-a") == 1
    assert store.get_slot("campaign-a", 0).state is SlotState.TERMINAL


def test_campaign_rejects_caller_authored_pass_without_execution_evidence(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )

    with pytest.raises((CampaignStateError, TypeError)):
        store.terminalize(claim, decision(claim, verdict=Verdict.PASS))

    assert store.get_slot("campaign-a", 0).state is SlotState.CLAIMED


def test_campaign_rejects_caller_authored_confirmed_rejection_without_evidence(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )
    forged = replace(
        decision(claim, verdict=Verdict.FAIL),
        fail_attribution="confirmed_rejection",
        evidence_ids=(),
    )

    with pytest.raises((CampaignStateError, TypeError)):
        store.terminalize(claim, forged)

    assert store.get_slot("campaign-a", 0).state is SlotState.CLAIMED


def test_terminal_decision_identifier_cannot_be_a_verdict_label(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a", slot_id=0, run_id="run-a",
        asset_identity="asset-sha", environment_identity="environment-sha",
        claim_metadata={},
    )
    add_evidence(store, claim)

    with pytest.raises(CampaignStateError, match="terminal verdicts"):
        store.terminalize(claim, "PASS")


def test_scoring_derives_task_and_component_verdicts_from_bound_evidence(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )
    evidence = add_evidence(store, claim)

    result = store.terminalize(claim, "decision-a")

    assert result.verdict is Verdict.PASS
    assert result.evidence_ids == (evidence.evidence_id,)
    assert [item["verdict"] for item in result.component_results] == ["PASS", "PASS"]
    assert result.attribution_result == {"category": None, "evidence_ids": ()}


def test_scoring_consumes_observation_authority_snapshot(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    identity = campaign_identity()
    register_campaign(store, identity)
    profile = MeasurementProfile(
        "formal-profile-sha",
        "QueryPerformanceCounter",
        1_000_000,
        1_000_000_000,
        "deadline-sha",
    )
    store.register_measurement_profile(identity.campaign_id, profile)
    claim = store.claim_slot(
        campaign_id="campaign-a", slot_id=0, run_id="run-a",
        asset_identity="asset-sha", environment_identity="environment-sha",
        claim_metadata={},
    )
    start = store.start_execution(
        claim,
        fixture_identity="fixture-sha",
        launch_plan_identity="plan-sha",
        launch_identity="launch-run-a",
        execution_owner_identity="owner-a",
    )
    assert start.launch_permitted is True
    measurement_identity = CompletionIdentity.create(
        campaign_id="campaign-a", slot_id=0, run_id="run-a",
        case_id="wait-case", interaction_id="interaction-a",
        interaction_type="public_wait", interaction_generation=1,
        measurement_observation_owner_identity="supervisor-a",
        terminal_decision_identity="decision-a",
    )
    authority = ObservationAuthority(tmp_path / "observations.sqlite3")
    record = SharedCompletionRecord.create(measurement_identity, profile, authority)
    try:
        assert record.publisher_begin() is True
        record.publish(
            TimedInteraction(
                "completed", "result-run-a", CompletionKind.RETURNED,
                10, 20, 1_000_000, "QueryPerformanceCounter"
            )
        )
        snapshot = record.terminal_snapshot()
        evidence = store.record_observation(claim, measurement_identity, authority)

        decision = store.terminalize(claim, "decision-a")

        assert decision.verdict is Verdict.PASS
        assert decision.evidence_ids == (evidence.evidence_id,)
    finally:
        record.close()


def test_registered_interaction_exception_cannot_score_as_pass(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a", slot_id=0, run_id="run-a",
        asset_identity="asset-sha", environment_identity="environment-sha",
        claim_metadata={},
    )
    add_evidence(store, claim, completion_kind=CompletionKind.REGISTERED_EXCEPTION)

    result = store.terminalize(claim, "decision-a")

    assert result.verdict is Verdict.FAIL
    assert result.fail_attribution == "unattributed_fail"
    assert result.component_results[0]["verdict"] == Verdict.FAIL.value


def test_scoring_rejects_evidence_reused_across_run_identity(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    first = store.claim_slot(
        campaign_id="campaign-a", slot_id=0, run_id="run-a",
        asset_identity="asset-sha", environment_identity="environment-sha",
        claim_metadata={},
    )
    evidence = add_evidence(store, first)
    second = store.claim_slot(
        campaign_id="campaign-a", slot_id=1, run_id="run-b",
        asset_identity="asset-sha", environment_identity="environment-sha",
        claim_metadata={},
    )
    store.start_execution(
        second,
        fixture_identity="fixture-sha",
        launch_plan_identity="plan-sha",
        launch_identity="launch-run-b",
        execution_owner_identity="owner-b",
    )

    with pytest.raises(CampaignStateError, match="claimed run"):
        store._record_run_evidence(second, evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("budget_ns", 2_000_000_000),
        ("clock_identity", "forged-clock"),
        ("counter_frequency", 2_000_000),
        ("measurement_profile_identity", "forged-profile"),
        ("deadline_formula_version", "forged-formula"),
    ],
)
def test_run_evidence_rejects_measurement_profile_mutation(tmp_path, field, value):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a", slot_id=0, run_id="run-a",
        asset_identity="asset-sha", environment_identity="environment-sha",
        claim_metadata={},
    )
    evidence = add_evidence(store, claim)

    with pytest.raises(CampaignStateError, match="registered measurement profile"):
        store._record_run_evidence(
            claim,
            replace(evidence, **{field: value, "evidence_id": f"mutated-{field}"}),
        )


def test_scoring_rejects_stale_evidence_even_when_its_payload_hash_is_recomputed(tmp_path):
    database = tmp_path / "campaign.sqlite3"
    store = CampaignStore(database)
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a", slot_id=0, run_id="run-a",
        asset_identity="asset-sha", environment_identity="environment-sha",
        claim_metadata={},
    )
    evidence = add_evidence(store, claim)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT payload_json FROM run_evidence WHERE evidence_id=?",
            (evidence.evidence_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["run_id"] = "stale-run"
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        connection.execute(
            "UPDATE run_evidence SET payload_json=?, payload_hash=? WHERE evidence_id=?",
            (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), evidence.evidence_id),
        )

    with pytest.raises(CampaignStateError, match="current run"):
        store.terminalize(claim, "decision-a")


def test_correct_profile_admits_only_thirty_two_passes(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store, campaign_identity(profile_type=CampaignProfileType.CORRECT))

    for slot_id in range(32):
        claim = store.claim_slot(
            campaign_id="campaign-a", slot_id=slot_id, run_id=f"run-{slot_id}",
            asset_identity="asset-sha", environment_identity="environment-sha",
            claim_metadata={},
        )
        add_evidence(store, claim)
        store.terminalize(claim, f"decision-{slot_id}")

    counts = store.counts("campaign-a")
    assert counts.verdicts[Verdict.PASS.value] == 32
    assert counts.verdicts[Verdict.FAIL.value] == 0
    assert counts.verdicts[Verdict.INVALID.value] == 0
    assert counts.verdicts[Verdict.NOT_PROVEN.value] == 0
    assert counts.admission == "PASS"


def test_wrong_profile_admits_only_thirty_two_evidence_confirmed_rejections(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store, campaign_identity(profile_type=CampaignProfileType.WRONG))

    for slot_id in range(32):
        claim = store.claim_slot(
            campaign_id="campaign-a", slot_id=slot_id, run_id=f"run-{slot_id}",
            asset_identity="asset-sha", environment_identity="environment-sha",
            claim_metadata={},
        )
        add_evidence(store, claim, elapsed_ticks=100)
        store.terminalize(claim, f"decision-{slot_id}")

    counts = store.counts("campaign-a")
    assert counts.verdicts[Verdict.FAIL.value] == 32
    assert counts.confirmed_rejections == 32
    assert counts.admission == "PASS"


def test_wrong_profile_does_not_admit_thirty_two_ordinary_failures(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store, campaign_identity(profile_type=CampaignProfileType.WRONG))

    for slot_id in range(32):
        claim = store.claim_slot(
            campaign_id="campaign-a", slot_id=slot_id, run_id=f"run-{slot_id}",
            asset_identity="asset-sha", environment_identity="environment-sha",
            claim_metadata={},
        )
        add_evidence(store, claim, state="INFRASTRUCTURE_FAILURE")
        store.terminalize(claim, f"decision-{slot_id}")

    counts = store.counts("campaign-a")
    assert counts.confirmed_rejections == 0
    assert counts.admission == "REJECT"


def test_early_stop_blocks_new_claims_and_preserves_claimed_run(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )
    store.stop_new_claims("campaign-a", "frozen W2 stop condition")

    with pytest.raises(CampaignStateError):
        store.claim_slot(
            campaign_id="campaign-a",
            slot_id=1,
            run_id="run-b",
            asset_identity="asset-sha",
            environment_identity="environment-sha",
            claim_metadata={},
        )
    add_evidence(store, claim, state="INFRASTRUCTURE_FAILURE")
    assert store.terminalize(claim, "decision-a").verdict is Verdict.NOT_PROVEN
    assert store.counts("campaign-a").unclaimed == 31


def test_atomic_claim_allows_only_one_concurrent_run_for_a_slot(tmp_path):
    database = tmp_path / "campaign.sqlite3"
    store = CampaignStore(database)
    second_store = CampaignStore(database)
    register_campaign(store)

    def claim(candidate_store, run_id):
        try:
            return candidate_store.claim_slot(
                campaign_id="campaign-a",
                slot_id=0,
                run_id=run_id,
                asset_identity="asset-sha",
                environment_identity="environment-sha",
                claim_metadata={},
            )
        except CampaignStateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda args: claim(*args),
                ((store, "run-a"), (second_store, "run-b")),
            )
        )

    assert sum(result is not None for result in results) == 1
    assert store.counts("campaign-a").attempted == 1


def test_terminal_persistence_recovers_the_same_prepared_decision(tmp_path):
    database = tmp_path / "campaign.sqlite3"
    store = CampaignStore(database)
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )
    add_evidence(store, claim)
    terminal = store.prepare_terminal_decision(claim, "decision-a")
    assert store.terminal_persistence_phase("decision-a") == "PREPARED"
    restored = CampaignStore(database)
    restored.prepare_terminal_decision(claim, "decision-a")
    restored.record_terminal_receipt("decision-a")
    assert restored.terminal_persistence_phase("decision-a") == "RECEIPT"
    restored.commit_terminal_decision("decision-a")
    assert restored.terminal_persistence_phase("decision-a") == "COMMITTED"
    assert restored.get_terminal_decision("decision-a") == terminal
    assert restored.get_slot("campaign-a", 0).state is SlotState.TERMINAL


def test_campaign_cannot_pool_assets_into_the_same_formal_slot_set():
    with pytest.raises(ValueError, match="exactly one asset identity"):
        CampaignIdentity(
            campaign_id="campaign-mixed",
            semantic_spec_identity="semantic-sha",
            benchmark_contract_identity="benchmark-sha",
            scoring_contract_identity="scoring-sha",
            calibration_campaign_identity="calibration-sha",
            deadline_formula_identity="deadline-sha",
            accepted_asset_identities=("asset-a", "asset-b"),
            environment_profile_identity="environment-sha",
            formal_profile_identity="formal-sha",
        )


def test_run_id_cannot_be_claimed_by_two_slots(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.claim_slot(
            campaign_id="campaign-a",
            slot_id=1,
            run_id="run-a",
            asset_identity="asset-sha",
            environment_identity="environment-sha",
            claim_metadata={},
        )
    assert store.get_slot("campaign-a", 1).state is SlotState.UNCLAIMED


def test_terminal_preparation_without_receipt_remains_pending(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )
    add_evidence(store, claim)
    store.prepare_terminal_decision(claim, "decision-a")
    with pytest.raises(CampaignStateError, match="durable receipt"):
        store.commit_terminal_decision("decision-a")
    assert store.get_slot("campaign-a", 0).state is SlotState.CLAIMED
    assert store.counts("campaign-a").pending == 1


def test_execution_start_rejects_claim_identity_mutation(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={"owner": "original"},
    )
    claim.claim_metadata["owner"] = "changed"
    with pytest.raises(CampaignStateError, match="committed run binding"):
        store.start_execution(
            claim,
            fixture_identity="fixture-sha",
            launch_plan_identity="plan-sha",
            launch_identity="launch-a",
            execution_owner_identity="owner-a",
        )
    assert store.get_start_record("campaign-a", 0) is None


def test_execution_start_rejects_a_different_asset_binding(tmp_path):
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    register_campaign(store)
    claim = store.claim_slot(
        campaign_id="campaign-a",
        slot_id=0,
        run_id="run-a",
        asset_identity="asset-sha",
        environment_identity="environment-sha",
        claim_metadata={},
    )
    changed_claim = replace(claim, asset_identity="other-asset")
    with pytest.raises(CampaignStateError, match="committed run binding"):
        store.start_execution(
            changed_claim,
            fixture_identity="fixture-sha",
            launch_plan_identity="plan-sha",
            launch_identity="launch-a",
            execution_owner_identity="owner-a",
        )
