import json

import pytest

from code_governance.context.models import AgentContext, ContextFile, MemoryContext, StrategyContext
from code_governance.evaluation.metrics import TraceSummary, TrialMetrics
from code_governance.evaluation.report import EvaluationReport, TrialResult, render_json
from code_governance.memory.retriever import TaskContext
from code_governance.repo.analyzer import RepoContext
from code_governance.git.context import ChangeContext, FileChange
from code_governance.runtime.execution import ExecutionResult
from code_governance.validator.core import Finding, ValidationResult
from code_governance.validator.evidence import (
    ValidationEvidence,
    VerificationEvidence,
    WorkspaceIdentity,
)


FORBIDDEN_PROMPT = "PROMPT_SECRET"
FORBIDDEN_CODE = "CODE_SECRET"
FORBIDDEN_OUTPUT = "OUTPUT_SECRET"


def build_context():
    return AgentContext(
        version="ctx-v1",
        task_context=TaskContext(
            task_text=f"do not leak {FORBIDDEN_PROMPT}",
            task_type="evaluation",
            target_files=("src/app.py",),
            modules=("app",),
            keywords=("secret",),
        ),
        repository_context=RepoContext(
            root="D:/repo",
            files=("src/app.py",),
            python_modules=("app",),
            git_status=("M src/app.py",),
            core_files=("src/app.py",),
            module_relationships={"src/app.py": ()},
        ),
        memory_context=(
            MemoryContext(
                id="memory-1",
                type="rule",
                rule=f"never print {FORBIDDEN_PROMPT}",
                scope="global",
                reason="test fixture",
                confidence=0.9,
            ),
        ),
        project_rules=("stay safe",),
        relevant_files=(ContextFile(path="src/app.py", module="app", role="entrypoint", summary="entrypoint"),),
        constraints=("do not leak",),
        strategies=(
            StrategyContext(
                id="strategy-1",
                strategy=f"avoid {FORBIDDEN_CODE}",
                source_failure="prior-failure",
                confidence=0.8,
                reason="test fixture",
            ),
        ),
        available_tools=("filesystem",),
        current_goal=f"hide {FORBIDDEN_OUTPUT}",
        working_memory=(FORBIDDEN_OUTPUT,),
        active_issues=(FORBIDDEN_CODE,),
        decisions=(FORBIDDEN_PROMPT,),
    )


def build_validation_evidence():
    result = ExecutionResult(
        action_id="verification:pytest",
        status="SUCCESS",
        metadata={"returncode": 0},
    )
    verification = VerificationEvidence(
        claim_id="pytest",
        request_id="verification:pytest",
        action_id="verification:pytest",
        execution_result_id="verification:pytest",
        argv=("pytest", "-q"),
        expected_result={"exit_code": 0},
        actual_result={"returncode": 0},
        passed=True,
        execution_result=result,
    )
    return ValidationEvidence(
        workspace=WorkspaceIdentity("D:/repo", "base", "base", "one", "one"),
        change_context=ChangeContext((FileChange("src/app.py", "M", 3, 1),)),
        execution_results=(result,),
        verification=(verification,),
    )


def test_trial_result_from_inputs_serializes_only_whitelisted_fields():
    events = (
        {
            "type": "action_decided",
            "data": {
                "action_type": "write",
                "target": "src/app.py",
                "prompt": FORBIDDEN_PROMPT,
                "code": FORBIDDEN_CODE,
            },
        },
        {
            "type": "tool_called",
            "data": {"tool_name": "shell", "stdout": FORBIDDEN_OUTPUT},
        },
    )

    result = TrialResult.from_inputs(
        task_id="task-1",
        runtime_mode="hardened",
        trace_events=events,
        context=build_context(),
        validation_evidence=build_validation_evidence(),
        validator_result=ValidationResult(()),
        repairs=0,
        terminal_state={
            "status": "FINISHED",
            "errors": [FORBIDDEN_OUTPUT],
            "context": FORBIDDEN_PROMPT,
            "termination_reason": "NO_PROGRESS",
            "validation_result": "PASS",
            "final_outcome": "SUCCEEDED",
        },
        fixture_id="sha256:fixture-one",
        baseline_commit="a" * 40,
    )

    payload = result.to_dict()

    assert payload["runtime_mode"] == "hardened"
    assert payload["success"] is True
    assert payload["metrics"]["context_size"] > 0
    assert payload["verification_command_count"] == 1
    assert payload["verification_all_passed"] is True
    assert payload["terminal_status"] == "FINISHED"
    assert payload["terminal_error_count"] == 1
    assert payload["termination_reason"] == "NO_PROGRESS"
    assert payload["validation_result"] == "PASS"
    assert payload["final_outcome"] == "SUCCEEDED"
    assert payload["fixture_id"] == "sha256:fixture-one"
    assert payload["baseline_commit"] == "a" * 40
    assert payload["metrics"]["memory_hit_count"] == 1
    assert payload["metrics"]["strategy_hit_count"] == 1
    assert "changed_files" not in payload
    assert "findings" not in payload
    assert "verification" not in payload


