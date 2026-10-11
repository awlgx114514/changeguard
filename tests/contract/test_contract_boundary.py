from code_governance.agent.contract_agent import ContractAgent
from code_governance.contract.schema import (
    ChangeContract,
    ClaimCoverage,
    Constraints,
    ExpectedChange,
    Scope,
    Verification,
    VerificationClaim,
)
from code_governance.contract.validator import ContractValidator
from code_governance.git.context import ChangeContext, FileChange
from code_governance.repo.analyzer import RepoContext
from code_governance.runtime.state import AgentState
from code_governance.validator.core import ValidatorEngine
from code_governance.validator.evidence import ValidationEvidence, WorkspaceIdentity


def repository_context() -> RepoContext:
    return RepoContext(
        root=".",
        files=("pyproject.toml", "src/parser.py", "tests/test_parser.py", "uv.lock"),
        python_modules=("parser",),
        git_status="clean",
        core_files=("src/parser.py",),
        module_relationships={},
    )


def valid_contract(**updates) -> ChangeContract:
    claim = VerificationClaim(
        id="parser_regression",
        description="Parser regression test passes.",
        request={"argv": ("uv", "run", "pytest", "tests/test_parser.py")},
    )
    values = {
        "goal": "Prevent the parser from crashing on empty input.",
        "non_goals": ["Do not change the public parser API."],
        "scope": Scope(paths=["src/parser.py"], change_level="L1"),
        "changes": [
            ExpectedChange(
                id="parser_empty_input",
                description="Empty input is handled without a crash.",
            )
        ],
        "constraints": Constraints(),
        "verification": Verification(
            claims=[claim],
            coverage=[
                ClaimCoverage(
                    claim_id=claim.id,
                    change_id="parser_empty_input",
                    coverage_type="direct",
                )
            ],
        ),
        "needs_clarification": False,
    }
    values.update(updates)
    return ChangeContract(**values)


def test_contract_agent_derives_a_verifiable_expected_change_from_repository_facts():
    contract = ContractAgent().generate(
        "Fix parser crash on empty input.",
        repository_context(),
    )

    assert contract.goal
    assert contract.changes
    assert contract.scope.paths == ["src/parser.py"]
    assert contract.verification.claims[0].request.argv == ("uv", "run", "pytest", "tests/test_parser.py")
    assert contract.changes[0].requirement == "required"
    assert {
        (coverage.claim_id, coverage.change_id, coverage.coverage_type)
        for coverage in contract.verification.coverage
    } == {
        (contract.verification.claims[0].id, contract.changes[0].id, "direct")
    }
    assert contract.needs_clarification is False


def test_contract_agent_marks_underspecified_task_for_clarification_without_inventing_scope():
    contract = ContractAgent().generate("Improve performance.", repository_context())

    assert contract.needs_clarification is True
    assert contract.scope.paths == []
    assert contract.verification.claims == []


def test_contract_agent_keeps_refactor_behavior_preservation_as_a_non_goal():
    contract = ContractAgent().generate(
        "Refactor src/parser.py without behavior change. Run uv run pytest tests/test_parser.py.",
        repository_context(),
    )

    assert contract.needs_clarification is False
    assert any("behavior" in item.lower() for item in contract.non_goals)


def test_contract_validator_rejects_empty_verification_before_runtime():
    result = ContractValidator().validate(
        valid_contract(verification=Verification())
    )

    assert result.is_valid is False
    assert "verification_required" in {finding.code for finding in result.findings}


def test_existing_validator_reports_an_unexpected_scope_change():
    result = ValidatorEngine().validate(
        valid_contract(scope=Scope(paths=["src/authentication"], change_level="L1")),
        ValidationEvidence(
            workspace=WorkspaceIdentity("D:/fixture", "base", "base", "one", "one"),
            change_context=ChangeContext((FileChange("src/payment.py", "M", 1, 0),)),
            execution_results=(),
            verification=(),
        ),
    )

    assert "unexpected_scope_change" in {finding.code for finding in result.findings}


def test_runtime_state_preserves_the_validated_contract_as_runtime_facts():
    contract = valid_contract()
    state = AgentState.initialize("Fix parser crash", contract=contract)

    restored = AgentState.from_dict(state.to_dict())

    assert state.contract["goal"] == contract.goal
    assert restored.contract == state.contract
