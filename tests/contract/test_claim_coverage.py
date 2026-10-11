from code_governance.contract.schema import (
    ChangeContract,
    ClaimCoverage,
    ExpectedChange,
    Scope,
    Verification,
    VerificationClaim,
)
from code_governance.contract.validator import ContractValidator


def _claim(claim_id="parser_tests"):
    return VerificationClaim(
        id=claim_id,
        description="Parser tests pass.",
        request={"argv": ("python", "-m", "pytest", "tests/test_parser.py")},
    )


def _change(change_id="parser_handles_empty", requirement="required"):
    return ExpectedChange(
        id=change_id,
        description="Parser handles empty input without crashing.",
        requirement=requirement,
    )


def _contract(changes, claims=(), coverage=()):
    return ChangeContract(
        goal="Fix parser empty-input crash.",
        scope=Scope(paths=["src/parser.py"]),
        changes=list(changes),
        verification=Verification(claims=list(claims), coverage=list(coverage)),
    )


def test_required_change_with_direct_coverage_is_contract_valid():
    contract = _contract(
        [_change()],
        [_claim()],
        [ClaimCoverage(claim_id="parser_tests", change_id="parser_handles_empty", coverage_type="direct")],
    )

    assert ContractValidator().validate(contract).is_valid is True


def test_required_change_without_coverage_is_rejected():
    result = ContractValidator().validate(_contract([_change()], [_claim()]))

    assert {finding.code for finding in result.findings} == {"EXPECTED_CHANGE_UNVERIFIED"}


def test_coverage_reference_to_missing_claim_is_rejected():
    result = ContractValidator().validate(
        _contract(
            [_change()],
            [_claim()],
            [ClaimCoverage(claim_id="missing", change_id="parser_handles_empty", coverage_type="direct")],
        )
    )

    assert {finding.code for finding in result.findings} == {"coverage_claim_missing"}


def test_coverage_reference_to_missing_change_is_rejected():
    result = ContractValidator().validate(
        _contract(
            [_change()],
            [_claim()],
            [ClaimCoverage(claim_id="parser_tests", change_id="missing", coverage_type="direct")],
        )
    )

    assert {finding.code for finding in result.findings} == {"coverage_change_missing"}


def test_planning_only_change_can_remain_uncovered():
    result = ContractValidator().validate(
        _contract([_change(requirement="planning_only")], [_claim()])
    )

    assert result.is_valid is True


def test_one_claim_can_cover_two_required_changes():
    changes = [_change(), _change("parser_preserves_api")]
    coverage = [
        ClaimCoverage(claim_id="parser_tests", change_id=change.id, coverage_type="direct")
        for change in changes
    ]

    assert ContractValidator().validate(_contract(changes, [_claim()], coverage)).is_valid is True


def test_indirect_coverage_does_not_satisfy_a_required_change():
    result = ContractValidator().validate(
        _contract(
            [_change()],
            [_claim()],
            [ClaimCoverage(claim_id="parser_tests", change_id="parser_handles_empty", coverage_type="indirect")],
        )
    )

    assert {finding.code for finding in result.findings} == {"EXPECTED_CHANGE_UNVERIFIED"}


def test_duplicate_contract_identity_is_rejected_at_contract_validation():
    claim = _claim()
    change = _change()
    result = ContractValidator().validate(
        _contract(
            [change, _change(change.id)],
            [claim, _claim(claim.id)],
            [
                ClaimCoverage(
                    claim_id=claim.id,
                    change_id=change.id,
                    coverage_type="direct",
                ),
                ClaimCoverage(
                    claim_id=claim.id,
                    change_id=change.id,
                    coverage_type="direct",
                ),
            ],
        )
    )

    assert {
        finding.code for finding in result.findings
    } >= {
        "expected_change_id_duplicate",
        "verification_claim_id_duplicate",
        "coverage_pair_duplicate",
    }


def test_v2_string_expected_changes_normalize_to_unbound_planning_only_changes():
    contract = ChangeContract.model_validate(
        {
            "version": 2,
            "goal": "Fix login bug.",
            "scope": {"paths": ["src/login.py"]},
            "expected_changes": ["Login accepts valid credentials."],
            "verification": {"claims": [_claim().model_dump(mode="python")]},
        }
    )

    assert contract.version == 3
    assert contract.changes[0].id == "legacy_0"
    assert contract.changes[0].requirement == "planning_only"
    assert contract.verification.coverage == []
