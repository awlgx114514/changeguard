from dataclasses import dataclass
from pathlib import Path
from enum import Enum
from typing import Optional, Tuple
from code_governance.runtime.action import OperationKind


class ToolPolicyError(PermissionError):
    """Raised when an otherwise valid Action exceeds runtime tool policy."""


@dataclass(frozen=True)
class ToolRuntimeConfig:
    """Runtime-owned limits supplied to ToolPolicy for one run."""

    allowed_tools: Optional[Tuple[str, ...]] = None
    max_tool_calls: int = 100
    shell_timeout_seconds: Optional[float] = 30.0


class PolicyDecision(str, Enum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str = ""
    timeout_seconds: Optional[float] = None
    tool_name: str = ""
    canonical_target: str = ""
    category: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW


class ToolPolicy:
    """Applies permission, call-budget, timeout, and workspace limits before execution."""

    def __init__(self, runtime_config: ToolRuntimeConfig = None):
        self._config = runtime_config or ToolRuntimeConfig()
        self._tool_calls = 0

    def decide(self, action, tool_metadata: dict, workspace: Path) -> PolicyResult:
        tool_name = str(tool_metadata.get("name", ""))
        fields = getattr(getattr(action, "operation", None), "fields", {})
        target = str(fields.get("path") or fields.get("directory") or "")
        canonical_target = ""
        if target:
            try:
                root = Path(workspace).resolve()
                canonical_target = str((root / target).resolve())
            except OSError:
                canonical_target = ""
        if self._config.allowed_tools is not None and tool_name not in self._config.allowed_tools:
            return PolicyResult(
                PolicyDecision.DENY,
                f"tool is not permitted: {tool_name}",
                tool_name=tool_name,
                canonical_target=canonical_target,
                category="POLICY_REJECTED",
            )
        if self._tool_calls >= self._config.max_tool_calls:
            return PolicyResult(
                PolicyDecision.DENY,
                "tool call limit reached",
                tool_name=tool_name,
                canonical_target=canonical_target,
                category="POLICY_REJECTED",
            )
        if target and getattr(action, "operation", None).kind in {
            OperationKind.FILE_READ, OperationKind.FILE_SEARCH, OperationKind.FILE_REPLACE,
            OperationKind.FILE_WRITE, OperationKind.FILE_CREATE, OperationKind.FILE_FIND,
        }:
            try:
                Path(canonical_target).relative_to(Path(workspace).resolve())
            except (ValueError, OSError):
                return PolicyResult(
                    PolicyDecision.DENY,
                    "target must remain inside the workspace",
                    tool_name=tool_name,
                    canonical_target=canonical_target,
                    category="POLICY_REJECTED",
                )

        kind = getattr(action, "operation", None).kind
        if tool_name == "filesystem" and kind in {OperationKind.FILE_READ, OperationKind.FILE_FIND, OperationKind.FILE_SEARCH}:
            decision = PolicyDecision.ALLOW
            category = "READ"
        elif tool_name == "git":
            decision = PolicyDecision.ALLOW
            category = "GIT"
        elif tool_name == "filesystem" and kind in {OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE}:
            decision = PolicyDecision.ASK
            category = "WRITE"
        elif tool_name == "shell":
            decision = PolicyDecision.ASK
            category = "COMMAND"
        else:
            return PolicyResult(
                PolicyDecision.DENY,
                f"tool action is not permitted: {tool_name}",
                tool_name=tool_name,
                canonical_target=canonical_target,
                category="POLICY_REJECTED",
            )

        self._tool_calls += 1
        timeout = self._config.shell_timeout_seconds if tool_name == "shell" else None
        return PolicyResult(
            decision,
            timeout_seconds=timeout,
            tool_name=tool_name,
            canonical_target=canonical_target,
            category=category,
        )

    def check(self, action, tool_metadata: dict, workspace: Path) -> PolicyResult:
        """Compatibility alias for callers migrating to :meth:`decide`."""
        return self.decide(action, tool_metadata, workspace)

    def enforce(self, action, tool_metadata: dict, workspace: Path) -> PolicyResult:
        result = self.decide(action, tool_metadata, workspace)
        if result.decision is PolicyDecision.DENY:
            raise ToolPolicyError(result.reason)
        return result
