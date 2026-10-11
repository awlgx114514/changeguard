import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from code_governance.cli import build_parser, main
from code_governance.contract.loader import load_contract
from code_governance.contract.validator import ContractValidator


def run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def prepare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test User")
    (repo / "app.py").write_text("old\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "initial")
    (repo / "app.py").write_text("new\n", encoding="utf-8")
    return repo


def valid_v3_contract():
    return {
        "version": 3,
        "goal": "update app",
        "scope": {"paths": ["app.py"], "change_level": "L1"},
        "changes": [{"id": "app_updated", "description": "app contains the new text", "requirement": "required"}],
        "verification": {
            "claims": [{
                "id": "app_check",
                "description": "app contains the new text",
                "request": {"argv": [
                    sys.executable, "-c",
                    "from pathlib import Path; assert Path('app.py').read_text() == 'new\\n'",
                ]},
                "expected_result": {"exit_code": 0},
            }],
            "coverage": [{"claim_id": "app_check", "change_id": "app_updated", "coverage_type": "direct"}],
            "assertions": [],
        },
    }


def write_payload(tmp_path: Path, payload) -> Path:
    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return contract


def write_contract(tmp_path: Path, expected_files: str = "app.py", verification=None) -> Path:
    payload = valid_v3_contract()
    payload["scope"]["paths"] = [expected_files]
    if verification is not None:
        payload["verification"] = verification
    return write_payload(tmp_path, payload)


@pytest.fixture
def approve_verification(monkeypatch):
    class ApprovedProvider:
        def request(self, request):
            from code_governance.tools import ApprovalOutcome

            return ApprovalOutcome.APPROVED

    monkeypatch.setattr("code_governance.cli.ConsoleApprovalProvider", ApprovedProvider)


def test_validate_command_reports_pass(capsys, tmp_path, approve_verification):
    repo = prepare_repo(tmp_path)
    contract = write_contract(tmp_path)

    exit_code = main(["validate", str(contract), "--repo", str(repo), "--approval", "interactive"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Status: PASS" in output
    assert "app.py" in output
    assert "Additions: 1" in output
    assert "Deletions: 1" in output


def test_validate_command_returns_nonzero_for_warning(capsys, tmp_path, approve_verification):
    repo = prepare_repo(tmp_path)
    contract = write_contract(tmp_path, expected_files="other.py")

    exit_code = main(["validate", str(contract), "--repo", str(repo), "--approval", "interactive"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Status: WARNING" in output
    assert "unexpected_scope_change" in output


def test_validate_command_supports_json_and_governed_verification(capsys, tmp_path, approve_verification):
    repo = prepare_repo(tmp_path)
    verification = valid_v3_contract()["verification"]
    verification["claims"][0]["request"]["argv"] = [sys.executable, "-c", "import sys; sys.exit(4)"]
    contract = write_contract(
        tmp_path,
        verification=verification,
    )

    exit_code = main([
        "validate", str(contract), "--repo", str(repo), "--format", "json", "--approval", "interactive"
    ])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "FAIL"
    assert payload["verification"]["all_passed"] is False


@pytest.mark.parametrize("output_format", ("text", "json"))
@pytest.mark.parametrize(
    "verification_override, expected_findings",
    (
        pytest.param(
            {"claims": [], "coverage": []},
            (
                ("verification_required", "Contract verification must include at least one structured claim."),
                ("EXPECTED_CHANGE_UNVERIFIED", "Required Expected Change lacks direct claim coverage: app_updated"),
            ),
            id="missing-claims",
        ),
        pytest.param(
            {"coverage": []},
            (("EXPECTED_CHANGE_UNVERIFIED", "Required Expected Change lacks direct claim coverage: app_updated"),),
            id="required-change-missing-coverage",
        ),
        pytest.param(
            {"coverage": [{"claim_id": "app_check", "change_id": "app_updated", "coverage_type": "indirect"}]},
            (("EXPECTED_CHANGE_UNVERIFIED", "Required Expected Change lacks direct claim coverage: app_updated"),),
            id="required-change-indirect-only",
        ),
        pytest.param(
            {"assertions": [{"id": "changed", "change_id": "unknown_change", "assertion_type": "file_changed", "config": {"path": "app.py"}}]},
            (("assertion_change_missing", "Assertion references unknown Expected Change: unknown_change"),),
            id="assertion-unknown-change",
        ),
        pytest.param(
            {"assertions": [{"id": "changed", "change_id": "app_updated", "claim_id": "unknown_claim", "assertion_type": "file_changed", "config": {"path": "app.py"}}]},
            (("assertion_claim_missing", "Assertion references unknown claim: unknown_claim"),),
            id="file-changed-unknown-claim",
        ),
        pytest.param(
            {"assertions": [{"id": "exited", "change_id": "app_updated", "claim_id": "unknown_claim", "assertion_type": "exit_code", "config": {"expected_exit_code": 0}}]},
            (("assertion_claim_missing", "Assertion references unknown claim: unknown_claim"),),
            id="exit-code-unknown-claim",
        ),
    ),
)
def test_validate_rejects_invalid_contract_at_admission(
    capsys, tmp_path, approve_verification, output_format, verification_override, expected_findings
):
    repo = prepare_repo(tmp_path)
    payload = valid_v3_contract()
    payload["verification"].update(verification_override)
    contract = write_payload(tmp_path, payload)

    exit_code = main([
        "validate", str(contract), "--repo", str(repo), "--approval", "interactive", "--format", output_format,
    ])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    for code, message in expected_findings:
        assert code in captured.err
        assert message in captured.err


def test_admission_failure_never_executes_verification(capsys, tmp_path, approve_verification):
    repo = prepare_repo(tmp_path)
    payload = valid_v3_contract()
    payload["verification"]["coverage"] = []
    payload["verification"]["claims"][0]["request"]["argv"] = [
        sys.executable, "-c", "from pathlib import Path; Path('verification-ran.txt').write_text('ran')",
    ]
    contract = write_payload(tmp_path, payload)

    exit_code = main(["validate", str(contract), "--repo", str(repo), "--approval", "interactive"])

    assert exit_code == 2
    assert "EXPECTED_CHANGE_UNVERIFIED" in capsys.readouterr().err
    assert not (repo / "verification-ran.txt").exists()


def test_admission_failure_precedes_snapshot_and_assertions(capsys, tmp_path, monkeypatch):
    payload = valid_v3_contract()
    payload["verification"]["coverage"] = []
    payload["verification"]["assertions"] = [
        {"id": "changed", "change_id": "app_updated", "assertion_type": "file_changed", "config": {"path": "app.py"}},
    ]
    contract = write_payload(tmp_path, payload)

    def forbidden_call(*args, **kwargs):
        pytest.fail("Invalid Contract reached workspace/verification/assertion processing")

    monkeypatch.setattr("code_governance.cli.collect_workspace_snapshot", forbidden_call)
    monkeypatch.setattr("code_governance.cli.execute_verification_claims", forbidden_call)
    monkeypatch.setattr("code_governance.cli.AssertionEvaluatorRegistry.evaluate", forbidden_call)

    exit_code = main(["validate", str(contract), "--repo", str(tmp_path / "nonexistent-repo")])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "EXPECTED_CHANGE_UNVERIFIED" in captured.err


def test_validate_allows_planning_only_without_direct_coverage(capsys, tmp_path, approve_verification):
    repo = prepare_repo(tmp_path)
    payload = valid_v3_contract()
    payload["changes"][0]["requirement"] = "planning_only"
    payload["verification"]["coverage"] = []
    contract = write_payload(tmp_path, payload)

    exit_code = main(["validate", str(contract), "--repo", str(repo), "--approval", "interactive"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Status: PASS" in captured.out
    assert captured.err == ""


def test_admission_warning_allows_validation_to_continue(capsys, tmp_path, approve_verification):
    repo = prepare_repo(tmp_path)
    payload = valid_v3_contract()
    payload["scope"]["paths"] = ["app.py", "."]
    contract = write_payload(tmp_path, payload)
    admission = ContractValidator().validate(load_contract(contract))
    assert admission.is_valid
    assert [(finding.code, finding.severity) for finding in admission.findings] == [("scope_unusually_broad", "warning")]

    exit_code = main(["validate", str(contract), "--repo", str(repo), "--approval", "interactive"])

    assert exit_code == 0
    assert "Status: PASS" in capsys.readouterr().out


def legacy_v1_contract():
    return {
        "metadata": {},
        "intent": {"goal": "update app"},
        "expected": {"files": ["app.py"], "change_level": "L1"},
        "constraints": {},
        "verification": {"claims": valid_v3_contract()["verification"]["claims"]},
    }


@pytest.mark.parametrize("explicit_version", (False, True))
def test_validate_preserves_legal_v1_normalization_and_admission(
    capsys, tmp_path, approve_verification, explicit_version
):
    repo = prepare_repo(tmp_path)
    payload = legacy_v1_contract()
    if explicit_version:
        payload["version"] = 1
    contract_path = write_payload(tmp_path, payload)
    canonical = load_contract(contract_path)

    assert canonical.version == 3
    assert canonical.goal == "update app"
    assert canonical.changes[0].requirement == "planning_only"
    assert canonical.verification.claims[0].id == "app_check"
    assert canonical.verification.coverage == []
    assert ContractValidator().validate(canonical).is_valid

    exit_code = main(["validate", str(contract_path), "--repo", str(repo), "--approval", "interactive"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Status: PASS" in captured.out
    assert captured.err == ""


def test_validate_rejects_v1_empty_verification_after_normalization(capsys, tmp_path):
    payload = legacy_v1_contract()
    payload["verification"] = {}
    contract_path = write_payload(tmp_path, payload)
    assert load_contract(contract_path).version == 3

    exit_code = main(["validate", str(contract_path), "--repo", str(tmp_path / "nonexistent-repo")])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "verification_required" in captured.err
    assert "Contract verification must include at least one structured claim." in captured.err


def test_run_command_rejects_removed_runtime_loop_flag():
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["run", "task.md", "--runtime-loop"])

    assert error.value.code == 2


def test_run_command_exposes_explicit_fail_closed_approval_mode():
    args = build_parser().parse_args(["run", "task.md", "--approval", "deny"])

    assert args.approval == "deny"


@pytest.mark.parametrize("mode", ("baseline", "hardened"))
def test_benchmark_command_accepts_each_supported_mode(mode):
    args = build_parser().parse_args(
        ["benchmark", "--dataset", "benchmarks/v1/tasks.yaml", "--mode", mode]
    )

    assert args.dataset == Path("benchmarks/v1/tasks.yaml")
    assert args.mode == mode


@pytest.mark.parametrize(
    "arguments",
    (
        ["benchmark", "--dataset", "benchmarks/v1/tasks.yaml"],
        [
            "benchmark",
            "--dataset",
            "benchmarks/v1/tasks.yaml",
            "--mode",
            "unknown",
        ],
    ),
)
def test_benchmark_command_rejects_absent_or_unknown_mode(arguments):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(arguments)

    assert error.value.code == 2


def write_empty_dataset(tmp_path: Path) -> Path:
    dataset = tmp_path / "tasks.yaml"
    dataset.write_text("id: cli-test\ntasks: []\n", encoding="utf-8")
    return dataset


def assert_empty_benchmark_payload(payload, mode):
    assert payload["benchmark_version"] == "v1"
    assert payload["runtime_mode"] == mode
    assert payload["dataset"] == {"id": "cli-test", "task_count": 0}
    assert payload["trial_count"] == 0
    assert payload["trials"] == []
    assert payload["run"]["run_count"] == 1
    assert payload["run"]["started_at"]
    assert payload["limitations"]
    assert payload["failure_summary"] == {}
    assert payload["aggregate_metrics"]["success_rate"] == 0.0
    assert payload["aggregate_metrics"]["memory_hit_rate"] is None


def test_benchmark_command_writes_requested_json_output(capsys, tmp_path):
    dataset = write_empty_dataset(tmp_path)
    output = tmp_path / "report.json"

    exit_code = main(
        [
            "benchmark",
            "--dataset",
            str(dataset),
            "--mode",
            "baseline",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert_empty_benchmark_payload(
        json.loads(output.read_text(encoding="utf-8")), "baseline"
    )


def test_benchmark_command_prints_json_without_output_path(capsys, tmp_path):
    dataset = write_empty_dataset(tmp_path)

    exit_code = main(
        ["benchmark", "--dataset", str(dataset), "--mode", "hardened"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert_empty_benchmark_payload(json.loads(captured.out), "hardened")


def test_benchmark_command_returns_two_for_invalid_dataset(capsys, tmp_path):
    dataset = tmp_path / "invalid.yaml"
    dataset.write_text("tasks: not-a-list\n", encoding="utf-8")

    exit_code = main(
        ["benchmark", "--dataset", str(dataset), "--mode", "baseline"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "Error:" in captured.err


def test_benchmark_command_returns_two_for_unrecoverable_runner_setup_error(
    capsys, monkeypatch, tmp_path
):
    dataset = write_empty_dataset(tmp_path)

    class FailingRunner:
        def __init__(self, loaded_dataset):
            self.dataset = loaded_dataset

        def run(self, mode):
            raise RuntimeError("runner setup failed")

    monkeypatch.setattr("code_governance.cli.BenchmarkRunner", FailingRunner)

    exit_code = main(
        ["benchmark", "--dataset", str(dataset), "--mode", "baseline"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "Error: runner setup failed" in captured.err
