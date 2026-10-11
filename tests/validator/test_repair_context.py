import json
from dataclasses import replace

from code_governance.contract.schema import ChangeContract, ExpectedChange, Verification
from code_governance.git.context import ChangeContext, FileChange
from code_governance.runtime.execution import ExecutionResult, FailureRecord
from code_governance.validator.assertions import AssertionEvaluatorRegistry
from code_governance.validator.core import Finding, ValidatorEngine
from code_governance.validator.evidence import ValidationEvidence, VerificationEvidence, WorkspaceIdentity
from code_governance.validator.repair_context import build_repair_contexts


def validation_case(*, returncode=0, execution_failed=False, observed=None):
    contract = ChangeContract(
        goal="Reject empty passwords", changes=[ExpectedChange(id="reject_empty", description="Reject empty passwords")],
        verification=Verification(
            claims=[{"id": "probe", "description": "Observe password acceptance", "request": {"argv": ["python", "probe.py"]}}],
            coverage=[{"claim_id": "probe", "change_id": "reject_empty", "coverage_type": "direct"}],
            assertions=[{"id": "empty_behavior", "change_id": "reject_empty", "claim_id": "probe",
                         "assertion_type": "behavior_probe", "config": {"probe_id": "probe", "expected": {"accepted": False}}}],
        ),
    )
    stdout = json.dumps({"protocol": "changeguard.behavior_probe.v1", "probe_id": "probe",
                         "observed": {"accepted": True} if observed is None else observed})
    result = ExecutionResult(
        action_id="verification:probe", status="FAILURE" if execution_failed else "SUCCESS",
        metadata={"returncode": returncode, "stdout_preview": stdout, "stdout_length": len(stdout), "stdout_truncated": False},
        failure=FailureRecord("TOOL", "TOOL_EXECUTION") if execution_failed else None,
    )
    evidence = ValidationEvidence(
        workspace=WorkspaceIdentity("D:/fixture", "head", "head", "before", "before"),
        change_context=ChangeContext((FileChange("src/auth.py", "M", 1, 1),)), execution_results=(result,),
        verification=(VerificationEvidence("probe", "verification:probe", "verification:probe", "verification:probe",
            ("python", "probe.py"), {"exit_code": 0}, {"returncode": returncode},
            not execution_failed and returncode == 0, result),),
    )
    assertions = tuple(AssertionEvaluatorRegistry().evaluate(a, evidence) for a in contract.verification.assertions)
    return contract, evidence, ValidatorEngine().validate(contract, evidence, assertions)


def test_assertion_failure_projects_expected_observed_and_lineage():
    contract, evidence, validation = validation_case()
    failure, = build_repair_contexts(contract, evidence, validation, attempt_id="run:validation:1")
    assert failure.failure_boundary == "ASSERTION"
    assert failure.change_id == "reject_empty"
    assert failure.assertion_id == "empty_behavior"
    assert failure.claim_id == "probe"
    assert failure.expected == {"accepted": False}
    assert failure.observed == {"accepted": True}
    assert failure.changed_files == ("src/auth.py",)
    assert failure.workspace_identity == evidence.workspace
    assert "execution_result:verification:probe" in failure.evidence_refs
    payload = failure.to_dict()
    assert "execution_result" not in payload and "stdout" not in payload
    assert "prompt" not in payload


def test_verification_failure_preserves_claim_and_exit_fact():
    contract, evidence, validation = validation_case(returncode=1)
    failure, = build_repair_contexts(contract, evidence, validation)
    assert failure.failure_boundary == "VERIFICATION"
    assert failure.claim_id == "probe"
    assert failure.assertion_id == "empty_behavior"
    assert failure.expected == {"exit_code": 0}
    assert failure.observed["returncode"] == 1
    assert failure.observed["assertion_status"] == "unsupported"
    assert "ASSERTION_FAILED" not in {f.code for f in validation.findings}


def test_environment_lifecycle_failure_never_produces_repair_context():
    contract, evidence, validation = validation_case(execution_failed=True)
    assert validation.status == "FAIL"
    assert build_repair_contexts(contract, evidence, validation) == ()


def test_pass_or_unsupported_evidence_never_produces_repair_context():
    contract, evidence, validation = validation_case(observed={"accepted": False})
    assert build_repair_contexts(contract, evidence, validation) == ()
    _, evidence, validation = validation_case()
    validation = replace(validation, findings=(Finding("assertion", "ASSERTION_UNSUPPORTED", "malformed", severity="error"),))
    assert build_repair_contexts(contract, evidence, validation) == ()


def test_mixed_environment_error_cannot_be_masked_by_repairable_assertion():
    contract, evidence, validation = validation_case()
    validation = replace(validation, findings=validation.findings + (
        Finding("workspace_identity", "workspace_revision_mismatch", "workspace changed", severity="error"),))
    assert build_repair_contexts(contract, evidence, validation) == ()


def test_large_values_are_explicitly_bounded_not_copied_as_stdout():
    contract, evidence, validation = validation_case(observed={"accepted": True, "large": "x" * 20000})
    failure, = build_repair_contexts(contract, evidence, validation)
    assert len(json.dumps(failure.to_dict())) < 6000
    assert failure.observed["truncated"] is True
    assert failure.observed["value_sha256"]
    original = json.loads(evidence.verification[0].execution_result.metadata["stdout_preview"])
    assert len(original["observed"]["large"]) == 20000


def test_attempt_identity_disambiguates_repeated_failed_verification_ids():
    contract, evidence, validation = validation_case()
    first, = build_repair_contexts(contract, evidence, validation, attempt_id="run:validation:1")
    second, = build_repair_contexts(contract, evidence, validation, attempt_id="run:validation:2")
    assert first.failure_id != second.failure_id
    assert first.evidence_refs == second.evidence_refs


def test_shared_claim_has_explicit_context_for_each_covered_required_change():
    contract, evidence, validation = validation_case(returncode=1)
    contract.changes.append(ExpectedChange(id="keep_valid", description="Keep valid passwords accepted"))
    contract.verification.coverage.append(type(contract.verification.coverage[0])(
        claim_id="probe", change_id="keep_valid", coverage_type="direct"))
    failures = build_repair_contexts(contract, evidence, validation)
    assert {item.change_id for item in failures} == {"reject_empty", "keep_valid"}
    assert len({item.failure_id for item in failures}) == 2


def test_changed_paths_are_complete_identities_not_truncated_strings():
    contract, evidence, validation = validation_case()
    path = "src/" + "/".join(["segment" * 10] * 8) + "/auth.py"
    evidence = replace(evidence, change_context=ChangeContext((FileChange(path, "M", 1, 1),)))
    failure, = build_repair_contexts(contract, evidence, validation)
    assert failure.changed_files == (path,)


def test_complete_context_is_bounded_including_workspace_and_references():
    contract, evidence, validation = validation_case()
    failure, = build_repair_contexts(contract, evidence, validation)
    assert len(json.dumps(failure.to_dict(), ensure_ascii=False).encode("utf-8")) <= 8192
    oversized = replace(evidence, workspace=replace(evidence.workspace, repository="x" * 10000))
    assert build_repair_contexts(contract, oversized, validation) == ()
    assertion = replace(validation.assertion_results[0], evidence_refs=("x" * 10000,))
    assert build_repair_contexts(contract, evidence, replace(validation, assertion_results=(assertion,))) == ()
