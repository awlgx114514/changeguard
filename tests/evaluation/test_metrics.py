import pytest

from code_governance.evaluation.metrics import (
    TraceSummary,
    TrialMetrics,
    aggregate_metrics,
    calculate_metrics,
    summarize_trace,
)


def test_summarize_trace_counts_events_and_repeated_actions_heuristically():
    events = (
        {"type": "action_decided", "data": {"operation": "file.read", "target": "src/app.py"}},
        {"type": "tool_called", "data": {"tool_name": "filesystem"}},
        {"type": "action_decided", "data": {"operation": "file.read", "target": "src/app.py"}},
        {"type": "repair_started", "data": {"attempt": 1}},
    )

    summary = summarize_trace(events)

    assert summary == TraceSummary(
        event_count=4,
        action_count=2,
        tool_call_count=1,
        repair_count=1,
        repeated_action_count=1,
        repeated_action_is_heuristic=True,
    )


def test_calculate_metrics_counts_successful_repairs_and_memory_hits():
    events = (
        {"type": "action_decided", "data": {"action_type": "read", "target": "src/app.py"}},
        {"type": "tool_called", "data": {"tool_name": "filesystem"}},
        {"type": "action_decided", "data": {"action_type": "write", "target": "src/app.py"}},
        {"type": "repair_started", "data": {"attempt": 1}},
    )

    metrics = calculate_metrics(
        success=True,
        trace_events=events,
        context_bytes=128,
        memory_hits=1,
        strategy_hits=0,
        repairs=1,
    )

    assert metrics.action_count == 2
    assert metrics.tool_call_count == 1
    assert metrics.repair_count == 1
    assert metrics.repair_success_rate == 1.0
    assert metrics.context_size == 128
    assert metrics.memory_hit_rate == 1.0
    assert metrics.success_rate == 1.0


def test_calculate_metrics_uses_boolean_memory_and_repaired_trial_denominators():
    failed_repair_events = (
        {"type": "repair_started", "data": {"attempt": 1}},
        {"type": "action_decided", "data": {"action_type": "write", "target": "src/app.py"}},
    )

    failed_metrics = calculate_metrics(
        success=False,
        trace_events=failed_repair_events,
        context_bytes=64,
        memory_hits=0,
        strategy_hits=2,
        repairs=1,
    )

    assert failed_metrics.repair_success_rate == 0.0
    assert failed_metrics.memory_hit_rate == 1.0
    assert failed_metrics.success_rate == 0.0

    both_kinds = calculate_metrics(False, (), 0, 1, 1, 0)
    assert both_kinds.memory_hit_rate == 1.0

    zero_denominator = calculate_metrics(False, (), 0, 0, 0, 0)

    assert zero_denominator.repair_success_rate is None
    assert zero_denominator.memory_hit_rate == 0.0


def test_calculate_metrics_uses_validated_harness_repair_evidence_for_repair_rate():
    metrics = calculate_metrics(
        success=True,
        trace_events=(),
        context_bytes=32,
        memory_hits=0,
        strategy_hits=0,
        repairs=1,
    )

    assert metrics.repair_count == 1
    assert metrics.repair_success_rate == 1.0


def test_aggregate_metrics_counts_boolean_hit_and_repaired_trials():
    aggregate = aggregate_metrics(
        (
            TrialMetrics(
                success_rate=1.0,
                action_count=2,
                tool_call_count=1,
                repair_count=2,
                repair_success_rate=1.0,
                context_size=128,
                memory_hit_rate=1.0,
                _trial_count=1,
                _repair_denominator=1,
                _memory_hits=1,
                _memory_denominator=1,
            ),
            TrialMetrics(
                success_rate=0.0,
                action_count=4,
                tool_call_count=3,
                repair_count=1,
                repair_success_rate=0.0,
                context_size=64,
                memory_hit_rate=1.0,
                _trial_count=1,
                _repair_denominator=1,
                _memory_hits=1,
                _memory_denominator=1,
            ),
        )
    )

    assert aggregate.success_rate == pytest.approx(0.5)
    assert aggregate.action_count == 6
    assert aggregate.tool_call_count == 4
    assert aggregate.repair_count == 3
    assert aggregate.repair_success_rate == pytest.approx(0.5)
    assert aggregate.context_size == 192
    assert aggregate.memory_hit_rate == 1.0


def test_missing_trace_and_context_evidence_stays_nullable_and_is_excluded():
    missing = calculate_metrics(
        success=False,
        trace_events=None,
        context_bytes=None,
        memory_hits=None,
        strategy_hits=None,
        repairs=None,
    )
    observed = calculate_metrics(
        success=True,
        trace_events=(),
        context_bytes=10,
        memory_hits=0,
        strategy_hits=1,
        repairs=0,
    )

    assert missing.action_count is None
    assert missing.tool_call_count is None
    assert missing.repair_count is None
    assert missing.context_size is None
    assert missing.memory_hit_rate is None

    aggregate = aggregate_metrics((missing, observed))
    assert aggregate.action_count == 0
    assert aggregate.context_size == 10
    assert aggregate.memory_hit_rate == 1.0
