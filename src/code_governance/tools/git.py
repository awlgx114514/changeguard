from pathlib import Path
from typing import Mapping, Sequence

from code_governance.runtime.bounded_execution import BoundedToolExecutor
from code_governance.runtime.process_lifecycle import ProcessLifecycleError
from .base import Tool


class GitTool(Tool):
    """Reads Git facts from the active workspace without changing it."""

    name = "git"
    description = "Read workspace Git status or diff."
    input_schema = {"action": "status | diff"}

    def __init__(self, executor=None, command_prefix: Sequence[str] = ("git",)):
        self._executor = executor or BoundedToolExecutor()
        self._command_prefix = tuple(command_prefix)

    def execute(self, payload: Mapping[str, object], workspace: Path) -> dict:
        action = str(payload.get("action", ""))
        commands = {"status": ["status", "--short"], "diff": ["diff", "--no-ext-diff"]}
        if action not in commands:
            raise ValueError(f"unsupported git action: {action}")
        execution = self._executor.run_subprocess(
            [*self._command_prefix, *commands[action]],
            workspace,
            timeout_seconds=float(payload.get("_runtime_timeout_seconds", 30.0)),
            action_id=str(payload.get("_runtime_action_id", "")),
            trace=payload.get("_runtime_trace"),
            omission_callback=payload.get("_runtime_capture_omission_callback"),
        )
        if execution.status == "TIMEOUT":
            raise TimeoutError("git command timed out after the bounded deadline")
        if execution.status == "FAILURE":
            raise ProcessLifecycleError(execution.error or "git process lifecycle failed")
        return {
            "returncode": execution.returncode,
            "stdout": execution.stdout,
            "stderr": execution.stderr,
        }
