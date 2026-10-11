from code_governance.git.context import ChangeContext, FileChange
from code_governance.report.report import build_report, render_json, render_report
from code_governance.runtime.execution import ExecutionResult
from code_governance.validator.core import Finding, ValidationResult
from code_governance.validator.evidence import (
    ValidationEvidence,
    VerificationEvidence,
    WorkspaceIdentity,
)


def context():
    return ChangeContext((FileChange("app.py", "M", 2, 1),))


def evidence(*, verification=()):
    results = tuple(item.execution_result for item in verification)
    return ValidationEvidence(
        workspace=WorkspaceIdentity("D:/fixture", "base", "base", "one", "one"),
        change_context=context(),
        execution_results=results,
        verification=tuple(verification),
    )


def test_report_pass_contains_evidence():
    report = build_report(evidence(), ValidationResult(()))

    assert report.status == "PASS"
    assert report.findings == ()
    assert report.evidence.changed_files == ("app.py",)
    assert report.evidence.additions == 2
    assert report.evidence.deletions == 1
    assert "Status: PASS" in render_report(report)


def test_warning_and_fail_status_follow_finding_severity():
    warning = Finding("scope", "unexpected_file", "unexpected", path="extra.py")
    failure = Finding("constraint", "forbidden_file", "forbidden", path="secret.py", severity="error")

    warning_report = build_report(evidence(), ValidationResult((warning,)))
    failure_report = build_report(evidence(), ValidationResult((failure,)))

    assert warning_report.status == "WARNING"
    assert failure_report.status == "FAIL"
    assert "extra.py" in render_report(warning_report)
    assert "secret.py" in render_report(failure_report)


def test_json_report_contains_findings_evidence_and_verification(tmp_path):
    result = ExecutionResult(
        action_id="verification:parser",
        status="SUCCESS",
        metadata={"returncode": 2},
    )
    verification = VerificationEvidence(
        claim_id="parser",
        request_id="verification:parser",
        action_id="verification:parser",
        execution_result_id="verification:parser",
        argv=("python", "-m", "pytest", "tests/test_parser.py"),
        expected_result={"exit_code": 0},
        actual_result={"returncode": 2},
        passed=False,
        execution_result=result,
    )
    report = build_report(
        evidence(verification=(verification,)),
        ValidationResult((
            Finding("verification", "verification_failed", "parser tests failed", severity="error"),
        )),
    )

    payload = __import__("json").loads(render_json(report))

    assert payload["status"] == "FAIL"
    assert payload["findings"][0]["code"] == "verification_failed"
    assert payload["evidence"]["changed_files"] == ["app.py"]
    assert payload["verification"]["all_passed"] is False


def test_report_does_not_turn_verification_display_into_a_validation_decision():
    result = ExecutionResult(
        action_id="verification:parser",
        status="SUCCESS",
        metadata={"returncode": 2},
    )
    verification = VerificationEvidence(
        claim_id="parser",
        request_id="verification:parser",
        action_id="verification:parser",
        execution_result_id="verification:parser",
        argv=("python", "-m", "pytest"),
        expected_result={"exit_code": 0},
        actual_result={"returncode": 2},
        passed=False,
        execution_result=result,
    )

    report = build_report(evidence(verification=(verification,)), ValidationResult(()))

    assert report.status == "PASS"
    assert report.findings == ()
    assert report.verification.all_passed is False
