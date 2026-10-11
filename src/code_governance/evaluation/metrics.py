"""Normalized evaluation metrics derived from trace and context summaries."""

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union


def _event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("type", ""))


def _event_data(event: Mapping[str, Any]) -> Mapping[str, Any]:
    data = event.get("data", {})
    return data if isinstance(data, Mapping) else {}


@dataclass(frozen=True)
class TraceSummary:
    """Compact trace counters safe to persist in benchmark reports."""

    event_count: int
    action_count: int
    tool_call_count: int
    repair_count: int
    repeated_action_count: int
    repeated_action_is_heuristic: bool = True

    def to_dict(self) -> dict:
        return {
            "event_count": self.event_count,
            "action_count": self.action_count,
            "tool_call_count": self.tool_call_count,
            "repair_count": self.repair_count,
            "repeated_action_count": self.repeated_action_count,
            "repeated_action_is_heuristic": self.repeated_action_is_heuristic,
        }


@dataclass(frozen=True)
class TrialMetrics:
    """Normalized per-trial metrics with exact benchmark field names."""

    success_rate: float
    action_count: Optional[int]
    tool_call_count: Optional[int]
    repair_count: Optional[int]
    repair_success_rate: Optional[float]
    context_size: Optional[int]
    memory_hit_rate: Optional[float]
    memory_hit_count: Optional[int] = None
    strategy_hit_count: Optional[int] = None
    _trial_count: int = 1
    _repair_denominator: int = 0
    _memory_hits: int = 0
    _memory_denominator: int = 0

    def to_dict(self) -> dict:
        return {
            "success_rate": self.success_rate,
            "action_count": self.action_count,
            "tool_call_count": self.tool_call_count,
            "repair_count": self.repair_count,
            "repair_success_rate": self.repair_success_rate,
            "context_size": self.context_size,
            "memory_hit_rate": self.memory_hit_rate,
            "memory_hit_count": self.memory_hit_count,
            "strategy_hit_count": self.strategy_hit_count,
        }


def summarize_trace(events: Sequence[Mapping[str, Any]]) -> TraceSummary:
    """Count trace events without retaining raw event payloads."""
    action_count = 0
    tool_call_count = 0
    repair_count = 0
    repeated_action_count = 0
    seen_actions: dict[tuple[str, str], int] = {}

    for event in events:
        event_type = _event_type(event)
        if event_type == "action_decided":
            action_count += 1
            data = _event_data(event)
            action_type = data.get("operation")
            target = data.get("target")
            if isinstance(action_type, str) and action_type and isinstance(target, str) and target:
                key = (action_type, target)
                seen_actions[key] = seen_actions.get(key, 0) + 1
                if seen_actions[key] > 1:
                    repeated_action_count += 1
        elif event_type == "tool_called":
            tool_call_count += 1
        elif event_type == "repair_started":
            repair_count += 1

    return TraceSummary(
        event_count=len(events),
        action_count=action_count,
        tool_call_count=tool_call_count,
        repair_count=repair_count,
        repeated_action_count=repeated_action_count,
    )


def calculate_metrics(
    success: bool,
    trace_events: Optional[Sequence[Mapping[str, Any]]],
    context_bytes: Optional[int],
    memory_hits: Optional[int],
    strategy_hits: Optional[int],
    repairs: Optional[int],
) -> TrialMetrics:
    """Normalize one trial into exact metric fields."""
    summary = summarize_trace(trace_events) if trace_events is not None else None
    repair_count = repairs if repairs is not None else None
    repair_denominator = 1 if repair_count is not None and repair_count > 0 else 0
    context_available = memory_hits is not None and strategy_hits is not None
    normalized_memory_hits = None if memory_hits is None else max(memory_hits, 0)
    normalized_strategy_hits = None if strategy_hits is None else max(strategy_hits, 0)
    memory_hit = (
        1
        if context_available
        and (normalized_memory_hits > 0 or normalized_strategy_hits > 0)
        else 0
    )
    memory_denominator = 1 if context_available else 0
    return TrialMetrics(
        success_rate=1.0 if success else 0.0,
        action_count=None if summary is None else summary.action_count,
        tool_call_count=None if summary is None else summary.tool_call_count,
        repair_count=repair_count,
        repair_success_rate=(
            None if repair_denominator == 0 else (1.0 if success else 0.0)
        ),
        context_size=None if context_bytes is None else max(context_bytes, 0),
        memory_hit_rate=(
            None if memory_denominator == 0 else float(memory_hit)
        ),
        memory_hit_count=normalized_memory_hits,
        strategy_hit_count=normalized_strategy_hits,
        _trial_count=1,
        _repair_denominator=repair_denominator,
        _memory_hits=memory_hit,
        _memory_denominator=memory_denominator,
    )


def aggregate_metrics(trials: Sequence[Union[TrialMetrics, Any]]) -> TrialMetrics:
    """Aggregate trial metrics with weighted denominators when available."""
    metrics = [trial.metrics if hasattr(trial, "metrics") else trial for trial in trials]
    if not metrics:
        return TrialMetrics(
            success_rate=0.0,
            action_count=0,
            tool_call_count=0,
            repair_count=0,
            repair_success_rate=None,
            context_size=0,
            memory_hit_rate=None,
            memory_hit_count=0,
            strategy_hit_count=0,
            _trial_count=0,
            _repair_denominator=0,
            _memory_hits=0,
            _memory_denominator=0,
        )

    trial_count = sum(max(metric._trial_count, 0) for metric in metrics)
    success_numerator = sum(metric.success_rate * max(metric._trial_count, 0) for metric in metrics)
    repair_denominator = sum(max(metric._repair_denominator, 0) for metric in metrics)
    repair_numerator = sum(
        0.0
        if metric.repair_success_rate is None
        else metric.repair_success_rate * max(metric._repair_denominator, 0)
        for metric in metrics
    )
    memory_hits = sum(max(metric._memory_hits, 0) for metric in metrics)
    memory_denominator = sum(max(metric._memory_denominator, 0) for metric in metrics)

    def sum_observed(field: str) -> Optional[int]:
        values = [getattr(metric, field) for metric in metrics]
        observed = [value for value in values if value is not None]
        return sum(observed) if observed else None

    return TrialMetrics(
        success_rate=0.0 if trial_count == 0 else success_numerator / trial_count,
        action_count=sum_observed("action_count"),
        tool_call_count=sum_observed("tool_call_count"),
        repair_count=sum_observed("repair_count"),
        repair_success_rate=(
            None if repair_denominator == 0 else repair_numerator / repair_denominator
        ),
        context_size=sum_observed("context_size"),
        memory_hit_rate=(
            None if memory_denominator == 0 else memory_hits / memory_denominator
        ),
        memory_hit_count=sum_observed("memory_hit_count"),
        strategy_hit_count=sum_observed("strategy_hit_count"),
        _trial_count=trial_count,
        _repair_denominator=repair_denominator,
        _memory_hits=memory_hits,
        _memory_denominator=memory_denominator,
    )
