from __future__ import annotations

import pytest

from benchmarks.task3_v6.measurement import (
    CompletionIdentity,
    CompletionKind,
    InteractionTimingAdapter,
    MeasurementProfile,
    MeasurementInfrastructureError,
    ObservationAuthority,
    SharedCompletionRecord,
    TimedInteraction,
)


def measurement_profile(*, budget_ns=10**9, frequency=1_000_000, clock="QueryPerformanceCounter"):
    return MeasurementProfile("profile-a", clock, frequency, budget_ns, "deadline-v1")


def create_record(identity_value=None, *, profile=None, authority=None):
    return SharedCompletionRecord.create(
        identity_value or identity(),
        profile or measurement_profile(),
        authority or ObservationAuthority(":memory:"),
    )


def identity() -> CompletionIdentity:
    return CompletionIdentity.create(
        campaign_id="campaign-a",
        run_id="run-a",
        case_id="wait-case",
        interaction_id="interaction-a",
        interaction_type="public_wait",
        interaction_generation=1,
        measurement_observation_owner_identity="supervisor-a",
        terminal_decision_identity="decision-a",
    )


def test_shared_record_uses_aligned_atomic_state_and_single_commit():
    record = create_record(
        profile=measurement_profile(budget_ns=10**12, frequency=10_000_000)
    )
    try:
        assert record.state_word_alignment == 8
        assert record.state_word_address % record.state_word_alignment == 0
        assert record.publisher_state_word_identity == record.supervisor_state_word_identity
        assert record.notification_is_initially_nonsignaled is True
        assert record.publisher_begin() is True
        assert record.publisher_begin() is False

        timing = InteractionTimingAdapter(record.profile).measure(lambda: 17)
        record.publish(timing)
        assert record.wait_for_notification(timeout_ms=0) is True
        assert record.wait_for_notification(timeout_ms=0) is False

        snapshot = record.terminal_snapshot()
        assert snapshot.state == "COMMITTED"
        assert snapshot.payload.result_identity == timing.result_identity
        assert snapshot.payload.interaction_generation == 1
        assert record.terminal_snapshot_count == 1
        with pytest.raises(MeasurementInfrastructureError):
            record.publish(timing)
    finally:
        record.close()


def test_supervisor_copies_committed_payload_after_publisher_view_exits():
    record = create_record(profile=measurement_profile(frequency=10_000_000))
    try:
        assert record.state_word_address != record.publisher_state_word_address
        assert record.publisher_begin() is True
        timing = InteractionTimingAdapter(record.profile).measure(lambda: {"value": 23})
        expected = record.publish(timing)
        record.publisher_exit()

        snapshot = record.terminal_snapshot()

        assert snapshot.state == "COMMITTED"
        assert snapshot.payload == expected
        assert snapshot.payload is not expected
        assert snapshot.payload.result_identity == timing.result_identity
    finally:
        record.close()


def test_supervisor_mapping_cannot_be_released_before_snapshot_ownership_transfer():
    record = create_record()
    assert record.publisher_begin() is True
    record.publish(
        TimedInteraction(
            "done", "owned-result", CompletionKind.RETURNED, 10, 11,
            1_000_000, "QueryPerformanceCounter"
        )
    )
    record.publisher_exit()

    with pytest.raises(MeasurementInfrastructureError, match="snapshot ownership"):
        record.close()

    assert record.terminal_snapshot().payload.result_identity == "owned-result"
    record.close()


def test_interaction_exception_is_timed_and_marked_as_registered_completion():
    class RegisteredFailure(Exception):
        pass

    profile = measurement_profile()
    outcome = InteractionTimingAdapter(
        profile,
        registered_exceptions=(RegisteredFailure,)
    ).measure(
        lambda: (_ for _ in ()).throw(RegisteredFailure("expected")),
    )

    assert outcome.completion_kind is CompletionKind.REGISTERED_EXCEPTION
    assert outcome.exception_type == "RegisteredFailure"
    assert outcome.end_ticks >= outcome.start_ticks
    assert outcome.counter_frequency > 0
    assert outcome.result_identity


