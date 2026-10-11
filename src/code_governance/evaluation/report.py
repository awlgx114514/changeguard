"""Privacy-safe JSON report models for evaluation runs."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Mapping, Optional, Sequence, Tuple

from code_governance.validator.core import ValidationResult
from code_governance.validator.evidence import ValidationEvidence

from .metrics import TraceSummary, TrialMetrics, aggregate_metrics, calculate_metrics, summarize_trace


def _require_runtime_mode(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("runtime_mode is required")
    return value


def _context_payload(context: Any) -> Mapping[str, Any]:
    payload = context.to_dict() if hasattr(context, "to_dict") else context
    if not isinstance(payload, Mapping):
        raise TypeError("context must provide a mapping-compatible to_dict()")
    return payload


def _context_bytes(context: Any) -> int:
    payload = _context_payload(context)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return len(encoded)


def _state_payload(terminal_state: Any) -> Mapping[str, Any]:
    if terminal_state is None:
        return {}
    payload = terminal_state.to_dict() if hasattr(terminal_state, "to_dict") else terminal_state
    return payload if isinstance(payload, Mapping) else {}


@dataclass(frozen=True)
class TrialResult:
    """Sanitized trial-level report row."""

    task_id: str
    runtime_mode: str
    success: bool
    metrics: TrialMetrics
    trace_summary: Optional[TraceSummary]
    report_status: str
    fixture_id: str = "unspecified"
    baseline_commit: Optional[str] = None
    failure_category: Optional[str] = None
    data_gaps: Tuple[str, ...] = ()
    finding_count: Optional[int] = None
    changed_file_count: Optional[int] = None
    additions: Optional[int] = None
    deletions: Optional[int] = None
    verification_command_count: Optional[int] = None
    verification_all_passed: Optional[bool] = None
    terminal_status: Optional[str] = None
    terminal_error_count: Optional[int] = None
    terminal_open_issue_count: Optional[int] = None
    termination_reason: Optional[str] = None
    validation_result: Optional[str] = None
    final_outcome: Optional[str] = None
    assertion_results: Tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        _require_runtime_mode(self.runtime_mode)
        if not isinstance(self.data_gaps, tuple):
            object.__setattr__(self, "data_gaps", tuple(self.data_gaps))
        if not isinstance(self.assertion_results, tuple):
            object.__setattr__(self, "assertion_results", tuple(self.assertion_results))

    @classmethod
    def from_inputs(
        cls,
        task_id: str,
        runtime_mode: str,
        trace_events: Sequence[Mapping[str, Any]],
        context: Any,
        validation_evidence: ValidationEvidence,
        validator_result: ValidationResult,
        repairs: int,
        terminal_state: Any = None,
        fixture_id: str = "unspecified",
        baseline_commit: Optional[str] = None,
    ) -> "TrialResult":
        context_payload = _context_payload(context)
        memory_hits = len(context_payload.get("memory_context", ()))
        strategy_hits = len(context_payload.get("strategies", ()))
        success = validator_result.status == "PASS"
        metrics = calculate_metrics(
            success=success,
            trace_events=trace_events,
            context_bytes=_context_bytes(context),
            memory_hits=memory_hits,
            strategy_hits=strategy_hits,
            repairs=repairs,
        )
        trace_summary = summarize_trace(trace_events)
        verification = validation_evidence.verification
        change_context = validation_evidence.change_context
        state_payload = _state_payload(terminal_state)
        errors = state_payload.get("errors", ())
        issues = state_payload.get("open_issues", ())
        state_available = terminal_state is not None
        return cls(
            task_id=task_id,
            runtime_mode=runtime_mode,
            success=success,
            metrics=metrics,
            trace_summary=trace_summary,
            # The field name remains stable for existing serialized reports;
            # its value is derived from the authoritative Validator result.
            report_status=validator_result.status,
            fixture_id=fixture_id,
            baseline_commit=baseline_commit,
            failure_category=None if success else _validation_failure_category(validator_result),
            data_gaps=() if state_available else ("terminal_state_missing",),
            finding_count=len(validator_result.findings),
            changed_file_count=len(change_context.changed_files),
            additions=change_context.additions,
            deletions=change_context.deletions,
            verification_command_count=len(verification),
            verification_all_passed=all(item.passed for item in verification),
            terminal_status=(
                str(state_payload.get("status", "")) if state_available else None
            ),
            terminal_error_count=(
                len(errors)
                if state_available
                and isinstance(errors, Sequence)
                and not isinstance(errors, (str, bytes))
                else (0 if state_available else None)
            ),
            terminal_open_issue_count=(
                len(issues)
                if state_available
                and isinstance(issues, Sequence)
                and not isinstance(issues, (str, bytes))
                else (0 if state_available else None)
            ),
            termination_reason=(
                str(state_payload.get("termination_reason", ""))
                if state_available else None
            ),
            validation_result=(
                str(state_payload.get("validation_result", "NOT_RUN"))
                if state_available else None
            ),
            final_outcome=(
                str(state_payload.get("final_outcome", ""))
                if state_available else None
            ),
            assertion_results=_assertion_result_rows(validator_result),
        )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "runtime_mode": self.runtime_mode,
            "success": self.success,
            "report_status": self.report_status,
            "fixture_id": self.fixture_id,
            "baseline_commit": self.baseline_commit,
            "failure_category": self.failure_category,
            "data_gaps": list(self.data_gaps),
            "metrics": self.metrics.to_dict(),
            "trace_summary": (
                None if self.trace_summary is None else self.trace_summary.to_dict()
            ),
            "finding_count": self.finding_count,
            "changed_file_count": self.changed_file_count,
            "additions": self.additions,
            "deletions": self.deletions,
            "verification_command_count": self.verification_command_count,
            "verification_all_passed": self.verification_all_passed,
            "terminal_status": self.terminal_status,
            "terminal_error_count": self.terminal_error_count,
            "terminal_open_issue_count": self.terminal_open_issue_count,
            "termination_reason": self.termination_reason,
            "validation_result": self.validation_result,
            "final_outcome": self.final_outcome,
            "assertion_results": [dict(item) for item in self.assertion_results],
        }


@dataclass(frozen=True)
class EvaluationReport:
    """Top-level sanitized evaluation report."""

    runtime_mode: str
    trials: Tuple[TrialResult, ...]
    aggregate_metrics: TrialMetrics
    benchmark_version: str = "v1"
    dataset_id: str = "unspecified"
    run_started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    run_count: int = 1
    agent_metadata: Optional[Mapping[str, Optional[str]]] = None
    limitations: Tuple[str, ...] = (
        "Model, network, local performance, and external tool variability are not controlled.",
        "Five fixed tasks support exploratory comparison only and do not prove general improvement.",
    )

    def __post_init__(self) -> None:
        _require_runtime_mode(self.runtime_mode)
        if not isinstance(self.trials, tuple):
            object.__setattr__(self, "trials", tuple(self.trials))
        if not isinstance(self.dataset_id, str) or not self.dataset_id.strip():
            raise ValueError("dataset_id is required")
        if not isinstance(self.benchmark_version, str) or not self.benchmark_version.strip():
            raise ValueError("benchmark_version is required")
        if self.run_count < 1:
            raise ValueError("run_count must be at least one")
        if not isinstance(self.limitations, tuple):
            object.__setattr__(self, "limitations", tuple(self.limitations))
        mismatched = [
            trial.task_id
            for trial in self.trials
            if trial.runtime_mode != self.runtime_mode
        ]
        if mismatched:
            raise ValueError(
                "trial runtime_mode must match report runtime_mode: "
                + ", ".join(mismatched)
            )

    @classmethod
    def from_trials(
        cls,
        runtime_mode: str,
        trials: Sequence[TrialResult],
        *,
        dataset_id: str,
        run_started_at: Optional[str] = None,
        run_count: int = 1,
        agent_metadata: Optional[Mapping[str, Optional[str]]] = None,
    ) -> "EvaluationReport":
        trials_tuple = tuple(trials)
        return cls(
            runtime_mode=runtime_mode,
            trials=trials_tuple,
            aggregate_metrics=aggregate_metrics(trials_tuple),
            dataset_id=dataset_id,
            run_started_at=(
                run_started_at
                if run_started_at is not None
                else datetime.now(timezone.utc).isoformat()
            ),
            run_count=run_count,
            agent_metadata=agent_metadata,
        )

    def to_dict(self) -> dict:
        failure_summary = Counter(
            trial.failure_category
            for trial in self.trials
            if trial.failure_category is not None
        )
        data_gap_summary = Counter(
            gap for trial in self.trials for gap in trial.data_gaps
        )
        agent = dict(self.agent_metadata) if self.agent_metadata is not None else None
        if agent is None or not agent.get("model"):
            data_gap_summary["agent_model_unavailable"] += 1
        return {
            "benchmark_version": self.benchmark_version,
            "runtime_mode": self.runtime_mode,
            "trial_count": len(self.trials),
            "dataset": {"id": self.dataset_id, "task_count": len(self.trials)},
            "run": {
                "started_at": self.run_started_at,
                "run_count": self.run_count,
                "agent": agent,
            },
            "aggregate_metrics": self.aggregate_metrics.to_dict(),
            "trials": [trial.to_dict() for trial in self.trials],
            "limitations": list(self.limitations),
            "failure_summary": dict(sorted(failure_summary.items())),
            "data_gap_summary": dict(sorted(data_gap_summary.items())),
        }


def render_json(report: EvaluationReport) -> str:
    """Render a stable JSON report with sorted keys and indentation."""
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def _assertion_result_rows(validation_result: ValidationResult) -> Tuple[Mapping[str, Any], ...]:
    return tuple(
        {
            "assertion_id": result.assertion_id,
            "change_id": result.change_id,
            "assertion_type": result.assertion_type.value,
            "status": result.status.value,
            "evidence_refs": list(result.evidence_refs),
        }
        for result in validation_result.assertion_results
    )


def _validation_failure_category(validation_result: ValidationResult) -> str:
    codes = {finding.code for finding in validation_result.findings}
    if "verification_execution_failed" in codes:
        return "VERIFICATION_EXECUTION"
    if "verification_failed" in codes:
        return "VERIFICATION"
    if codes.intersection({"ASSERTION_FAILED", "ASSERTION_UNSUPPORTED"}):
        return "ASSERTION"
    return "VALIDATION"
