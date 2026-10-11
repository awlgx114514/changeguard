import hashlib
from pathlib import Path

from code_governance.tools.base import Tool
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.registry import ToolRegistry
from code_governance.trace.writer import TraceManager
from code_governance.runtime.action import Action, OperationKind


class EchoTool(Tool):
    name = "echo"
    description = "Returns its provided value."
    input_schema = {"value": "string"}

    def execute(self, payload, workspace):
        return {"value": payload["value"]}


class TraceFilesystemTool(Tool):
    name = "filesystem"
    description = "Returns a filesystem-shaped result without touching disk."
    input_schema = {"action": "string", "path": "string"}

    def execute(self, payload, workspace):
        return {"path": payload["path"]}


def test_registry_registers_describes_and_records_tool_calls(tmp_path):
    trace = TraceManager("run-1")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(EchoTool())

    result = registry.invoke("echo", {"value": "ok"}, Path(tmp_path))

    assert registry.get("echo").description == "Returns its provided value."
    assert registry.list_tools() == ("echo",)
    assert result == {"value": "ok"}
    assert [event.type for event in trace.get_events()] == ["tool_called", "tool_finished"]
    assert trace.get_events()[0].data["tool_name"] == "echo"


def test_filesystem_trace_hashes_and_measures_oversized_requested_targets(tmp_path):
    trace = TraceManager("long-filesystem-target")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(TraceFilesystemTool())
    target = "PRIVATE_LONG_TARGET_" + "x" * 600

    registry.invoke("filesystem", {"operation": "file.read", "path": target}, tmp_path)

    expected = f"sha256:{hashlib.sha256(target.encode('utf-8')).hexdigest()};length:{len(target)}"
    events = [event.data for event in trace.get_events() if event.type in {"tool_called", "tool_finished"}]
    assert len(events) == 2
    assert all(event["requested_target"] == expected for event in events)
    assert all(len(event["requested_target"]) <= 512 for event in events)
    assert all("PRIVATE_LONG_TARGET_" not in str(event) for event in events)


def test_filesystem_tool_reads_and_searches_inside_workspace_only(tmp_path):
    (tmp_path / "notes.txt").write_text("channel guidance", encoding="utf-8")
    tool = FilesystemTool()

    content = tool.execute({"operation": "file.read", "path": "notes.txt"}, tmp_path)
    matches = tool.execute({"operation": "file.find", "name_query": "notes"}, tmp_path)

    assert content["path"] == "notes.txt"
    assert content["content"] == "channel guidance"
    assert matches == {"paths": ["notes.txt"]}


def test_registry_executes_a_schema_shaped_write_action(tmp_path):
    marker = "EXPECTED_WRITE_CONTENT"
    registry = ToolRegistry()
    registry.register(FilesystemTool())
    action = Action.create_operation(OperationKind.FILE_CREATE, path="app.py", content=marker)

    registry.execute(action, tmp_path)

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == marker


def test_registry_traces_a_safe_write_failure_diagnostic(tmp_path):
    trace = TraceManager("run-1")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    action = Action.create_operation(OperationKind.FILE_CREATE, path="", content="PRIVATE_WRITE_CONTENT")

    result = registry.execute(action, tmp_path)

    assert result.status == "FAILURE"
    assert result.failure.stage == "PRE_EXECUTION_GOVERNANCE"
    assert result.failure.category == "HARD_GUARD_DENY"

    event = next(event.data for event in trace.get_events() if event.type == "hard_guard_evaluated")

    assert event["passed"] is False
    assert event["reason_hash"]
    assert event["reason_length"] > 0
    assert "PRIVATE_WRITE_CONTENT" not in str(event)
