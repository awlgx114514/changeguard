from pathlib import Path

import pytest

from code_governance.evaluation.phase4d_readiness import (
    prepare_readiness_checkpoint,
    run_deterministic_readiness,
)
from code_governance.evaluation.phase4d_checkpoint import FailureCheckpoint
from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.evaluation.runner import BenchmarkRunner
from code_governance.runtime.action import Action, OperationKind
from code_governance.tools import ApprovalOutcome
from code_governance.workspace.manager import WorkspaceManager
from code_governance.contract.schema import ChangeContract


class _OrdinaryAgent:
    def __init__(self):
        self.actions = [
            Action.create_operation(OperationKind.FILE_CREATE, path="result.txt", content="generated\n"),
            Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="ordinary run complete"),
        ]

    def decide(self, _state, _context):
        return self.actions.pop(0)


class _CountingDecisionAgent:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def decide(self, _state, _context):
        self.calls += 1
        if self.fail:
            raise RuntimeError("diagnostic decision sentinel")
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="stop for admission test")


class _CountingWorkspaceManager(WorkspaceManager):
    def __init__(self):
        super().__init__()
        self.restore_calls = 0

    def restore(self, repo_path, snapshot_path):
        self.restore_calls += 1
        return super().restore(repo_path, snapshot_path)


class _RepairTwiceAgent:
    def __init__(self):
        self.calls = 0

    def decide(self, state, _context):
        self.calls += 1
        if self.calls in {1, 4}:
            return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="end this validation attempt")
        if self.calls in {2, 3}:
            if self.calls == 2:
                return Action.create_operation(OperationKind.FILE_READ, path="src/auth.py", reason="inspect current implementation")
            previous = next(
                item for item in reversed(state.recent_observations)
                if item.action_id == state.recent_actions[-1].id
            )
            content = previous.metadata["content_preview"]
            replacement = 'def check_password(password):\n    return bool(password) and password == "s3cure-pass"\n'
            return Action.create_operation(
                OperationKind.FILE_REPLACE,
                path="src/auth.py",
                expected_text=content,
                replacement=replacement,
                observed_file_hash=previous.metadata["file_hash"],
                reason="reject empty passwords",
            )
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="finish repaired validation")


class _FailOnFourthDecisionAgent:
    def __init__(self, path):
        self.path = path
        self.calls = 0

    def decide(self, state, context):
        self.calls += 1
        if self.calls == 4:
            raise RuntimeError("same decision failure")
        if self.calls == 1:
            return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="force first validation")
        if self.calls == 2:
            return Action.create_operation(OperationKind.FILE_READ, path=self.path, reason="inspect implementation")
        if self.calls == 3:
            observation = state.recent_observations[-1]
            return Action.create_operation(
                OperationKind.FILE_REPLACE,
                path=self.path,
                expected_text=observation.metadata["content_preview"],
                replacement=observation.metadata["content_preview"],
                observed_file_hash=observation.metadata["file_hash"],
                reason="retain behavior while isolating decision failure",
            )
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="unexpected extra decision")


class _ApproveAll:
    def request(self, _request):
        return ApprovalOutcome.APPROVED


def _reseal_contract(checkpoint, mutate):
    payload = checkpoint.to_dict()
    contract = payload["contract"]
    mutate(contract)
    payload["agent_state"]["contract"] = contract
    payload["context"]["contract"] = contract
    return FailureCheckpoint.create(payload)


def _repair_start(checkpoint, output, *, agent, workspace_manager=None, approval_provider=None):
    repository = Path(checkpoint.payload["artifact_root"]) / "p" / "repo"
    return HarnessOrchestrator(
        coding_agent=agent,
        workspace_manager=workspace_manager or WorkspaceManager(),
        approval_provider=approval_provider,
        runtime_max_steps=int(checkpoint.payload["budgets"]["steps_total"]),
        max_repairs=int(checkpoint.payload["budgets"]["repairs_total"]),
    ).repair_start(
        checkpoint,
        repo_path=repository,
        trial_id="trial:admission-test",
        run_id="run-admission-test",
        include_repair_context=True,
        final_workspace_path=output,
    )


def test_readiness_creates_two_real_failure_checkpoints_and_four_repair_forks(tmp_path):
    report = run_deterministic_readiness(tmp_path / "readiness")

    assert report["readiness"] == "PASS"
    assert report["task_count"] >= 2
    assert report["checkpoint_count"] == 2
    assert report["trial_count"] == 4
    assert all(check["passed"] for check in report["checks"].values())
    assert report["false_success_count"] == 0
    assert report["provider_call_count"] == 0
    assert report["canonical_action_path_preserved"] is True
    assert report["runtime_core_changed"] is False
    assert all(item["steps_consumed_at_end"] > item["steps_consumed_at_start"] for item in report["trials"])
    assert all(item["steps_consumed_at_end"] <= item["steps_total"] for item in report["trials"])
    assert all(item["repairs_consumed_at_end"] == 1 for item in report["trials"])


def test_ordinary_harness_run_keeps_existing_canonical_behavior(evaluation_fixture, tmp_path):
    task = evaluation_fixture.make_task()
    repository = tmp_path / "ordinary" / "repo"
    repository.parent.mkdir(parents=True)
    import shutil
    shutil.copytree(evaluation_fixture.snapshot, repository)
    BenchmarkRunner._initialize_repository(repository)
    task_file = tmp_path / "ordinary" / "task.md"
    task_file.write_text(task.description, encoding="utf-8")
    approval = type("Approved", (), {"request": lambda self, _request: ApprovalOutcome.APPROVED})()

    result = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda _task, _repo: task.to_contract()),
        coding_agent=_OrdinaryAgent(),
        approval_provider=approval,
        max_repairs=2,
        runtime_max_steps=10,
    ).run(task_file, repository)

    assert result.success is True
    assert result.repairs == 0
    assert len(result.validation_attempts) == 1


