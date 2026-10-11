import pytest

from code_governance.runtime.action import Action, ActionType, OperationKind
from code_governance.runtime.action_validator import ActionValidationError, ActionValidator
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.registry import ToolRegistry
from code_governance.runtime.loop import AgentLoop
from code_governance.runtime.state import AgentState
from code_governance.trace.writer import TraceManager
from code_governance.tools.approval import ApprovalOutcome
from code_governance.tools.policy import ToolPolicy
from code_governance.tools.shell import ShellTool
import sys


def test_transport_schema_is_flattened_but_canonical_action_has_one_operation_kind():
    schema = Action.model_schema()

    assert "oneOf" not in schema
    assert "allOf" not in schema
    assert "operation" in schema["properties"]
    assert set(schema["required"]) == set(schema["properties"])

    action = Action.from_dict(
        {
            "id": "find-1",
            "operation": "file.find",
            "name_query": "harness",
            "reason": "discover the relevant file",
        }
    )

    assert action.operation.kind is OperationKind.FILE_FIND
    assert action.operation.fields == {"name_query": "harness"}


def test_operation_catalog_exposes_provider_arguments_and_example_for_context():
    search = next(item for item in Action.operation_descriptions() if item["operation"] == "file.search")

    assert search["provider_arguments"] == (
        {"name": "query", "canonical_field": "query", "type": "string", "required": True},
        {"name": "path", "canonical_field": "path", "type": "string", "required": False},
    )
    assert search["provider_example"] == {
        "operation": "file.search",
        "arguments": {"query": "text", "path": None},
        "reason": "search workspace content",
    }


def test_operation_catalog_exposes_unambiguous_selection_guidance():
    catalog = {item["operation"]: item for item in Action.operation_descriptions()}

    assert "Find the file named action.py" in catalog["file.find"]["examples"]
    assert "Find where ActionValidator is defined" in catalog["file.search"]["examples"]
    assert "Inspect src/runtime/action.py around the Action class" in catalog["file.read"]["examples"]
    assert "Change this exact function implementation" in catalog["file.replace"]["examples"]
    assert "locating class/function definitions" in catalog["file.find"]["when_not_to_use"]
    assert "known file path" in catalog["file.read"]["when_to_use"]
    assert "file.search → file.read" in catalog["file.search"]["transition_hints"]


def test_legacy_search_shape_normalizes_once_to_file_find():
    action = Action.create(
        ActionType.READ,
        "harness",
        input={"action": "search_file"},
    )

    assert action.operation.kind is OperationKind.FILE_FIND
    assert action.operation.fields == {"name_query": "harness"}


def test_validator_rejects_absolute_v2_file_path_at_action_boundary():
    action = Action.from_dict(
        {
            "id": "read-1",
            "operation": "file.read",
            "path": "C:/outside.txt",
            "reason": "read source",
        }
    )

    with pytest.raises(ActionValidationError, match="workspace-relative"):
        ActionValidator().validate(action)


def test_validator_allows_workspace_wide_file_search_without_path():
    action = Action.from_dict(
        {
            "id": "search-1",
            "operation": "file.search",
            "query": "needle",
            "reason": "search all workspace files",
        }
    )

    ActionValidator().validate(action)


def test_file_find_routes_name_query_without_resolving_it_as_a_path(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "harness.py").write_text("pass\n", encoding="utf-8")
    action = Action.from_dict(
        {"id": "find-2", "operation": "file.find", "name_query": "harness", "reason": "discover"}
    )
    registry = ToolRegistry()
    registry.register(FilesystemTool())

    result = registry.execute(action, tmp_path)

    assert result.success is True
    assert result.metadata["paths"] == ["src/harness.py"]


def test_partial_read_can_authorize_exact_local_replace(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("before\nneedle\nafter\n", encoding="utf-8")

    class Agent:
        def __init__(self):
            self.actions = [
                Action.create_operation(OperationKind.FILE_READ, path="notes.txt", start_line=1, line_count=2),
                Action.create_operation(
                    OperationKind.FILE_REPLACE,
                    path="notes.txt",
                    expected_text="needle",
                    replacement="changed",
                    observed_file_hash="placeholder",
                ),
            ]

        def decide(self, state, context):
            action = self.actions.pop(0)
            if action.operation.kind is OperationKind.FILE_REPLACE:
                read = state.recent_observations[-1]
                return Action.create_operation(
                    OperationKind.FILE_REPLACE,
                    path="notes.txt",
                    expected_text="needle",
                    replacement="changed",
                    observed_file_hash=read.metadata["file_hash"],
                )
            return action

    registry = ToolRegistry()
    registry.register(FilesystemTool())
    state = AgentLoop(Agent(), registry, TraceManager("replace-v2"), max_steps=2).run(
        AgentState.initialize("replace observed text"), {}, tmp_path
    )

    assert target.read_text(encoding="utf-8") == "before\nchanged\nafter\n"
    assert state.recent_observations[-1].success is True


def test_reread_after_replace_replaces_stale_file_evidence(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("before\nneedle\n", encoding="utf-8")

    class Agent:
        def __init__(self):
            self.step = 0

        def decide(self, state, context):
            self.step += 1
            if self.step == 1:
                return Action.create_operation(OperationKind.FILE_READ, path="notes.txt")
            if self.step == 2:
                return Action.create_operation(
                    OperationKind.FILE_REPLACE,
                    path="notes.txt",
                    expected_text="needle",
                    replacement="changed",
                    observed_file_hash=state.recent_observations[-1].metadata["file_hash"],
                )
            if self.step == 3:
                return Action.create_operation(OperationKind.FILE_READ, path="notes.txt")
            return Action.create_operation(OperationKind.WORKFLOW_FINISH)

    registry = ToolRegistry()
    registry.register(FilesystemTool())
    state = AgentLoop(Agent(), registry, TraceManager("fresh-v2"), max_steps=4).run(
        AgentState.initialize("replace then reread"), {}, tmp_path
    )

    summaries = "\n".join(item.summary for item in state.working_memory_summary)
    assert "changed" in summaries
    assert "needle" not in summaries
    assert state.workspace_revision == 1


def test_governed_create_and_command_are_reachable_only_with_explicit_approval(tmp_path):
    provider = type("Approved", (), {"request": lambda self, request: ApprovalOutcome.APPROVED})()
    registry = ToolRegistry(policy=ToolPolicy(), approval_provider=provider)
    registry.register(FilesystemTool())
    registry.register(ShellTool())

    create = registry.execute(
        Action.create_operation(OperationKind.FILE_CREATE, path="created.txt", content="ok"), tmp_path
    )
    command = registry.execute(
        Action.create_operation(OperationKind.COMMAND_RUN, argv=[sys.executable, "-c", "print('verified')"]), tmp_path
    )

    assert create.success is True
    assert command.success is True
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "ok"


def test_governed_create_fails_closed_without_an_approval_provider(tmp_path):
    registry = ToolRegistry(policy=ToolPolicy())
    registry.register(FilesystemTool())

    result = registry.execute(
        Action.create_operation(OperationKind.FILE_CREATE, path="blocked.txt", content="no"), tmp_path
    )

    assert result.success is False
    assert result.failure.category == "APPROVAL_UNAVAILABLE"
    assert not (tmp_path / "blocked.txt").exists()
