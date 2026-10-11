import sys

from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.legacy_action_v1 import ActionType
from code_governance.tools import (
    ApprovalOutcome,
    FilesystemTool,
    ShellTool,
    ToolRegistry,
)
from code_governance.tools.policy import PolicyDecision, PolicyResult, ToolPolicy, ToolRuntimeConfig
from code_governance.trace.writer import TraceManager


class FixedApprovalProvider:
    def __init__(self, outcome):
        self._outcome = outcome
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return self._outcome


class AskPolicy:
    def decide(self, action, tool_metadata, workspace):
        return PolicyResult(
            PolicyDecision.ASK,
            tool_name=tool_metadata["name"],
            canonical_target=str(action.operation.fields.get("path", action.operation.fields.get("name_query", ""))),
            category="WRITE",
        )


class RecordingFilesystemTool:
    name = "filesystem"
    description = "Records whether a filesystem invocation occurs."
    input_schema = {}

    def __init__(self):
        self.calls = 0

    def execute(self, payload, workspace):
        self.calls += 1
        return {"path": str(payload["path"]), "written": 0, "state_changed": False}


def registry_with_governance(provider, *, policy=None, trace_manager=None):
    result = ToolRegistry(
        trace_manager=trace_manager,
        policy=policy or ToolPolicy(),
        approval_provider=provider,
    )
    result.register(FilesystemTool())
    result.register(ShellTool())
    return result


def test_policy_allows_current_read_only_tools(tmp_path):
    result = ToolPolicy().decide(
        Action.create(ActionType.READ, target="notes.txt"),
        {"name": "filesystem"}, tmp_path,
    )
    assert result.decision is PolicyDecision.ALLOW


def test_policy_asks_before_workspace_write(tmp_path):
    result = ToolPolicy().decide(
        Action.create(ActionType.WRITE, target="notes.txt", input={"content": "x"}),
        {"name": "filesystem"}, tmp_path,
    )
    assert result.decision is PolicyDecision.ASK


def test_policy_denies_disallowed_tool_without_spending_budget(tmp_path):
    policy = ToolPolicy(ToolRuntimeConfig(allowed_tools=("git",), max_tool_calls=1))
    result = policy.decide(Action.create(ActionType.READ, target="x"), {"name": "filesystem"}, tmp_path)
    assert result.decision is PolicyDecision.DENY
    assert "not permitted" in result.reason


def test_policy_allows_empty_filesystem_target_for_tool_validation(tmp_path):
    result = ToolPolicy().decide(
        Action.create(ActionType.READ, target=""),
        {"name": "filesystem"}, tmp_path,
    )
    assert result.decision is PolicyDecision.ALLOW


def registry(policy):
    result = ToolRegistry(policy=policy)
    result.register(FilesystemTool())
    result.register(ShellTool())
    return result


def test_policy_rejects_a_second_tool_call_after_runtime_limit(tmp_path):
    tools = registry(ToolPolicy(ToolRuntimeConfig(max_tool_calls=1)))
    action = Action.create(ActionType.WRITE, "notes.txt", {"content": "one"})

    tools.execute(action, tmp_path)

    result = tools.execute(Action.create(ActionType.READ, "notes.txt"), tmp_path)

    assert result.status == "FAILURE"
    assert result.failure.stage == "PRE_EXECUTION_GOVERNANCE"
    assert result.failure.category == "POLICY_DENY"


def test_policy_rejects_targets_outside_the_workspace(tmp_path):
    tools = registry(ToolPolicy())

    result = tools.execute(Action.create(ActionType.READ, "..\\outside.txt"), tmp_path)

    assert result.status == "FAILURE"
    assert result.failure.stage == "PRE_EXECUTION_GOVERNANCE"
    assert result.failure.category == "POLICY_DENY"


def test_policy_timeout_is_applied_to_shell_execution(tmp_path):
    tools = registry_with_governance(
        FixedApprovalProvider(ApprovalOutcome.APPROVED),
        policy=ToolPolicy(ToolRuntimeConfig(shell_timeout_seconds=0.01)),
    )
    action = Action.create(
        ActionType.RUN_COMMAND,
        input={"command": [sys.executable, "-c", "import time; time.sleep(1)"]},
    )

    result = tools.execute(action, tmp_path)

    assert result.status == "FAILURE"
    assert result.failure.stage == "TOOL_EXECUTION"
    assert result.failure.category == "TOOL_TIMEOUT"


def test_successful_shell_output_is_available_to_the_next_model_decision(tmp_path):
    tools = registry_with_governance(FixedApprovalProvider(ApprovalOutcome.APPROVED))
    action = Action.create(
        ActionType.RUN_COMMAND,
        input={"command": [sys.executable, "-c", "print('LOCATED_PROCESS_IMPLEMENTATION')"]},
    )

    result = tools.execute(action, tmp_path)

    assert result.success is True
    assert "LOCATED_PROCESS_IMPLEMENTATION" in result.summary


