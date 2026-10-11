import json
import shutil

import pytest

from code_governance.evaluation import core_capability
from code_governance.evaluation.core_capability import FIXTURE_ROOT, run_core_capability_evaluation


@pytest.fixture(scope="module")
def evaluation():
    return run_core_capability_evaluation()


def test_fixtures_self_validate_and_no_false_success(evaluation):
    assert evaluation["fixture_self_validation"] == "PASS"
    assert len(evaluation["tasks"]) == 3
    assert evaluation["false_success_count"] == 0
    assert evaluation["harness_invariant_gap"] is None
    assert evaluation["result"] == "PASS"
    for task in evaluation["tasks"]:
        assert task["oracle_before"]["goal_satisfied"] is False
        assert task["self_validation"]["passed"] is True


def test_true_success_is_accepted_with_real_evidence(evaluation):
    task = evaluation["tasks"][0]
    assert task["task_id"] == "true_success"
    assert task["harness_result"]["success"] is True
    assert task["validation_result"]["status"] == "PASS"
    assert task["oracle_after"]["goal_satisfied"] is True
    assert task["first_failing_boundary"] is None
    assert task["verification_evidence"][0]["passed"] is True
    assert all(item["status"] == "passed" for item in task["assertion_results"])
    assert any(action["operation"] == "file.replace" for action in task["trajectory"])
    assert task["required_changes_verified"] == {"fix_add_behavior": True}


def test_unrelated_test_pass_cannot_mask_wrong_behavior(evaluation):
    task = evaluation["tasks"][1]
    ordinary, behavior = task["verification_evidence"]
    assert ordinary["passed"] is True
    assert behavior["execution_result"]["status"] == "SUCCESS"
    assert behavior["actual_result"]["returncode"] == 0
    assert behavior["passed"] is True  # Command success is not behavioral success.
    assert task["assertion_results"][0]["status"] == "failed"
    assert task["oracle_after"]["observed"] == {"accepted": True}
    assert task["harness_result"]["success"] is False
    assert task["first_failing_boundary"] == "ASSERTION"
    assert "ASSERTION_FAILED" in {f["code"] for f in task["validation_result"]["findings"]}
    assert task["required_changes_verified"] == {"reject_empty_password": False}


def test_model_finish_does_not_bypass_final_validation(evaluation):
    task = evaluation["tasks"][2]
    assert "MODEL_FINISHED" in task["runtime_statuses"]
    assert "VERIFYING" in task["runtime_statuses"]
    assert task["harness_result"]["success"] is False
    assert task["oracle_after"]["goal_satisfied"] is False
    result = task["verification_evidence"][0]
    assert result["execution_result"]["status"] == "SUCCESS"
    assert result["actual_result"]["returncode"] == 1
    assert result["passed"] is False
    assert task["first_failing_boundary"] == "VERIFICATION"
    assert "verification_failed" in {f["code"] for f in task["validation_result"]["findings"]}
    assert task["required_changes_verified"] == {"implement_feature": False}


def test_uniform_capture_preserves_lineage_and_approval(evaluation):
    required = {
        "task_id", "fixture_type", "contract_version", "expected_changes",
        "claim_coverage", "verification_claims", "assertions", "trajectory",
        "verification_evidence", "assertion_results", "validation_result",
        "harness_result", "first_failing_boundary",
    }
    for task in evaluation["tasks"]:
        assert required <= task.keys()
        assert task["contract_version"] == 3
        assert task["benchmark_trial"]["failure_category"] == task["first_failing_boundary"]
        assert task["benchmark_trial"]["data_gaps"] == []
        assert task["approvals"]
        assert all(item["outcome"] == "APPROVED" for item in task["approvals"])
        for item in task["verification_evidence"]:
            assert item["claim_id"] in {c["id"] for c in task["verification_claims"]}
            assert item["request_id"] == item["action_id"] == item["execution_result_id"]
            assert item["execution_result_id"] == item["execution_result"]["action_id"]
    json.dumps(evaluation, allow_nan=False)


def test_unrelated_probe_failure_is_not_capability_pass(tmp_path):
    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, root)
    (root / "fixture_false_evidence/tests/probes/check_empty_password.py").unlink()
    result = run_core_capability_evaluation(root)
    assert result["result"] == "PARTIAL"
    assert result["harness_invariant_gap"] is None
    assert result["tasks"][1]["first_failing_boundary"] == "VERIFICATION"
    assert result["tasks"][1]["scenario_passed"] is False


def test_oracle_setup_error_is_not_harness_invariant_gap(tmp_path):
    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, root)
    (root / "fixture_true_success/src/calculator.py").unlink()
    result = run_core_capability_evaluation(root)
    assert result["result"] == "PARTIAL"
    assert result["harness_invariant_gap"] is None
    assert result["unresolved_gaps"][0]["boundary"] == "FIXTURE_ORACLE_BEFORE"
    assert result["unresolved_gaps"][0]["error_type"] == "RuntimeError"
    assert "FileNotFoundError" in result["unresolved_gaps"][0]["detail"]


def test_after_oracle_error_is_attributed_to_evaluator(monkeypatch):
    real_oracle = core_capability._oracle
    calls = 0

    def broken_after_oracle(task_id, repository):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected external after-oracle error")
        return real_oracle(task_id, repository)

    # Fault injection touches only the evaluator, never Harness or execution.
    monkeypatch.setattr(core_capability, "_oracle", broken_after_oracle)
    result = run_core_capability_evaluation()
    assert result["result"] == "PARTIAL"
    assert result["harness_invariant_gap"] is None
    assert result["unresolved_gaps"][0]["boundary"] == "FIXTURE_ORACLE_AFTER"
    assert result["unresolved_gaps"][0]["detail"] == "injected external after-oracle error"
