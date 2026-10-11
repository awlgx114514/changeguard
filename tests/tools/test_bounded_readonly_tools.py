import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from code_governance.runtime.action import Action, ActionType
from code_governance.runtime.bounded_execution import BoundedToolExecutor
from code_governance.tools.git import GitTool
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.registry import ToolRegistry
from code_governance.tools.guards import HardGuard
from code_governance.tools.policy import ToolPolicy, ToolRuntimeConfig
from code_governance.trace.writer import TraceManager


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_git_status_and_diff_keep_existing_read_only_output_shape(tmp_path):
    _run_git(tmp_path, "init")
    _run_git(tmp_path, "config", "user.email", "test@example.com")
    _run_git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "notes.txt").write_text("one\n", encoding="utf-8")
    _run_git(tmp_path, "add", "notes.txt")
    _run_git(tmp_path, "commit", "-m", "initial")
    (tmp_path / "notes.txt").write_text("two\n", encoding="utf-8")

    registry = ToolRegistry(bounded_timeout_seconds=2)
    registry.register(GitTool())

    status = registry.execute(Action.create(ActionType.GIT_OPERATION, target="status"), tmp_path)
    diff = registry.execute(Action.create(ActionType.GIT_OPERATION, target="diff"), tmp_path)

    assert status.success is True
    assert diff.success is True
    assert status.metadata["returncode"] == 0
    assert diff.metadata["returncode"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows descendant termination is required")
def test_git_timeout_terminates_controlled_root_and_child_before_return(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    helper = tmp_path / "slow_git.py"
    helper.write_text(
        textwrap.dedent(
            """
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            open(sys.argv[1], 'w', encoding='ascii').write(str(child.pid))
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )
    tool = GitTool(
        executor=BoundedToolExecutor(),
        command_prefix=(sys.executable, str(helper), str(child_pid_file)),
    )

    with pytest.raises(TimeoutError):
        tool.execute(
            {"action": "status", "_runtime_timeout_seconds": 0.8},
            tmp_path,
        )

    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    probe = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Get-Process -Id {child_pid} -ErrorAction SilentlyContinue"],
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == ""


def test_filesystem_search_keeps_sorted_output_shape(tmp_path):
    (tmp_path / "z-notes.txt").write_text("z", encoding="utf-8")
    (tmp_path / "a-notes.txt").write_text("a", encoding="utf-8")

    result = FilesystemTool().execute({"operation": "file.find", "name_query": "notes"}, tmp_path)

    assert result == {"paths": ["a-notes.txt", "z-notes.txt"]}


@pytest.mark.skipif(os.name != "nt", reason="Windows descendant termination is required")
def test_filesystem_search_timeout_terminates_worker_and_child(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    helper = tmp_path / "slow_search.py"
    helper.write_text(
        textwrap.dedent(
            """
            import json, subprocess, sys, time
            request = json.loads(sys.stdin.read())
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            open(sys.argv[1], 'w', encoding='ascii').write(str(child.pid))
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )
    tool = FilesystemTool(
        search_worker_command=(sys.executable, str(helper), str(child_pid_file)),
    )

    with pytest.raises(TimeoutError):
        tool.execute(
            {"operation": "file.find", "name_query": "timeout", "_runtime_timeout_seconds": 0.8},
            tmp_path,
        )

    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    probe = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Get-Process -Id {child_pid} -ErrorAction SilentlyContinue"],
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == ""


def test_registry_timeout_result_is_authoritative_and_trace_is_quiescent(tmp_path):
    helper = tmp_path / "slow_git.py"
    helper.write_text("import time; time.sleep(30)", encoding="utf-8")
    trace = TraceManager("registry-timeout")
    registry = ToolRegistry(trace_manager=trace, bounded_timeout_seconds=0.2)
    registry.register(GitTool(command_prefix=(sys.executable, str(helper))))

    result = registry.execute(Action.create(ActionType.GIT_OPERATION, target="status"), tmp_path)

    assert result.status == "TIMEOUT"
    assert result.failure.stage == "TOOL_EXECUTION"
    assert result.failure.category == "TOOL_TIMEOUT"
    event_types = [event.type for event in trace.get_events()]
    assert event_types.index("tool_quiescent") < event_types.index("tool_execution_completed")


def test_policy_deny_stops_before_bounded_execution_starts(tmp_path):
    trace = TraceManager("policy-deny-bounded")
    registry = ToolRegistry(
        trace_manager=trace,
        policy=ToolPolicy(ToolRuntimeConfig(allowed_tools=("filesystem",))),
    )
    registry.register(GitTool())

    result = registry.execute(Action.create(ActionType.GIT_OPERATION, target="status"), tmp_path)

    assert result.failure.category == "POLICY_DENY"
    assert "tool_execution_started" not in [event.type for event in trace.get_events()]


def test_hard_guard_deny_stops_before_bounded_execution_starts(tmp_path):
    trace = TraceManager("guard-deny-bounded")
    registry = ToolRegistry(
        trace_manager=trace,
        hard_guards=(HardGuard(prohibited_capabilities=("git",)),),
    )
    registry.register(GitTool())

    result = registry.execute(Action.create(ActionType.GIT_OPERATION, target="status"), tmp_path)

    assert result.failure.category == "HARD_GUARD_DENY"
    assert "tool_execution_started" not in [event.type for event in trace.get_events()]