def test_repair_start_admits_valid_contract_and_existing_warning_semantics(tmp_path, monkeypatch):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "valid")
    # Isolate this test at the ContractValidator boundary. Producer trajectory
    # binding has dedicated adversarial coverage in test_phase4d_checkpoint_round3.py.
    monkeypatch.setattr(type(checkpoint), "_validate_producer_binding", lambda *_args: None)
    admitted_with_warning = _reseal_contract(
        checkpoint,
        lambda contract: contract["scope"].update(paths=["repository"]),
    )
    agent = _CountingDecisionAgent(fail=True)
    result = _repair_start(admitted_with_warning, tmp_path / "valid-final", agent=agent)

    assert agent.calls == 1
    assert result.harness_result.success is False
    assert result.harness_result.failure.category == "DECISION_ERROR"
    assert result.harness_result.termination_reason == "DECISION_ERROR"


def test_normal_harness_repair_and_repair_start_attribute_same_decision_error(tmp_path):
    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "source")
    repository = Path(checkpoint.payload["artifact_root"]) / "p" / "repo"
    contract = ChangeContract.model_validate(checkpoint.payload["contract"])
    task_file = tmp_path / "normal" / "task.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text(checkpoint.payload["task"]["description"], encoding="utf-8")
    normal = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda _task, _repo: contract),
        coding_agent=_FailOnFourthDecisionAgent("src/auth.py"),
        approval_provider=_ApproveAll(),
        max_repairs=2,
        runtime_max_steps=10,
    ).run(task_file, repository)

    resumed = _repair_start(
        checkpoint, tmp_path / "resume-final", agent=_CountingDecisionAgent(fail=True),
        approval_provider=_ApproveAll(),
    ).harness_result

    assert (normal.failure.stage, normal.failure.category, normal.termination_reason) == (
        resumed.failure.stage, resumed.failure.category, resumed.termination_reason,
    ) and normal.failure.category == resumed.failure.category == "DECISION_ERROR", (
        (normal.failure.stage, normal.failure.category, normal.termination_reason),
        (resumed.failure.stage, resumed.failure.category, resumed.termination_reason),
    )


def test_later_validation_attempt_action_ids_exclude_prefix_and_prior_repair_actions(tmp_path):
    from code_governance.evaluation.phase4d_readiness import prepare_readiness_checkpoint

    checkpoint = prepare_readiness_checkpoint("behavior_repair", tmp_path / "multi-repair")
    result = _repair_start(
        checkpoint,
        tmp_path / "multi-repair-final",
        agent=_RepairTwiceAgent(),
        approval_provider=_ApproveAll(),
    ).harness_result

    assert len(result.validation_attempts) == 2, repr(result)
    prefix_ids = set(checkpoint.payload["validation_attempt"]["action_ids"])
    first_ids = set(result.validation_attempts[0].action_ids)
    second_ids = set(result.validation_attempts[1].action_ids)
    assert len(first_ids) == 1
    assert len(second_ids) == 3
    assert not (prefix_ids & first_ids)
    assert not (prefix_ids & second_ids)
    assert not (first_ids & second_ids)
    assert result.success is True


@pytest.mark.parametrize(
    "mutate,expected_code",
    [
        (lambda contract: contract["verification"].update(coverage=[]), "EXPECTED_CHANGE_UNVERIFIED"),
        (lambda contract: contract["verification"]["assertions"][0].update(claim_id="missing_claim"), "assertion_claim_missing"),
    ],
)
def test_repair_start_rejects_semantically_invalid_checkpoint_before_decision_or_execution(
    tmp_path, monkeypatch, mutate, expected_code,
):
    original = prepare_readiness_checkpoint("behavior_repair", tmp_path / "invalid")
    # This test targets semantic contract admission after checkpoint structure
    # validation; K1/K2 producer identity is independently exercised elsewhere.
    monkeypatch.setattr(type(original), "_validate_producer_binding", lambda *_args: None)
    invalid = _reseal_contract(original, mutate)
    invalid.validate()
    initial_evidence = invalid.payload["validation_attempt"]
    agent = _CountingDecisionAgent()
    manager = _CountingWorkspaceManager()

    def unexpected_execution(*_args, **_kwargs):
        pytest.fail("invalid checkpoint reached Tool execution or verification")

    monkeypatch.setattr(HarnessOrchestrator, "_run_runtime_loop", unexpected_execution)
    monkeypatch.setattr(HarnessOrchestrator, "_validate", unexpected_execution)

    with pytest.raises(ValueError, match=expected_code):
        _repair_start(
            invalid, tmp_path / "invalid-final", agent=agent, workspace_manager=manager,
        )

    assert agent.calls == 0
    assert manager.restore_calls == 0
    assert not (tmp_path / "invalid-final").exists()
    assert invalid.payload["validation_attempt"] == initial_evidence
    assert invalid.validate() is None


def test_valid_checkpoint_fingerprint_does_not_bypass_contract_admission(tmp_path, monkeypatch):
    valid = prepare_readiness_checkpoint("behavior_repair", tmp_path / "signed-invalid")
    monkeypatch.setattr(type(valid), "_validate_producer_binding", lambda *_args: None)
    invalid = _reseal_contract(
        valid,
        lambda contract: contract["verification"].update(coverage=[]),
    )
    assert invalid.fingerprint != valid.fingerprint
    invalid.validate()

    agent = _CountingDecisionAgent()
    with pytest.raises(ValueError, match="EXPECTED_CHANGE_UNVERIFIED"):
        _repair_start(invalid, tmp_path / "signed-invalid-final", agent=agent)
    assert agent.calls == 0