def test_missing_committed_record_is_infrastructure_failure_not_timeout():
    record = create_record()
    try:
        snapshot = record.terminal_snapshot()
        assert snapshot.state == "INFRASTRUCTURE_FAILURE"
        assert snapshot.infrastructure_failure is True
        assert snapshot.semantic_timeout == "UNPROVEN"
        assert snapshot.observation_count == 1
    finally:
        record.close()


def test_staging_record_is_not_reobserved_or_scored_as_timeout():
    record = create_record()
    try:
        assert record.publisher_begin() is True
        snapshot = record.terminal_snapshot()
        assert snapshot.state == "STAGING"
        assert snapshot.infrastructure_failure is True
        assert snapshot.semantic_timeout == "UNPROVEN"
        assert snapshot.observation_count == 1
    finally:
        record.close()


def test_semantic_timeout_requires_valid_committed_timing_and_integer_deadline():
    record = create_record(profile=measurement_profile(budget_ns=49_999))
    try:
        assert record.publisher_begin() is True
        timing = TimedInteraction(
            result="done",
            result_identity="result-a",
            completion_kind=CompletionKind.RETURNED,
            start_ticks=100,
            end_ticks=150,
            counter_frequency=1_000_000,
            clock_identity="QueryPerformanceCounter",
        )
        record.publish(timing)
        snapshot = record.terminal_snapshot()
        assert snapshot.state == "COMMITTED"
        assert snapshot.infrastructure_failure is False
        assert snapshot.elapsed_ticks == 50
        assert snapshot.semantic_timeout == "CONFIRMED"
    finally:
        record.close()


def test_completion_exactly_at_integer_deadline_is_on_time():
    record = create_record(profile=measurement_profile(budget_ns=50_000))
    try:
        assert record.publisher_begin() is True
        timing = TimedInteraction(
            result="done",
            result_identity="result-b",
            completion_kind=CompletionKind.RETURNED,
            start_ticks=100,
            end_ticks=150,
            counter_frequency=1_000_000,
            clock_identity="QueryPerformanceCounter",
        )
        record.publish(timing)
        snapshot = record.terminal_snapshot()
        assert snapshot.state == "COMMITTED"
        assert snapshot.infrastructure_failure is False
        assert snapshot.semantic_timeout == "NOT_CONFIRMED"
    finally:
        record.close()


def test_staging_snapshot_never_accepts_partial_payload_or_changes_late():
    record = create_record(profile=measurement_profile(budget_ns=20_000))
    try:
        assert record.publisher_begin() is True
        timing = TimedInteraction(
            result="done",
            result_identity="late-result",
            completion_kind=CompletionKind.RETURNED,
            start_ticks=100,
            end_ticks=120,
            counter_frequency=1_000_000,
            clock_identity="QueryPerformanceCounter",
        )
        staged = record.stage(timing)
        snapshot = record.terminal_snapshot()
        assert staged.result_identity == "late-result"
        assert snapshot.state == "STAGING"
        assert snapshot.payload is None
        assert snapshot.semantic_timeout == "UNPROVEN"

        record.commit()
        assert record.terminal_snapshot_count == 1
        assert record.wait_for_notification(timeout_ms=0) is True
        with pytest.raises(MeasurementInfrastructureError):
            record.terminal_snapshot()
    finally:
        record.close()


@pytest.mark.parametrize(
    ("counter_frequency", "clock_identity"),
    [
        (2_000_000, "QueryPerformanceCounter"),
        (1_000_000, "forged-clock"),
    ],
)
def test_measurement_record_must_match_supervisor_registered_profile(
    counter_frequency, clock_identity
):
    record = create_record()
    try:
        assert record.publisher_begin() is True
        record.publish(
            TimedInteraction(
                result="done",
                result_identity="profile-bound-result",
                completion_kind=CompletionKind.RETURNED,
                start_ticks=10,
                end_ticks=20,
                    counter_frequency=counter_frequency,
                    clock_identity=clock_identity,
            )
        )

        snapshot = record.terminal_snapshot()

        assert snapshot.state == "INVALID_RECORD"
        assert snapshot.semantic_timeout == "UNPROVEN"
    finally:
        record.close()