def test_ask_approved_executes_after_hard_guard_passes(tmp_path):
    registry = registry_with_governance(FixedApprovalProvider(ApprovalOutcome.APPROVED))

    result = registry.execute(Action.create_operation(OperationKind.FILE_CREATE, path="notes.txt", content="ok"), tmp_path)

    assert result.success is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "ok"


def test_ask_rejected_does_not_execute_tool(tmp_path):
    result = registry_with_governance(FixedApprovalProvider(ApprovalOutcome.REJECTED)).execute(
        Action.create(ActionType.WRITE, "notes.txt", {"content": "blocked"}), tmp_path
    )

    assert result.failure.stage == "PRE_EXECUTION_GOVERNANCE"
    assert result.failure.category == "APPROVAL_REJECTED"
    assert not (tmp_path / "notes.txt").exists()


def test_ask_without_provider_fails_closed(tmp_path):
    result = registry_with_governance(None).execute(
        Action.create(ActionType.WRITE, "notes.txt", {"content": "blocked"}), tmp_path
    )

    assert result.failure.category == "APPROVAL_UNAVAILABLE"
    assert not (tmp_path / "notes.txt").exists()


def test_registry_without_policy_preserves_direct_write_execution(tmp_path):
    tools = ToolRegistry()
    tools.register(FilesystemTool())

    result = tools.execute(
        Action.create_operation(OperationKind.FILE_CREATE, path="notes.txt", content="explicit"),
        tmp_path,
    )

    assert result.success is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "explicit"


def test_approval_cannot_bypass_workspace_hard_guard(tmp_path):
    result = registry_with_governance(
        FixedApprovalProvider(ApprovalOutcome.APPROVED), policy=AskPolicy()
    ).execute(Action.create_operation(OperationKind.FILE_CREATE, path="..\\outside.txt", content="blocked"), tmp_path)

    assert result.failure.category == "HARD_GUARD_DENY"
    assert not (tmp_path.parent / "outside.txt").exists()


def test_empty_injected_guards_cannot_bypass_workspace_containment_after_approval(tmp_path):
    tool = RecordingFilesystemTool()
    registry = ToolRegistry(
        policy=AskPolicy(),
        approval_provider=FixedApprovalProvider(ApprovalOutcome.APPROVED),
        hard_guards=(),
    )
    registry.register(tool)

    result = registry.execute(
        Action.create_operation(OperationKind.FILE_CREATE, path="..\\outside.txt", content="blocked"),
        tmp_path,
    )

    assert result.failure.category == "HARD_GUARD_DENY"
    assert tool.calls == 0


def test_ask_rejection_traces_the_decision_and_never_calls_the_tool(tmp_path):
    trace = TraceManager("approval-rejection")
    registry = registry_with_governance(
        FixedApprovalProvider(ApprovalOutcome.REJECTED), trace_manager=trace
    )
    action = Action.create(ActionType.WRITE, "notes.txt", {"content": "blocked"})

    registry.execute(action, tmp_path)

    events = trace.get_events()
    assert [event.type for event in events] == [
        "tool_policy_decided",
        "approval_requested",
        "approval_resolved",
    ]
    assert {event.data["action_id"] for event in events} == {action.id}
    assert "tool_called" not in [event.type for event in events]


def test_policy_deny_never_requests_approval(tmp_path):
    trace = TraceManager("policy-deny")
    registry = registry_with_governance(
        FixedApprovalProvider(ApprovalOutcome.APPROVED),
        policy=ToolPolicy(ToolRuntimeConfig(allowed_tools=("git",))),
        trace_manager=trace,
    )

    result = registry.execute(Action.create(ActionType.READ, "notes.txt"), tmp_path)

    assert result.failure.category == "POLICY_DENY"
    assert [event.type for event in trace.get_events()] == ["tool_policy_decided"]


def test_governance_trace_normalizes_long_action_and_parent_ids_consistently(tmp_path):
    trace = TraceManager("long-governance-ids")
    provider = FixedApprovalProvider(ApprovalOutcome.APPROVED)
    registry = registry_with_governance(provider, trace_manager=trace)
    action = Action.create_operation(OperationKind.FILE_CREATE, path="notes.txt", content="ok")
    action = Action(id="a" * 600, operation=action.operation)
    parent_action_id = "p" * 600

    result = registry.execute(action, tmp_path, parent_action_id=parent_action_id)

    assert result.success is True
    events = [
        event for event in trace.get_events()
        if event.type in {
            "tool_policy_decided",
            "approval_requested",
            "approval_resolved",
            "hard_guard_evaluated",
        }
    ]
    assert len(events) == 4
    assert {event.data["action_id"] for event in events} == {provider.requests[0].action_id}
    assert len(provider.requests[0].action_id) <= 512
    assert len({event.data["parent_action_id"] for event in events}) == 1
    assert len(events[0].data["parent_action_id"]) <= 512
