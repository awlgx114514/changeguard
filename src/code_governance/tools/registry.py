import hashlib
from pathlib import Path
import subprocess
from typing import Mapping, Optional, Tuple
from datetime import datetime, timezone
from time import perf_counter

from .base import Tool
from .approval import ApprovalOutcome, ApprovalRequest
from .guards import HardGuard
from code_governance.runtime.bounded_execution import BoundedToolExecutor
from code_governance.runtime.process_lifecycle import ProcessLifecycleError
from code_governance.runtime.execution import (
    ExecutionResult, FailureRecord, evidence_qualifiers, stream_metadata, timeout_streams,
)
from code_governance.runtime.action import OperationKind, operation_route


class ToolRegistry:
    """Registers tools and records explicit invocations when tracing is enabled."""

    def __init__(self, trace_manager=None, policy=None, approval_provider=None, hard_guards=None, bounded_timeout_seconds: float = 30.0):
        if bounded_timeout_seconds <= 0:
            raise ValueError("bounded_timeout_seconds must be positive")
        self._tools = {}
        self._trace_manager = trace_manager
        self._policy = policy
        self._approval_provider = approval_provider
        self._hard_guards = (HardGuard(),) + tuple(hard_guards or ())
        self._bounded_timeout_seconds = bounded_timeout_seconds
        self._bounded_executor = BoundedToolExecutor()

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool name must not be empty")
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def set_trace_manager(self, trace_manager) -> None:
        self._trace_manager = trace_manager

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"unknown tool: {name}") from error

    def list_tools(self) -> Tuple[str, ...]:
        return tuple(sorted(self._tools))

    def describe(self) -> Tuple[dict, ...]:
        return tuple(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.input_schema),
            }
            for tool in (self._tools[name] for name in self.list_tools())
        )

    def invoke(
        self,
        name: str,
        payload: Mapping[str, object],
        workspace: Path,
        action_id: str = "",
        timeout_seconds=None,
        parent_action_id: str = "",
        capture_omission_callback=None,
    ) -> dict:
        tool = self.get(name)
        start_time = datetime.now(timezone.utc).isoformat()
        started = perf_counter()
        if self._trace_manager is not None:
            self._trace_manager.emit(
                "tool_called",
                {
                    "tool_name": name,
                    "action_id": action_id,
                    "parent_action_id": self._correlation_id(parent_action_id),
                    "start_time": start_time,
                    **self._payload_diagnostics(name, payload),
                },
            )
        try:
            execution_payload = dict(payload)
            if name == "shell" and timeout_seconds is not None:
                execution_payload["_runtime_timeout_seconds"] = timeout_seconds
            if name == "git" or payload.get("operation") == OperationKind.FILE_FIND.value:
                execution_payload.setdefault(
                    "_runtime_timeout_seconds",
                    timeout_seconds or self._bounded_timeout_seconds,
                )
                execution_payload["_runtime_action_id"] = action_id
                execution_payload["_runtime_trace"] = self._trace_manager
                if capture_omission_callback is not None:
                    execution_payload["_runtime_capture_omission_callback"] = capture_omission_callback
            result = tool.execute(execution_payload, workspace)
        except Exception as error:
            if self._trace_manager is not None:
                self._trace_manager.emit(
                    "tool_finished",
                    {
                        "tool_name": name,
                        "action_id": action_id,
                        "parent_action_id": self._correlation_id(parent_action_id),
                        "start_time": start_time,
                        "duration": perf_counter() - started,
                        "success": False,
                        "error_category": "tool_error",
                        **self._payload_diagnostics(name, payload),
                        **self._error_diagnostics(error),
                    },
                )
            raise
        if self._trace_manager is not None:
            self._trace_manager.emit(
                "tool_finished",
                {
                    "tool_name": name,
                    "action_id": action_id,
                    "parent_action_id": self._correlation_id(parent_action_id),
                    "start_time": start_time,
                    "duration": perf_counter() - started,
                    "success": True,
                    "result_type": "tool_result",
                    **self._payload_diagnostics(name, payload),
                },
            )
        return result

    def execute(self, action, workspace: Path, *, parent_action_id: str = "") -> ExecutionResult:
        """Route one canonical Runtime Action to one bounded ExecutionResult."""
        tool_name, payload = self.tool_request(action)
        metadata = {
            "name": tool_name,
            "description": self.get(tool_name).description,
            "input_schema": dict(self.get(tool_name).input_schema),
        }
        policy_result = None
        if self._policy is not None:
            try:
                policy_result = self._policy.decide(action, metadata, workspace)
            except Exception as error:
                self._emit_policy_decision(action, tool_name, "DENY", "", "policy evaluation failed", parent_action_id)
                return self._governance_failure(action, tool_name, "POLICY_DENY", error, parent_action_id)
            self._emit_policy_decision(
                action,
                tool_name,
                policy_result.decision.value,
                policy_result.canonical_target,
                policy_result.reason,
                parent_action_id,
            )
            if policy_result.decision.value == "DENY":
                return self._governance_failure(
                    action,
                    tool_name,
                    "POLICY_DENY",
                    PermissionError(policy_result.reason or "policy denied tool execution"),
                    parent_action_id,
                )
            if policy_result.decision.value == "ASK":
                request = self._approval_request(action, tool_name, policy_result, workspace)
                self._emit_approval_requested(request, parent_action_id)
                outcome = self._approval_outcome(request)
                self._emit_approval_resolved(action, tool_name, policy_result.canonical_target, outcome, parent_action_id)
                if outcome is not ApprovalOutcome.APPROVED:
                    category = (
                        "APPROVAL_REJECTED"
                        if outcome is ApprovalOutcome.REJECTED
                        else "APPROVAL_UNAVAILABLE"
                    )
                    return self._governance_failure(
                        action,
                        tool_name,
                        category,
                        PermissionError(category.lower().replace("_", " ")),
                        parent_action_id,
                    )
        for guard in self._hard_guards:
            try:
                guard_result = guard.check(action, metadata, payload, workspace)
            except Exception as error:
                self._emit_guard(action, tool_name, "", False, "hard guard evaluation failed", parent_action_id)
                return self._governance_failure(action, tool_name, "HARD_GUARD_DENY", error, parent_action_id)
            self._emit_guard(
                action,
                tool_name,
                guard_result.canonical_target,
                guard_result.allowed,
                guard_result.reason,
                parent_action_id,
            )
            if not guard_result.allowed:
                return self._governance_failure(
                    action,
                    tool_name,
                    "HARD_GUARD_DENY",
                    PermissionError(guard_result.reason or "hard guard denied tool execution"),
                    parent_action_id,
                )
        capture_omitted = False

        def record_capture_omission(stdout_omitted: bool, stderr_omitted: bool) -> None:
            nonlocal capture_omitted
            capture_omitted = stdout_omitted or stderr_omitted

        try:
            raw_result = self.invoke(
                tool_name,
                payload,
                workspace,
                action_id=getattr(action, "id", ""),
                timeout_seconds=getattr(policy_result, "timeout_seconds", None),
                parent_action_id=parent_action_id,
                capture_omission_callback=record_capture_omission,
            )
        except Exception as error:
            if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
                category = "TOOL_TIMEOUT"
            elif isinstance(error, ProcessLifecycleError):
                category = "EXECUTION_LIFECYCLE_FAILURE"
            else:
                category = "TOOL_ERROR"
            return self._failure_result(
                action, tool_name, "TOOL_EXECUTION", category, error,
                parent_action_id, invoked=True, capture_omitted=capture_omitted,
            )
        return self._success_result(action, tool_name, raw_result, parent_action_id, capture_omitted)

    def _success_result(self, action, tool_name: str, raw_result: Mapping[str, object], parent_action_id: str = "", capture_omitted: bool = False) -> ExecutionResult:
        operation = action.operation.kind
        result = dict(raw_result)
        metadata = self._semantic_metadata(operation, result, capture_omitted=capture_omitted)
        metadata.setdefault("effect_uncertain", False)
        if operation is OperationKind.COMMAND_RUN:
            metadata.setdefault("output_partial", False)
        canonical_target = str(metadata.get("path", self._action_target(action)))
        effect_type = self._effect_type(operation)
        state_changed = bool(metadata.get("state_changed", False))
        if parent_action_id:
            metadata["parent_action_id"] = parent_action_id
        information_gain = operation in {
            OperationKind.FILE_READ, OperationKind.FILE_FIND, OperationKind.FILE_SEARCH,
            OperationKind.COMMAND_RUN, OperationKind.GIT_STATUS, OperationKind.GIT_DIFF,
        }
        fingerprint = self._fingerprint({"tool": tool_name, "effect": effect_type, "metadata": metadata})
        return ExecutionResult(
            action_id=action.id,
            status="SUCCESS",
            canonical_target=canonical_target,
            effect_type=effect_type,
            state_changed=state_changed,
            information_gain=information_gain,
            result_fingerprint=fingerprint,
            summary=self._summary(operation, canonical_target, metadata),
            artifact_reference=canonical_target or None,
            metadata=metadata,
        )

    def _failure_result(self, action, tool_name: str, stage: str, category: str, error: Exception, parent_action_id: str = "", *, invoked: bool = False, capture_omitted: bool = False) -> ExecutionResult:
        text = str(error)
        operation = action.operation.kind
        effect_type = self._effect_type(operation)
        effect_uncertain = invoked and operation in {
            OperationKind.COMMAND_RUN,
            OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE,
        }
        metadata = {
            "failure_stage": stage,
            "failure_category": category,
            "effect_uncertain": effect_uncertain,
        }
        if capture_omitted:
            metadata["truncated"] = True
        timeout_output = timeout_streams(error) if category == "TOOL_TIMEOUT" else None
        if timeout_output is not None:
            stdout, stderr = timeout_output
            metadata.update(stream_metadata(stdout, stderr))
            metadata["output_partial"] = True
        elif operation is OperationKind.COMMAND_RUN:
            metadata.update(stream_metadata("", ""))
            metadata["output_partial"] = category == "TOOL_TIMEOUT"
        else:
            metadata["output_partial"] = False
        if parent_action_id:
            metadata["parent_action_id"] = parent_action_id
        failure = FailureRecord(
            stage=stage,
            category=category,
            action_id=str(getattr(action, "id", "")),
            tool=tool_name,
            detail=text[:512],
            error_type=type(error).__name__,
            error_hash=self._fingerprint(text),
        )
        return ExecutionResult(
            action_id=failure.action_id,
            status=(
                "TIMEOUT"
                if category == "TOOL_TIMEOUT" and tool_name in {"git", "filesystem"}
                else "FAILURE"
            ),
            canonical_target=self._action_target(action),
            effect_type=effect_type,
            result_fingerprint=failure.error_hash,
            summary=self._failure_summary(failure.detail or category, metadata),
            metadata=metadata,
            failure=failure,
        )

    @staticmethod
    def _failure_summary(detail: str, metadata: Mapping[str, object]) -> str:
        parts = list(evidence_qualifiers(dict(metadata)))
        for stream in ("stdout", "stderr"):
            preview = metadata.get(f"{stream}_preview", "")
            if preview:
                parts.append(f"{stream}:\n{preview}")
        if not parts:
            return detail[:8192]
        parts.append(f"Failure: {detail[:512]}")
        return "\n".join(parts)[:9216]

    def _governance_failure(self, action, tool_name: str, category: str, error: Exception, parent_action_id: str) -> ExecutionResult:
        return self._failure_result(action, tool_name, "PRE_EXECUTION_GOVERNANCE", category, error, parent_action_id)

    @staticmethod
    def _bounded(value) -> str:
        return str(value or "")[:512]

    @classmethod
    def _correlation_id(cls, value) -> str:
        """Keep normal IDs readable while making oversized IDs trace-safe."""
        text = str(value or "")
        if len(text) <= 512:
            return text
        return f"sha256:{cls._fingerprint(text)}"

    def _approval_request(self, action, tool_name: str, policy_result, workspace: Path) -> ApprovalRequest:
        target = policy_result.canonical_target or self._resolved_target(workspace, self._action_target(action)) or ""
        return ApprovalRequest(
            action_id=self._correlation_id(getattr(action, "id", "")),
            tool_name=self._bounded(tool_name),
            canonical_target=self._bounded(target),
            effect=self._bounded(action.operation.kind.value),
            reason=self._bounded(getattr(action, "reason", "") or policy_result.reason),
        )

    def _approval_outcome(self, request: ApprovalRequest) -> ApprovalOutcome:
        if self._approval_provider is None:
            return ApprovalOutcome.UNAVAILABLE
        try:
            outcome = self._approval_provider.request(request)
        except Exception:
            return ApprovalOutcome.UNAVAILABLE
        return outcome if isinstance(outcome, ApprovalOutcome) else ApprovalOutcome.UNAVAILABLE

    def _emit_policy_decision(self, action, tool_name: str, decision: str, canonical_target: str, reason: str, parent_action_id: str) -> None:
        if self._trace_manager is None:
            return
        self._trace_manager.emit("tool_policy_decided", {
            "action_id": self._correlation_id(getattr(action, "id", "")),
            "parent_action_id": self._correlation_id(parent_action_id),
            "tool_name": self._bounded(tool_name),
            "canonical_target": self._bounded(canonical_target),
            "decision": decision,
            "reason_length": len(str(reason or "")),
            "reason_hash": self._fingerprint(self._bounded(reason)),
        })

    def _emit_approval_requested(self, request: ApprovalRequest, parent_action_id: str) -> None:
        if self._trace_manager is None:
            return
        self._trace_manager.emit("approval_requested", {
            "action_id": request.action_id,
            "parent_action_id": self._correlation_id(parent_action_id),
            "tool_name": request.tool_name,
            "canonical_target": request.canonical_target,
            "effect": request.effect,
        })

    def _emit_approval_resolved(self, action, tool_name: str, canonical_target: str, outcome: ApprovalOutcome, parent_action_id: str) -> None:
        if self._trace_manager is None:
            return
        self._trace_manager.emit("approval_resolved", {
            "action_id": self._correlation_id(getattr(action, "id", "")),
            "parent_action_id": self._correlation_id(parent_action_id),
            "tool_name": self._bounded(tool_name),
            "canonical_target": self._bounded(canonical_target),
            "outcome": outcome.value,
        })

    def _emit_guard(self, action, tool_name: str, canonical_target: str, allowed: bool, reason: str, parent_action_id: str) -> None:
        if self._trace_manager is None:
            return
        self._trace_manager.emit("hard_guard_evaluated", {
            "action_id": self._correlation_id(getattr(action, "id", "")),
            "parent_action_id": self._correlation_id(parent_action_id),
            "tool_name": self._bounded(tool_name),
            "canonical_target": self._bounded(canonical_target),
            "passed": allowed,
            "reason_length": len(str(reason or "")),
            "reason_hash": self._fingerprint(self._bounded(reason)),
        })

    def tool_request(self, action):
        """Return the tool and immutable execution payload for a Runtime Action."""
        kind = action.operation.kind
        fields = dict(action.operation.fields)
        route = operation_route(kind)
        if route == "filesystem":
            return route, {"operation": kind.value, **fields}
        if route == "shell":
            # Validation owns argv completeness.  This route is also queried
            # while recording a rejected action, so it must not obscure the
            # earlier ActionValidator failure with a KeyError.
            return route, {"command": fields.get("argv")}
        if route == "git":
            return route, {"action": "status" if kind is OperationKind.GIT_STATUS else "diff"}
        raise ValueError(f"Action does not map to a tool: {kind.value}")

    def _emit_policy_diagnostic(self, action, workspace: Path, passed: bool, error=None) -> None:
        if self._trace_manager is None:
            return
        target = self._action_target(action)
        data = {
            "action_id": str(getattr(action, "id", "")),
            "passed": passed,
            "requested_target": target,
            "resolved_target": self._resolved_target(workspace, target),
        }
        if error is not None:
            data.update(self._error_diagnostics(error))
        self._trace_manager.emit("tool_policy_evaluated", data)

    @staticmethod
    def _resolved_target(workspace: Path, target: str):
        try:
            return (Path(workspace).resolve() / target).resolve().as_posix()
        except OSError:
            return None

    @classmethod
    def _payload_diagnostics(cls, name: str, payload: Mapping[str, object]) -> dict:
        if name != "filesystem":
            return {}
        data = {
            "operation": payload.get("operation"),
            "requested_target": cls._trace_target(payload.get("path")),
        }
        content = payload.get("content")
        if isinstance(content, str):
            data.update(
                {
                    "content_length": len(content),
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "content_empty": not content,
                }
            )
        return data

    @classmethod
    def _trace_target(cls, value) -> str:
        if not isinstance(value, str):
            return f"invalid:{type(value).__name__}"
        if len(value) <= 512:
            return value
        return f"sha256:{cls._fingerprint(value)};length:{len(value)}"

    @classmethod
    def _semantic_metadata(cls, operation: OperationKind, result: Mapping[str, object], *, capture_omitted: bool = False) -> dict:
        if operation in {OperationKind.FILE_FIND, OperationKind.FILE_SEARCH}:
            if "matches" in result:
                matches = list(result.get("matches", ()))[:1000]
                return {
                    "matches": matches, "match_count": len(matches),
                    "content_preview": "", "content_truncated": False,
                    "truncated": capture_omitted,
                }
            if "paths" in result:
                paths = [str(path) for path in result.get("paths", ())][:1000]
                return {
                    "paths": paths, "path_count": len(paths),
                    "content_preview": "", "content_truncated": False,
                    "truncated": capture_omitted,
                }
        if operation is OperationKind.FILE_READ:
            content = str(result.get("content", ""))
            return {
                "path": str(result.get("path", "")),
                "content_length": len(content),
                "content_fingerprint": cls._fingerprint(content),
                "file_hash": str(result.get("file_hash", cls._fingerprint(content))),
                "content_preview": content[:8192],
                "content_truncated": bool(result.get("next_cursor")) or len(content) > 8192,
                "next_cursor": result.get("next_cursor"),
                "eof": bool(result.get("eof", not result.get("next_cursor"))),
                "start_line": result.get("start_line"),
                "line_count": result.get("line_count"),
            }
        if operation in {OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE}:
            return {
                "path": str(result.get("path", "")),
                "written": int(result.get("written", 0)),
                "content_fingerprint": str(result.get("content_fingerprint", "")),
                "state_changed": bool(result.get("state_changed", False)),
            }
        streams = {}
        stream_truncated = capture_omitted
        for key in ("stdout", "stderr"):
            text = str(result.get(key, ""))
            omitted = result.get(f"{key}_omitted") is True
            was_truncated = omitted or len(text) > 4096
            stream_truncated = stream_truncated or was_truncated
            streams.update(
                {
                    f"{key}_length": len(text),
                    f"{key}_fingerprint": cls._fingerprint(text),
                    f"{key}_preview": text[:4096],
                    f"{key}_truncated": was_truncated,
                }
            )
        return {"returncode": result.get("returncode"), "truncated": stream_truncated, **streams}

    @staticmethod
    def _summary(operation: OperationKind, target: str, metadata: Mapping[str, object]) -> str:
        qualifiers = evidence_qualifiers(dict(metadata))
        if operation in {OperationKind.FILE_FIND, OperationKind.FILE_SEARCH}:
            if "paths" in metadata:
                summary = f"Search returned {metadata['path_count']} paths"
            else:
                summary = f"Search returned {metadata.get('match_count', 0)} matches"
        elif operation is OperationKind.FILE_READ:
            summary = (
                f"Read {target}; content_fingerprint={metadata['content_fingerprint']}; "
                f"content_length={metadata['content_length']}; "
                f"truncated={metadata['content_truncated']}:\n{metadata['content_preview']}"
            )
        elif operation in {OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE}:
            summary = (
                f"Wrote {target}; state_changed={metadata['state_changed']}; "
                f"content_fingerprint={metadata['content_fingerprint']}"
            )
        else:
            summary = f"Ran {operation.value}; returncode={metadata.get('returncode')}"
        if operation is OperationKind.COMMAND_RUN:
            output = "\n".join(
                f"{stream}:\n{metadata.get(f'{stream}_preview', '')}"
                for stream in ("stdout", "stderr")
                if metadata.get(f"{stream}_preview")
            )
            if output:
                summary = f"{summary}; command output:\n{output}"
        return "\n".join((*qualifiers, summary))

    @staticmethod
    def _action_target(action) -> str:
        fields = action.operation.fields
        for field in ("path", "directory", "name_query", "query"):
            value = fields.get(field)
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _effect_type(operation: OperationKind) -> str:
        if operation in {OperationKind.FILE_READ, OperationKind.FILE_FIND, OperationKind.FILE_SEARCH}:
            return "READ"
        if operation in {OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE}:
            return "WRITE"
        if operation is OperationKind.COMMAND_RUN:
            return "COMMAND"
        return "GIT"

    @staticmethod
    def _fingerprint(value) -> str:
        if not isinstance(value, str):
            import json
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _error_diagnostics(error: Exception) -> dict:
        text = str(error)
        preview = text[:512]
        return {
            "error_type": type(error).__name__,
            "error_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "error_preview": preview,
            "error_preview_truncated": len(text) > len(preview),
        }
