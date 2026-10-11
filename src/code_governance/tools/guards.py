"""Deterministic execution invariants which approval cannot override."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional
from code_governance.runtime.action import OperationKind


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str = ""
    canonical_target: str = ""


class HardGuard:
    """Check workspace, input-shape, capability, and optional call-limit invariants."""

    def __init__(
        self,
        *,
        max_tool_calls: Optional[int] = None,
        prohibited_capabilities: Iterable[str] = (),
    ):
        self._max_tool_calls = max_tool_calls
        self._prohibited_capabilities = frozenset(str(item) for item in prohibited_capabilities)
        self._tool_calls = 0

    def check(self, action, tool_metadata: Mapping[str, object], payload: Mapping[str, object], workspace: Path) -> GuardResult:
        tool_name = str(tool_metadata.get("name", ""))
        canonical_target = self._canonical_target(action, workspace)
        if tool_name == "filesystem":
            if action.operation.kind not in {OperationKind.FILE_FIND, OperationKind.FILE_SEARCH} and not canonical_target:
                return GuardResult(False, "filesystem target must remain inside the workspace")
            shape_error = self._filesystem_shape_error(payload)
        elif tool_name == "shell":
            shape_error = self._shell_shape_error(payload)
        elif tool_name == "git":
            shape_error = self._git_shape_error(payload)
        else:
            shape_error = "tool has no approved hard-guard payload shape"
        if shape_error:
            return GuardResult(False, shape_error, canonical_target)
        capabilities = {tool_name}
        metadata_capabilities = tool_metadata.get("capabilities", ())
        if isinstance(metadata_capabilities, (list, tuple, set, frozenset)):
            capabilities.update(str(item) for item in metadata_capabilities)
        if capabilities & self._prohibited_capabilities:
            return GuardResult(False, "tool capability is prohibited", canonical_target)
        if self._max_tool_calls is not None and self._tool_calls >= self._max_tool_calls:
            return GuardResult(False, "tool call limit reached", canonical_target)
        self._tool_calls += 1
        return GuardResult(True, canonical_target=canonical_target)

    @staticmethod
    def _canonical_target(action, workspace: Path) -> str:
        fields = getattr(getattr(action, "operation", None), "fields", {})
        target = fields.get("path") or fields.get("directory")
        if not target:
            return ""
        if not isinstance(target, str) or not target:
            return ""
        try:
            root = Path(workspace).resolve()
            candidate = Path(target)
            if candidate.is_absolute():
                return ""
            resolved = (root / candidate).resolve()
            resolved.relative_to(root)
            return str(resolved)
        except (OSError, ValueError):
            return ""

    @staticmethod
    def _filesystem_shape_error(payload: Mapping[str, object]) -> str:
        operation = payload.get("operation")
        if operation not in {item.value for item in OperationKind if item.value.startswith("file.")}:
            return "filesystem payload is invalid"
        if operation not in {"file.find", "file.search"} and not isinstance(payload.get("path"), str):
            return "filesystem path is invalid"
        if operation in {"file.write", "file.create"} and not isinstance(payload.get("content"), str):
            return "filesystem content must be text"
        return ""

    @staticmethod
    def _shell_shape_error(payload: Mapping[str, object]) -> str:
        command = payload.get("command")
        if not isinstance(command, (list, tuple)) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            return "command must be a non-empty list of strings"
        return ""

    @staticmethod
    def _git_shape_error(payload: Mapping[str, object]) -> str:
        if payload.get("action") not in {"status", "diff"}:
            return "git payload is invalid"
        return ""