def test_render_json_includes_runtime_modes_and_excludes_forbidden_content():
    result = TrialResult.from_inputs(
        task_id="task-1",
        runtime_mode="baseline",
        trace_events=(
            {"type": "repair_started", "data": {"attempt": 1, "input": FORBIDDEN_PROMPT}},
        ),
        context=build_context(),
        validation_evidence=build_validation_evidence(),
        validator_result=ValidationResult((
            Finding("validation", "failed", "validation failed", severity="error"),
        )),
        repairs=1,
        terminal_state={"status": "FAILED", "stderr": FORBIDDEN_OUTPUT},
    )
    report = EvaluationReport(
        runtime_mode="baseline",
        trials=(result,),
        aggregate_metrics=TrialMetrics(
            success_rate=0.0,
            action_count=0,
            tool_call_count=0,
            repair_count=1,
            repair_success_rate=0.0,
            context_size=result.metrics.context_size,
            memory_hit_rate=1.0,
            _trial_count=1,
            _repair_denominator=1,
            _memory_hits=1,
            _memory_denominator=1,
        ),
        benchmark_version="v1",
        dataset_id="changeguard-evaluation-v1",
        run_started_at="2026-08-25T00:00:00+00:00",
        run_count=1,
        agent_metadata={"agent_type": "FakeAgent", "model": "deterministic-v1"},
    )

    rendered = render_json(report)
    payload = json.loads(rendered)

    assert payload["runtime_mode"] == "baseline"
    assert payload["trials"][0]["runtime_mode"] == "baseline"
    assert payload["benchmark_version"] == "v1"
    assert payload["dataset"] == {
        "id": "changeguard-evaluation-v1",
        "task_count": 1,
    }
    assert payload["run"]["started_at"] == "2026-08-25T00:00:00+00:00"
    assert payload["run"]["run_count"] == 1
    assert payload["run"]["agent"]["model"] == "deterministic-v1"
    assert payload["limitations"]
    assert payload["failure_summary"] == {"VALIDATION": 1}
    assert payload["data_gap_summary"] == {}
    assert FORBIDDEN_PROMPT not in rendered
    assert FORBIDDEN_CODE not in rendered
    assert FORBIDDEN_OUTPUT not in rendered
    assert payload["trials"][0]["metrics"]["repair_success_rate"] == 0.0


def test_trial_result_derives_success_only_from_validator_outcome():

    result = TrialResult.from_inputs(
        task_id="task-1",
        runtime_mode="baseline",
        trace_events=(),
        context=build_context(),
        validation_evidence=build_validation_evidence(),
        validator_result=ValidationResult((
            Finding("validation", "failed", "validation failed", severity="error"),
        )),
        repairs=1,
        terminal_state={"status": "FAILED"},
    )

    payload = result.to_dict()

    assert result.success is False
    assert payload["success"] is False
    assert payload["metrics"]["success_rate"] == 0.0
    assert payload["metrics"]["repair_count"] == 1
    assert payload["metrics"]["repair_success_rate"] == 0.0
    assert payload["failure_category"] == "VALIDATION"


def test_trial_result_keeps_missing_terminal_state_nullable():
    result = TrialResult.from_inputs(
        task_id="task-1",
        runtime_mode="baseline",
        trace_events=(),
        context=build_context(),
        validation_evidence=build_validation_evidence(),
        validator_result=ValidationResult(()),
        repairs=0,
        terminal_state=None,
    )

    assert result.terminal_status is None
    assert result.terminal_error_count is None
    assert result.terminal_open_issue_count is None
    assert "terminal_state_missing" in result.data_gaps


def test_runtime_mode_is_required_at_trial_and_report_levels():
    metrics = TrialMetrics(
        success_rate=1.0,
        action_count=1,
        tool_call_count=0,
        repair_count=0,
        repair_success_rate=None,
        context_size=8,
        memory_hit_rate=None,
        _trial_count=1,
        _repair_denominator=0,
        _memory_hits=0,
        _memory_denominator=0,
    )
    summary = TraceSummary(
        event_count=1,
        action_count=1,
        tool_call_count=0,
        repair_count=0,
        repeated_action_count=0,
        repeated_action_is_heuristic=True,
    )

    with pytest.raises(ValueError, match="runtime_mode"):
        TrialResult(
            task_id="task-1",
            runtime_mode="",
            success=True,
            metrics=metrics,
            trace_summary=summary,
            report_status="PASS",
        )

    with pytest.raises(ValueError, match="runtime_mode"):
        EvaluationReport(runtime_mode="", trials=(), aggregate_metrics=metrics)


def test_report_rejects_trial_mode_mismatch_and_summarizes_categorical_gaps():
    result = TrialResult(
        task_id="task-1",
        runtime_mode="hardened",
        success=False,
        metrics=TrialMetrics(
            success_rate=0.0,
            action_count=None,
            tool_call_count=None,
            repair_count=None,
            repair_success_rate=None,
            context_size=None,
            memory_hit_rate=None,
        ),
        trace_summary=None,
        report_status="ERROR",
        fixture_id="sha256:fixture-one",
        baseline_commit="b" * 40,
        failure_category="harness_exception",
        data_gaps=("context_missing", "trace_missing"),
        terminal_status=None,
        terminal_error_count=None,
        terminal_open_issue_count=None,
    )

    with pytest.raises(ValueError, match="trial runtime_mode"):
        EvaluationReport.from_trials(
            "baseline",
            (result,),
            dataset_id="evaluation-v1",
            run_started_at="2026-08-25T00:00:00+00:00",
        )

    report = EvaluationReport.from_trials(
        "hardened",
        (result,),
        dataset_id="evaluation-v1",
        run_started_at="2026-08-25T00:00:00+00:00",
    )
    payload = report.to_dict()

    assert payload["failure_summary"] == {"harness_exception": 1}
    assert payload["data_gap_summary"] == {
        "agent_model_unavailable": 1,
        "context_missing": 1,
        "trace_missing": 1,
    }
    assert payload["trials"][0]["trace_summary"] is None
    assert payload["trials"][0]["metrics"]["action_count"] is None
