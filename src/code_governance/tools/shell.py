from pathlib import Path
import subprocess
from typing import Mapping

from .base import Tool


class ShellTool(Tool):
    """Runs an explicitly supplied command inside the active workspace."""

    name = "shell"
    description = "Run a command list within the active workspace."
    input_schema = {"command": "non-empty list of command arguments"}

    def execute(self, payload: Mapping[str, object], workspace: Path) -> dict:
        command = payload.get("command")
        if not isinstance(command, (list, tuple)) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise ValueError("command must be a non-empty list of strings")
        timeout = payload.get("_runtime_timeout_seconds")
        try:
            completed = subprocess.run(
                list(command),
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"shell command timed out after {timeout} seconds") from error
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
