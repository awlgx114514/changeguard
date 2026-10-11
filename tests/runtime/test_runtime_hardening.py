import json

import pytest

from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.action_validator import ActionValidationError, ActionValidator
from code_governance.runtime.history import Issue, IssueStatus, WorkingMemoryItem
from code_governance.runtime.observation import Observation
from code_governance.runtime.state import AgentState


def test_state_persists_references_and_compact_working_state_only(tmp_path):
    state = AgentState.initialize(
        task_description="Update the channel",
        task_id="task-1",
        workspace_reference="workspace-1",
        plan_reference="plan-1",
        context_reference="context-1",
        run_id="run-1",
    )
    state.working_memory_summary.append(
        WorkingMemoryItem(
            key="read:channel.py",
            type="file_content",
            summary="Read the Channel interface",
            target="channel.py",
            importance=20,
        )
    )
    state.issues.append(
        Issue(
            id="issue-1",
            type="tool_error",
            description="missing file",
            status=IssueStatus.OPEN,
        )
    )
    state.save(tmp_path / "state.json")

    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    restored = AgentState.load(tmp_path / "state.json")

    assert "context" not in payload
    assert "current_plan" not in payload
    assert "actions" not in payload
    assert "observations" not in payload
    assert "working_memory" not in payload
    assert payload["task_description"] == "Update the channel"
    assert payload["workspace_reference"] == "workspace-1"
    assert payload["plan_reference"] == "plan-1"
    assert payload["context_reference"] == "context-1"
    assert restored.to_dict() == state.to_dict()


def test_state_uses_canonical_runtime_collections_without_legacy_aliases():
    state = AgentState.initialize(
        task_description="Inspect repository",
        workspace_reference="workspace-1",
    )
    action = Action.create_operation(OperationKind.FILE_READ, path="src/demo/core.py")
    observation = Observation(
        action_id=action.id,
        type="file_content",
        success=True,
        summary="Read src/demo/core.py",
    )

    state.record_action(action)
    state.record_observation(observation)

    assert state.recent_actions == [action]
    assert state.recent_observations == [observation]
    assert state.workspace_reference == "workspace-1"
    assert not hasattr(state, "actions")
    assert not hasattr(state, "observations")
    assert not hasattr(state, "working_memory")
    assert not hasattr(state, "workspace")
    assert not hasattr(state, "task")


def test_action_validator_rejects_actions_without_required_input():
    with pytest.raises(ActionValidationError, match="workspace-relative"):
        ActionValidator().validate(Action.create_operation(OperationKind.FILE_READ, reason="inspect"))

    with pytest.raises(ActionValidationError, match="argv"):
        ActionValidator().validate(Action.create_operation(OperationKind.COMMAND_RUN, reason="test"))


def test_observation_persists_only_compact_metadata_in_state(tmp_path):
    state = AgentState.initialize("Inspect repository")
    observation = Observation(
        action_id="action-1",
        type="file_content",
        success=True,
        summary="Read a source file",
        artifact_reference="src/demo/channel.py",
        metadata={"content_length": 10000},
    )
    state.record_observation(observation)
    state.save(tmp_path / "state.json")

    payload = (tmp_path / "state.json").read_text(encoding="utf-8")

    assert "content_length" in payload
    assert '"result":' not in payload
    assert "x" * 1000 not in payload


def test_history_issue_lifecycle_excludes_resolved_issue_from_active_view():
    state = AgentState.initialize("Inspect repository")
    issue = Issue(
        id="issue-1",
        type="tool_error",
        description="missing file",
        status=IssueStatus.OPEN,
    )
    state.issues.append(issue)

    state.resolve_issue("issue-1")

    assert state.open_issues == ()
    assert state.issues[0].status == IssueStatus.RESOLVED


def test_forbidden_action_never_triggers_a_direct_repair_path(tmp_path):
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
    from tests.agent.test_harness_v2 import prepare_repo
    from code_governance.tools import FilesystemTool, ShellTool, ToolRegistry

    class DecidingAgent:
        def __init__(self):
            self.actions = [
                Action.create_operation(OperationKind.FILE_CREATE, path="../forbidden.py", content="changed\n"),
                Action.create_operation(OperationKind.WORKFLOW_FINISH),
            ]

        def decide(self, state, context):
            return self.actions.pop(0)

    repo = prepare_repo(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Do not modify the core module", encoding="utf-8")
    contract = ChangeContract(
        metadata=Metadata(project="demo"),
        intent=Intent(goal="leave core unchanged"),
        expected=Expected(files=[], change_level="L1"),
        constraints=Constraints(forbidden=["forbidden.py"]),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )
    tool_registry = ToolRegistry()
    tool_registry.register(FilesystemTool())
    tool_registry.register(ShellTool())
    result = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: contract),
        coding_agent=DecidingAgent(),
        tool_registry=tool_registry,
        runtime_loop=True,
    ).run(task_file, repo)

    trace = json.loads(next((repo / ".agent" / "runs").glob("*/trace.json")).read_text(encoding="utf-8"))

    assert result.success is True
    assert result.repairs == 0
    assert trace["schema_version"] == "v2"
    assert [event["type"] for event in trace["events"]].count("agent_loop_started") == 1
