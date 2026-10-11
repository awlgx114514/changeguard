from code_governance.contract.schema import ChangeContract
from code_governance.git.context import ChangeContext, FileChange
from code_governance.validator.core import Finding, ValidatorEngine
from code_governance.validator.evidence import ValidationEvidence, WorkspaceIdentity
from code_governance.validator.rules import DiffSizeRule, ForbiddenFileRule, UnexpectedFileRule


def contract(*, files=None, forbidden=None, level="L1"):
    return ChangeContract.from_mapping(
        {
            "metadata": {},
            "intent": {"goal": "test"},
            "expected": {"files": files or [], "change_level": level},
            "constraints": {"forbidden": forbidden or []},
            "verification": {},
        }
    )


def context(*changes):
    return ChangeContext(tuple(changes))


def evidence(*changes):
    return ValidationEvidence(
        workspace=WorkspaceIdentity("D:/fixture", "base", "base", "one", "one"),
        change_context=context(*changes),
        execution_results=(),
        verification=(),
    )


def change(path, status="M", additions=1, deletions=0):
    return FileChange(path, status, additions, deletions)


def test_unexpected_file_rule_reports_files_outside_expected_scope():
    findings = UnexpectedFileRule().check(
        contract(files=["src/app.py"]),
        context(change("src/app.py"), change("src/extra.py")),
    )

    assert len(findings) == 1
    assert findings[0].code == "unexpected_scope_change"
    assert findings[0].path == "src/extra.py"


def test_forbidden_file_rule_reports_exact_forbidden_path():
    findings = ForbiddenFileRule().check(
        contract(forbidden=["config.py"]),
        context(change("config.py"), change("src/app.py")),
    )

    assert len(findings) == 1
    assert findings[0].code == "forbidden_file"
    assert findings[0].path == "config.py"


def test_diff_size_rule_reports_file_count_over_limit():
    changes = [change(f"file-{index}.py") for index in range(6)]

    findings = DiffSizeRule().check(contract(level="L1"), context(*changes))

    assert len(findings) == 1
    assert findings[0].code == "diff_size_exceeded"
    assert "6 files" in findings[0].message


def test_diff_size_rule_reports_line_count_over_limit():
    findings = DiffSizeRule().check(
        contract(level="L1"),
        context(change("large.py", additions=150, deletions=51)),
    )

    assert len(findings) == 1
    assert "201 lines" in findings[0].message


def test_diff_size_rule_allows_l3_without_limit():
    changes = [change(f"file-{index}.py", additions=1000) for index in range(100)]

    assert DiffSizeRule().check(contract(level="L3"), context(*changes)) == ()


def test_engine_aggregates_findings_and_reports_failure():
    result = ValidatorEngine().validate(
        contract(files=["src/app.py"], forbidden=["blocked.py"]),
        evidence(change("src/extra.py"), change("blocked.py")),
    )

    assert result.is_valid is False
    assert {finding.code for finding in result.findings} == {
        "unexpected_scope_change",
        "forbidden_file",
    }


def test_engine_reports_valid_result_when_no_rule_finds_issue():
    result = ValidatorEngine().validate(
        contract(files=["src/app.py"]),
        evidence(change("src/app.py")),
    )

    assert result.is_valid is True
    assert result.findings == ()


def test_rule_rejects_no_inputs_by_returning_no_findings():
    assert UnexpectedFileRule().check(contract(), context()) == ()
    assert ForbiddenFileRule().check(contract(), context()) == ()
    assert DiffSizeRule().check(contract(level="L3"), context()) == ()


def test_engine_accepts_custom_rule():
    class CustomRule:
        name = "custom"

        def check(self, contract, context):
            return (Finding(self.name, "custom_finding", "custom finding"),)

    result = ValidatorEngine(rules=[CustomRule()]).validate(contract(), evidence())

    assert result.is_valid is False
    assert result.findings[0].code == "custom_finding"
