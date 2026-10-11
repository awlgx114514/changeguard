import json
import hashlib
from pathlib import Path
import subprocess

import pytest

from code_governance.agent.base import CodingAgentResult
from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.contract.schema import (
    ChangeContract,
    Constraints,
    Expected,
    Intent,
    Metadata,
    Verification,
    VerificationClaim,
)
from code_governance.evaluation.profiles import (
    BaselineActionValidator,
    BaselineHistoryManager,
    CompactTraceManager,
    RuntimeProfile,
)
from code_governance.runtime.action import Action, ActionType, OperationKind
from code_governance.runtime.action_validator import ActionValidationError, ActionValidator
from code_governance.runtime.history import HistoryManager
from code_governance.runtime.observation import Observation
from code_governance.runtime.state import AgentState
from code_governance.tools.approval import ApprovalOutcome
from code_governance.tools.policy import ToolPolicy
from code_governance.trace.writer import TraceWriter


@pytest.mark.parametrize(
    ("mode", "has_policy", "validator_type", "history_type", "runtime_repair"),
    (
        ("baseline", False, BaselineActionValidator, BaselineHistoryManager, False),
        ("hardened", True, ActionValidator, HistoryManager, True),
    ),
)
def test_runtime_profiles_select_their_dependency_sets(
    mode, has_policy, validator_type, history_type, runtime_repair
):
    profile = RuntimeProfile.for_mode(mode)
    registry = profile.build_tool_registry()

    assert profile.runtime_repair is runtime_repair
    assert registry.list_tools() == ("filesystem", "git", "shell")
    assert isinstance(registry._policy, ToolPolicy) is has_policy
    assert isinstance(profile.action_validator(), validator_type)
    assert isinstance(profile.history_manager(), history_type)


def test_baseline_validator_requires_only_an_action_instance():
    validator = RuntimeProfile.for_mode("baseline").action_validator()

    validator.validate(Action("", ActionType.READ))

    with pytest.raises(ActionValidationError, match="Action"):
        validator.validate(object())


def test_baseline_profile_keeps_working_memory_without_issue_lifecycle(tmp_path):
    profile = RuntimeProfile.for_mode("baseline")
    state = AgentState.initialize("task", {}, {})
    action = Action.create(ActionType.READ, target="missing.py", reason="inspect")
    observation = Observation(action.id, type="file_content", success=False, error="missing")

    history = profile.history_manager().record(state, action, observation)

    assert state.working_memory_summary
    assert state.issues == []
    assert state.decisions == []
    assert history == {"working_items": 1, "active_issues": 0}


@pytest.mark.parametrize(
    ("mode", "manager_type", "expected_data"),
    (
        (
            "baseline",
            CompactTraceManager,
            {
                "action_id": "action-1",
                    "operation": "file.write",
                "target": "notes.txt",
                "tool": "filesystem",
                "success": True,
            },
        ),
        (
            "hardened",
            None,
            {
                "action_id": "action-1",
                    "operation": "file.write",
                "target": "notes.txt",
                "tool": "filesystem",
                "success": True,
                "agent": "DeterministicAgent",
                "reason": "write private source text",
            },
        ),
    ),
)
def test_profile_trace_handling_distinguishes_compact_and_hardened_metadata(
    tmp_path, mode, manager_type, expected_data
):
    manager = RuntimeProfile.for_mode(mode).trace_manager("run-1", TraceWriter(tmp_path))
    manager.emit(
        "action_executed",
        {
            "action_id": "action-1",
                "operation": "file.write",
            "target": "notes.txt",
            "tool": "filesystem",
            "success": True,
            "agent": "DeterministicAgent",
            "reason": "write private source text",
        },
    )

    assert isinstance(manager, manager_type) if manager_type else not isinstance(manager, CompactTraceManager)
    assert manager.get_events()[0].data == expected_data


def test_unknown_runtime_mode_is_rejected():
    with pytest.raises(ValueError, match="^unsupported runtime mode: unknown$"):
        RuntimeProfile.for_mode("unknown")


class ProfileAgent:
    def __init__(self, actions):
        self._actions = list(actions)
        self.repair_calls = 0

    def decide(self, state, context):
        return self._actions.pop(0)

    def run(self, task, contract, plan, workspace, context):
        self.repair_calls += 1
        return CodingAgentResult(stdout="basic repair completed", stderr="", returncode=0)


class ApprovedApprovalProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


