from dataclasses import replace

import pytest

from code_governance.context.builder import ContextBuilder
from code_governance.evaluation.repair_capability import run_repair_evaluation


@pytest.fixture(scope="module")
def evaluation():
    return run_repair_evaluation()


@pytest.mark.parametrize("index,boundary", [(0, "ASSERTION"), (1, "VERIFICATION")])
def test_structured_failure_drives_real_governed_repair(evaluation, index, boundary):
    record = evaluation[index]
    assert record["harness_result"]["success"] is True
    assert record["oracle_after"]["goal_satisfied"] is True
    assert record["scenario_passed"] is True
    assert record["repairs"] == 1
    failed, passed = record["validation_attempts"]
    assert failed["validation_result"]["status"] == "FAIL"
    assert passed["validation_result"]["status"] == "PASS"
    assert failed["failure_contexts"][0]["failure_boundary"] == boundary
    assert record["first_failing_boundary"] == boundary
    assert record["final_failure_boundary"] is None
    assert record["agent_repair_contexts"]
    assert record["repair_prompt_exposed"] is True
    assert any(action["operation"] == "file.replace" for action in record["trajectory"])
    assert all(approval["outcome"] == "APPROVED" for approval in record["approvals"])


def test_assertion_repair_does_not_equate_probe_exit_zero_with_goal(evaluation):
    failed = evaluation[0]["validation_attempts"][0]
    assert all(item["passed"] for item in failed["verification_evidence"])
    failure, = failed["failure_contexts"]
    assert failure["expected"] == {"accepted": False}
    assert failure["observed"] == {"accepted": True}
    assert failure["changed_files"] == ["src/auth.py"]
    assert "ASSERTION_FAILED" in {f["code"] for f in failed["validation_result"]["findings"]}


def test_verification_repair_owns_boundary_before_derived_assertion(evaluation):
    failed = evaluation[1]["validation_attempts"][0]
    evidence, = failed["verification_evidence"]
    assert evidence["execution_result"]["status"] == "SUCCESS"
    assert evidence["actual_result"]["returncode"] == 1
    failure, = failed["failure_contexts"]
    assert failure["claim_id"] == "feature_test"
    assert failure["expected"] == {"exit_code": 0}
    assert failure["observed"]["returncode"] == 1
    assert failure["observed"]["assertion_status"] == "failed"
    assert failure["changed_files"] == ["src/feature.py"]
    assert "ASSERTION_FAILED" not in {f["code"] for f in failed["validation_result"]["findings"]}


def test_failed_attempt_and_repair_actions_link_to_new_validation(evaluation):
    for record in evaluation:
        first, second = record["validation_attempts"]
        assert first["attempt_id"] != second["attempt_id"]
        assert second["repair_failure_ids"] == [f["failure_id"] for f in first["failure_contexts"]]
        assert set(second["action_ids"]) <= {a["id"] for a in record["trajectory"]}
        assert set(first["action_ids"]).isdisjoint(second["action_ids"])
        assert second["workspace_identity"]["fingerprint_after"] != first["workspace_identity"]["fingerprint_after"]
        for attempt in (first, second):
            for evidence in attempt["verification_evidence"]:
                assert evidence["request_id"] == evidence["action_id"] == evidence["execution_result_id"]
        assert second["failure_contexts"] == []


def test_without_failure_context_retry_cannot_fix_goal():
    class NoFailureExposure(ContextBuilder):
        def with_state(self, context, state):
            return replace(super().with_state(context, state), repair_context=())

    records = run_repair_evaluation(context_builder=NoFailureExposure())
    for record in records:
        assert record["repairs"] == 1
        assert record["harness_result"]["success"] is False
        assert record["oracle_after"]["goal_satisfied"] is False
        assert record["agent_repair_contexts"] == []


def test_verification_process_failure_does_not_enter_repair_cycle():
    records = run_repair_evaluation(deny_verification=True)
    for record in records:
        assert record["harness_result"]["success"] is False
        assert record["repairs"] == 0
        assert record["agent_repair_contexts"] == []
        assert len(record["validation_attempts"]) == 1
        assert record["validation_attempts"][0]["failure_contexts"] == []
        assert record["first_failing_boundary"] == "VERIFICATION_EXECUTION"


def test_interrupted_repair_preserves_actual_failure_and_prior_validation(monkeypatch):
    from code_governance.evaluation.repair_capability import _RepairScenarioAgent

    decide = _RepairScenarioAgent.decide

    def fail_repair(self, state, context):
        if context.repair_context:
            raise RuntimeError("repair decision unavailable")
        return decide(self, state, context)

    monkeypatch.setattr(_RepairScenarioAgent, "decide", fail_repair)
    for record in run_repair_evaluation():
        result = record["harness_result"]
        assert result["success"] is False
        assert result["failure"]["stage"] == "ADAPTER"
        assert result["failure"]["category"] == "DECISION_ERROR"
        assert result["termination_reason"] == "DECISION_ERROR"
        assert result["final_outcome"] == "FAILED"
        assert record["validation_result"]["status"] == "FAIL"
        assert record["workspace_identity"] == record["validation_attempts"][0]["workspace_identity"]
        assert record["verification_evidence"]
        assert len(record["validation_attempts"]) == 1
