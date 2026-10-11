from code_governance.evaluation.metrics import calculate_metrics
from code_governance.evaluation.report import TrialResult
from code_governance.git.context import ChangeContext
from code_governance.validator.assertion import (
    AssertionResult,
    AssertionStatus,
    AssertionType,
)
from code_governance.validator.core import Finding, ValidationResult
from code_governance.validator.evidence import ValidationEvidence, WorkspaceIdentity
from code_governance.validator.assertions import AssertionEvaluatorRegistry


def test_evaluation_serializes_existing_validator_assertion_results_without_evaluating(monkeypatch):
    evidence = ValidationEvidence(
        workspace=WorkspaceIdentity("D:/fixture", "base", "base", "a", "a"),
        change_context=ChangeContext(()),
        execution_results=(),
        verification=(),
    )
    assertion_result = AssertionResult(
        assertion_id="source_changed",
        change_id="feature_enabled",
        assertion_type=AssertionType.FILE_CHANGED,
        status=AssertionStatus.FAILED,
        evidence_refs=(),
        message="expected path absent",
    )
    validator_result = ValidationResult(
        (Finding("assertion", "ASSERTION_FAILED", "failed", severity="error"),),
        (assertion_result,),
    )

    def unexpected_evaluation(*args, **kwargs):
        raise AssertionError("Evaluation must not execute assertion evaluators")

    monkeypatch.setattr(AssertionEvaluatorRegistry, "evaluate", unexpected_evaluation)

    trial = TrialResult.from_inputs(
        task_id="assertion-task",
        runtime_mode="hardened",
        trace_events=(),
        context={"memory_context": (), "strategies": ()},
        validation_evidence=evidence,
        validator_result=validator_result,
        repairs=0,
    )

    assert trial.failure_category == "ASSERTION"
    assert trial.assertion_results == (
        {
            "assertion_id": "source_changed",
            "change_id": "feature_enabled",
            "assertion_type": "file_changed",
            "status": "failed",
            "evidence_refs": [],
        },
    )
    assert trial.to_dict()["assertion_results"][0]["assertion_id"] == "source_changed"
