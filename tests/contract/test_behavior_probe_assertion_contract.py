from code_governance.contract.schema import (
    ChangeContract,
    ExpectedChange,
    Verification,
    VerificationClaim,
)
from code_governance.contract.validator import ContractValidator


def _contract(*, assertion_config, claim_exit_code=0, claim_id="probe"):
    claim = VerificationClaim(
        id="probe",
        description="Observe behavior.",
        request={"argv": ("python", "probe.py")},
        expected_result={"exit_code": claim_exit_code},
    )
    return ChangeContract(
        goal="Change behavior.",
        changes=[ExpectedChange(id="change", description="Expected behavior changes.")],
        verification=Verification(
            claims=[claim],
            assertions=[
                {
                    "id": "behavior",
                    "change_id": "change",
                    "claim_id": claim_id,
                    "assertion_type": "behavior_probe",
                    "config": assertion_config,
                }
            ],
        ),
    )


def test_behavior_probe_contract_requires_probe_id_and_expected_json_value():
    contract = _contract(assertion_config={"probe_id": "", "expected": {1, 2}})

    result = ContractValidator().validate(contract)

    assert "behavior_probe_config_invalid" in {finding.code for finding in result.findings}


def test_behavior_probe_contract_requires_success_exit_code_zero():
    contract = _contract(
        assertion_config={"probe_id": "probe", "expected": False},
        claim_exit_code=1,
    )

    result = ContractValidator().validate(contract)

    assert "behavior_probe_exit_code_required" in {
        finding.code for finding in result.findings
    }