class StateRecordingHarness(HarnessOrchestrator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.runtime_transitions = []

    def _transition_runtime_state(self, state, status, trace):
        super()._transition_runtime_state(state, status, trace)
        self.runtime_transitions.append(AgentState.from_dict(state.to_dict()))


def profile_contract(expected_files=(), forbidden=()):
    return ChangeContract(
        metadata=Metadata(project="demo"),
        intent=Intent(goal="exercise evaluation runtime profile"),
        expected=Expected(files=list(expected_files), change_level="L1"),
        constraints=Constraints(forbidden=list(forbidden)),
        verification=Verification(
            claims=[
                VerificationClaim(
                    id="diff_check",
                    description="The diff is valid.",
                    request={"argv": ("git", "diff", "--check")},
                )
            ]
        ),
    )


def prepare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    package = repo / "src" / "demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    for command in (
        ("init",),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test User"),
        ("add", "."),
        ("commit", "-m", "initial"),
    ):
        subprocess.run(
            ("git", *command),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    return repo


def profile_task(tmp_path):
    task_file = tmp_path / "profile-task.md"
    task_file.write_text("Exercise the evaluation runtime profile", encoding="utf-8")
    return task_file


@pytest.mark.parametrize("mode", ("baseline", "hardened"))
def test_profile_trace_written_through_harness_uses_its_persisted_format(tmp_path, mode):
    profile = RuntimeProfile.for_mode(mode)
    repo = prepare_repo(tmp_path)
    tool_registry = profile.build_tool_registry()
    if mode == "hardened":
        tool_registry._approval_provider = ApprovedApprovalProvider()
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(
            generator=lambda task, context: profile_contract(("src/demo/core.py",))
        ),
            coding_agent=ProfileAgent(
                (
                    Action.create_operation(OperationKind.FILE_READ, path="src/demo/core.py", reason="inspect core"),
                    Action.create_operation(
                    OperationKind.FILE_WRITE,
                    path="src/demo/core.py",
                    content="VALUE = 2\n",
                        observed_file_hash=hashlib.sha256((repo / "src/demo/core.py").read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
                    reason="update core",
                ),
                Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete"),
            )
        ),
        tool_registry=tool_registry,
        runtime_loop=True,
        history_manager_factory=profile.history_manager,
        trace_manager_factory=profile.trace_manager,
        action_validator_factory=profile.action_validator,
        runtime_repair=profile.runtime_repair,
        runtime_issue_lifecycle=profile.runtime_issue_lifecycle,
    )

    result = harness.run(profile_task(tmp_path), repo)
    payload = json.loads(
        next((repo / ".agent" / "runs").glob("*/trace.json")).read_text(encoding="utf-8")
    )

    assert result.success is True
    assert "VALUE = 2" in result.patch_path.read_text(encoding="utf-8")
    assert result.report.verification.commands[0].command == "git diff --check"
    assert result.report.verification.commands[0].returncode == 0
    assert any(
        event["type"] == "tool_called"
        and event["data"].get("action_id") == "verification:diff_check"
        for event in payload["events"]
    )
    if mode == "baseline":
        assert set(payload) == {"run_id", "schema_version", "events"}
        assert payload["schema_version"] == "v1"
        assert all(
            set(event) == {"type", "data"}
            and
            set(event["data"]) <= {"action_id", "operation", "target", "tool", "success"}
            for event in payload["events"]
        )
    else:
        assert payload["schema_version"] == "v2"
        assert payload["context_version"] == "v1"
        assert all(
            set(event) == {"type", "data", "sequence", "timestamp"}
            for event in payload["events"]
        )


@pytest.mark.parametrize(
    ("mode", "expected_issue_lifecycle"),
    (("baseline", False), ("hardened", True)),
)
def test_profile_repair_uses_its_issue_lifecycle_setting(tmp_path, mode, expected_issue_lifecycle):
    profile = RuntimeProfile.for_mode(mode)
    repo = prepare_repo(tmp_path)
    agent = ProfileAgent(
        (
                Action.create_operation(OperationKind.FILE_CREATE, path="forbidden.py", content="changed\n", reason="trigger a validator failure"),
                Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete initial run"),
                Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete runtime repair"),
        )
    )
    tool_registry = profile.build_tool_registry()
    if mode == "hardened":
        tool_registry._approval_provider = ApprovedApprovalProvider()
    harness = StateRecordingHarness(
        contract_agent=ContractAgent(
            generator=lambda task, context: profile_contract(forbidden=("forbidden.py",))
        ),
        coding_agent=agent,
        tool_registry=tool_registry,
        max_repairs=1,
        runtime_loop=True,
        history_manager_factory=profile.history_manager,
        trace_manager_factory=profile.trace_manager,
        action_validator_factory=profile.action_validator,
        runtime_repair=profile.runtime_repair,
        runtime_issue_lifecycle=profile.runtime_issue_lifecycle,
    )

    result = harness.run(profile_task(tmp_path), repo)
    repair_transitions = [
        state for state in harness.runtime_transitions if state.status == "REPAIRING"
    ]

    assert result.success is False
    assert result.repairs == (1 if profile.runtime_repair else 0)
    assert bool(repair_transitions) is expected_issue_lifecycle
    if expected_issue_lifecycle:
        assert repair_transitions[0].issues
    else:
        assert all(not state.issues for state in harness.runtime_transitions)
        assert agent.repair_calls == 0