def test_completion_payload_does_not_store_supervisor_budget():
    record = create_record(profile=measurement_profile(budget_ns=123_456))
    try:
        assert record.publisher_begin() is True
        payload = record.publish(
            TimedInteraction(
                "done", "budget-bound-result", CompletionKind.RETURNED,
                10, 20, 1_000_000, "QueryPerformanceCounter"
            )
        )

        assert "budget_ns" not in payload.__dataclass_fields__
        snapshot = record.terminal_snapshot()
        assert snapshot.budget_ns == 123_456
    finally:
        record.close()


def test_recreated_observer_reads_first_terminal_observation(tmp_path):
    authority_path = tmp_path / "observations.sqlite3"
    authority = ObservationAuthority(authority_path)
    first = create_record(authority=authority)
    second = None
    try:
        assert first.publisher_begin() is True
        first.stage(
            TimedInteraction(
                "done", "staged-result", CompletionKind.RETURNED, 10, 20,
                1_000_000, "QueryPerformanceCounter"
            )
        )
        initial = first.terminal_snapshot()
        assert initial.state == "STAGING"
        first.commit()

        second = create_record(first.identity, authority=ObservationAuthority(authority_path))
        restored = second.terminal_snapshot()

        assert restored == initial
        assert restored.observation_count == 1
        first.close()
        restarted = create_record(
            first.identity,
            authority=ObservationAuthority(authority_path),
        )
        try:
            assert restarted.terminal_snapshot() == initial
        finally:
            restarted.close()
    finally:
        if second is not None:
            second.close()
        first.close()


def test_new_allocation_cannot_claim_same_logical_observation(tmp_path):
    authority_path = tmp_path / "observations.sqlite3"
    authority = ObservationAuthority(authority_path)
    first_identity = identity()
    first = create_record(first_identity, authority=authority)
    second = None
    try:
        assert first.publisher_begin() is True
        first.terminal_snapshot()
        second_identity = CompletionIdentity.create(
            campaign_id=first_identity.campaign_id,
            slot_id=first_identity.slot_id,
            run_id=first_identity.run_id,
            case_id=first_identity.case_id,
            interaction_id=first_identity.interaction_id,
            interaction_type=first_identity.interaction_type,
            interaction_generation=first_identity.interaction_generation,
            measurement_observation_owner_identity=first_identity.measurement_observation_owner_identity,
            terminal_decision_identity=first_identity.terminal_decision_identity,
        )
        second = create_record(
            second_identity,
            authority=ObservationAuthority(authority_path),
        )

        with pytest.raises(MeasurementInfrastructureError, match="already bound"):
            second.terminal_snapshot()
    finally:
        if second is not None:
            second.close()
        first.close()


def test_stale_generation_event_cannot_wake_the_next_generation():
    first_identity = identity()
    second_identity = CompletionIdentity.create(
        campaign_id="campaign-a",
        run_id="run-a",
        case_id="wait-case",
        interaction_id="interaction-a",
        interaction_type="public_wait",
        interaction_generation=2,
        measurement_observation_owner_identity="supervisor-a",
        terminal_decision_identity="decision-a",
    )
    authority = ObservationAuthority(":memory:")
    first = create_record(first_identity, authority=authority)
    second = create_record(second_identity, authority=authority)
    try:
        assert first.publisher_begin() is True
        first.publish(
            TimedInteraction("old", "old-result", CompletionKind.RETURNED, 10, 20,
                             1_000_000, "QueryPerformanceCounter")
        )
        assert first.wait_for_notification(timeout_ms=0) is True
        assert second.wait_for_notification(timeout_ms=0) is False
        assert second.publisher_begin() is True
        second.publish(
            TimedInteraction("new", "new-result", CompletionKind.RETURNED, 30, 40,
                             1_000_000, "QueryPerformanceCounter")
        )
        assert second.wait_for_notification(timeout_ms=0) is True
        assert first.identity.notification_object_identity != second.identity.notification_object_identity
        assert first.identity.interaction_generation != second.identity.interaction_generation
    finally:
        first.terminal_snapshot()
        second.terminal_snapshot()
        first.close()
        second.close()
