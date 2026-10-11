import json
from pathlib import Path
import subprocess
import sys

from code_governance.evaluation.real_model_repair import (
    FIXTURE_ROOT,
    _ApprovalBoundWorkspaceManager,
    _ScopedFixtureApproval,
    real_model_repair_tasks,
)
from code_governance.tools import ApprovalOutcome
from code_governance.tools.approval import ApprovalRequest


def _run(argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30, check=False)


def test_real_model_repair_fixture_contracts_have_separate_claims_and_direct_coverage():
    behavior, verification, wrong_location = real_model_repair_tasks()
    assert len(behavior.verification.claims) == 2
    assert {item.assertion_type.value for item in behavior.verification.assertions} == {"exit_code", "behavior_probe"}
    assert all(item.coverage_type == "direct" for task in (behavior, verification, wrong_location)
               for item in task.verification.coverage)
    assert verification.verification.claims[0].request.argv[-1] == "tests/test_calculator.py"
    assert wrong_location.expected_files == ("src/config.py", "src/service.py")


def test_real_model_repair_fixtures_reproduce_bug_and_control_cases():
    behavior = FIXTURE_ROOT / "behavior"
    tests = _run((sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_auth.py"), behavior)
    probe = _run((sys.executable, "-B", "tests/probes/empty_password_probe.py"), behavior)
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert json.loads(probe.stdout)["observed"]["accepted"] is True

    for name, command in (
        ("verification", "tests/test_calculator.py"),
        ("wrong_location", "tests/test_service.py"),
    ):
        root = FIXTURE_ROOT / name
        result = _run((sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", command), root)
        assert result.returncode == 1, result.stdout + result.stderr


def test_fixture_approval_allows_writes_in_harness_managed_workspace(tmp_path):
    source_repo = tmp_path / "input-repo"
    (source_repo / ".git").mkdir(parents=True)
    (source_repo / "src").mkdir()
    (source_repo / "src" / "auth.py").write_text("def check_password(password): return True\n", encoding="utf-8")

    task = real_model_repair_tasks()[0]
    approval = _ScopedFixtureApproval(task)
    manager = _ApprovalBoundWorkspaceManager(approval)
    workspace = manager.create(source_repo)
    try:
        def request(target):
            return approval.request(ApprovalRequest(
                action_id="repair-action",
                tool_name="filesystem",
                canonical_target=str(target),
                effect="file.replace",
                reason="Apply the fixture-local repair.",
            ))

        allowed = request(workspace.path / "src" / "auth.py")
        source_copy = request(source_repo / "src" / "auth.py")
        unrelated = request(workspace.path / "src" / "other.py")
    finally:
        manager.cleanup(workspace)

    assert allowed is ApprovalOutcome.APPROVED
    assert source_copy is ApprovalOutcome.REJECTED
    assert unrelated is ApprovalOutcome.REJECTED
