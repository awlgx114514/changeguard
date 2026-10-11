"""Canonical Action V2.

The runtime only receives this typed representation.  Historical Action V1
payloads are handled by :mod:`legacy_action_v1` at an explicit ingress, never
by a convenience view on this object.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping
import uuid


class ActionType(str, Enum):
    """Deprecated V1 input names, accepted only by LegacyActionV1Normalizer."""

    READ = "READ"
    WRITE = "WRITE"
    RUN_COMMAND = "RUN_COMMAND"
    GIT_OPERATION = "GIT_OPERATION"
    RUN_CODE = "RUN_CODE"
    GET_CONTEXT = "GET_CONTEXT"
    FINISH = "FINISH"


class OperationKind(str, Enum):
    FILE_READ = "file.read"
    FILE_FIND = "file.find"
    FILE_SEARCH = "file.search"
    FILE_REPLACE = "file.replace"
    FILE_WRITE = "file.write"
    FILE_CREATE = "file.create"
    COMMAND_RUN = "command.run"
    GIT_STATUS = "git.status"
    GIT_DIFF = "git.diff"
    PTC_RUN = "ptc.run"
    CONTEXT_CURRENT = "context.current"
    WORKFLOW_FINISH = "workflow.finish"


_OPERATION_FIELDS = {
    OperationKind.FILE_READ: ("path", "start_line", "line_count", "cursor"),
    OperationKind.FILE_FIND: ("name_query", "directory"),
    OperationKind.FILE_SEARCH: ("query", "path"),
    OperationKind.FILE_REPLACE: ("path", "expected_text", "replacement", "observed_file_hash"),
    OperationKind.FILE_WRITE: ("path", "content", "observed_file_hash"),
    OperationKind.FILE_CREATE: ("path", "content"),
    OperationKind.COMMAND_RUN: ("argv",),
    OperationKind.GIT_STATUS: (),
    OperationKind.GIT_DIFF: (),
    OperationKind.PTC_RUN: ("program",),
    OperationKind.CONTEXT_CURRENT: (),
    OperationKind.WORKFLOW_FINISH: (),
}

_OPERATION_DESCRIPTIONS = {
    OperationKind.FILE_READ: "Read one workspace-relative file by bounded line range or returned cursor.",
    OperationKind.FILE_FIND: "Find files by filename text; name_query is never a path.",
    OperationKind.FILE_SEARCH: "Search literal content in the workspace or one declared path.",
    OperationKind.FILE_REPLACE: "Replace one exactly observed text region in one current file.",
    OperationKind.FILE_WRITE: "Replace a fully observed current existing file.",
    OperationKind.FILE_CREATE: "Create a new file; fails if the path already exists.",
    OperationKind.COMMAND_RUN: "Run an argv command from the workspace after approval.",
    OperationKind.GIT_STATUS: "Read git status.",
    OperationKind.GIT_DIFF: "Read git diff.",
    OperationKind.PTC_RUN: "Experimental bounded read-only PTC program.",
    OperationKind.CONTEXT_CURRENT: "Keep current context available.",
    OperationKind.WORKFLOW_FINISH: "Request final task validation.",
}

_DEFAULT_AGENT_GUIDANCE = {
    "when_to_use": (),
    "when_not_to_use": (),
    "examples": (),
    "transition_hints": (),
}

_OPERATION_AGENT_GUIDANCE = {
    OperationKind.FILE_FIND: {
        "when_to_use": ("exact filename is unknown", "need discover a file location"),
        "when_not_to_use": ("searching source content", "locating class/function definitions"),
        "examples": ("Find the file named action.py", "Find all test files"),
        "transition_hints": ("file.find → file.read",),
    },
    OperationKind.FILE_SEARCH: {
        "when_to_use": ("locating classes/functions", "finding references", "searching error messages"),
        "when_not_to_use": ("reading known file content", "inspecting a known line range"),
        "examples": ("Find where ActionValidator is defined", "Find all usages of action_type"),
        "transition_hints": ("file.search → file.read",),
    },
    OperationKind.FILE_READ: {
        "when_to_use": ("known file path", "need inspect implementation", "need bounded source context"),
        "when_not_to_use": ("discovering files", "searching repository content"),
        "examples": ("Open src/runtime/action.py", "Inspect src/runtime/action.py around the Action class"),
        "transition_hints": ("file.read → file.replace",),
    },
    OperationKind.FILE_REPLACE: {
        "when_to_use": ("target content has been read", "need replace one exact local section"),
        "when_not_to_use": ("creating new files", "broad rewriting"),
        "examples": ("Change this exact function implementation",),
        "transition_hints": ("file.replace → command.run",),
    },
    OperationKind.FILE_WRITE: {
        "when_to_use": ("entire existing file has been observed",),
        "when_not_to_use": ("editing a local section",),
        "examples": ("Replace a fully reviewed generated file",),
        "transition_hints": ("file.write → command.run",),
    },
    OperationKind.COMMAND_RUN: {
        "when_to_use": ("verify a completed modification",),
        "when_not_to_use": ("discovering files", "editing source through shell workarounds"),
        "examples": ("Run the focused test after replacing code",),
        "transition_hints": ("file.replace → command.run", "file.write → command.run"),
    },
}

# The catalog is the shared source for operation identity, shape, agent-facing
# description and Registry route.  Semantic safety rules intentionally remain
# explicit Validator/Runtime code rather than catalog configuration.
_OPERATION_ROUTES = {
    OperationKind.FILE_READ: "filesystem",
    OperationKind.FILE_FIND: "filesystem",
    OperationKind.FILE_SEARCH: "filesystem",
    OperationKind.FILE_REPLACE: "filesystem",
    OperationKind.FILE_WRITE: "filesystem",
    OperationKind.FILE_CREATE: "filesystem",
    OperationKind.COMMAND_RUN: "shell",
    OperationKind.GIT_STATUS: "git",
    OperationKind.GIT_DIFF: "git",
    OperationKind.PTC_RUN: None,
    OperationKind.CONTEXT_CURRENT: None,
    OperationKind.WORKFLOW_FINISH: None,
}

# Provider transport is intentionally separate from the Runtime Action shape.
# Each entry names only model-supplied intent fields and maps them to canonical
# fields without adapter-local operation rules.
_PROVIDER_ARGUMENTS = {
    OperationKind.FILE_READ: (
        ("path", "path", "string", True),
        ("start_line", "start_line", "integer", False),
        ("max_lines", "line_count", "integer", False),
        ("cursor", "cursor", "string", False),
    ),
    OperationKind.FILE_FIND: (
        ("name_query", "name_query", "string", True),
        ("directory", "directory", "string", False),
    ),
    OperationKind.FILE_SEARCH: (
        ("query", "query", "string", True),
        ("path", "path", "string", False),
    ),
    OperationKind.FILE_REPLACE: (
        ("path", "path", "string", True),
        ("expected_text", "expected_text", "string", True),
        ("replacement", "replacement", "string", True),
    ),
    OperationKind.FILE_WRITE: (
        ("path", "path", "string", True),
        ("content", "content", "string", True),
    ),
    OperationKind.FILE_CREATE: (
        ("path", "path", "string", True),
        ("content", "content", "string", True),
    ),
    OperationKind.COMMAND_RUN: (("argv", "argv", "array", True),),
    OperationKind.GIT_STATUS: (),
    OperationKind.GIT_DIFF: (),
    OperationKind.PTC_RUN: (("program", "program", "string", True),),
    OperationKind.CONTEXT_CURRENT: (),
    OperationKind.WORKFLOW_FINISH: (),
}


def operation_route(kind: OperationKind):
    """Return the unique Registry route, or ``None`` for Runtime-only operations."""
    return _OPERATION_ROUTES[OperationKind(kind)]


@dataclass(frozen=True)
class Operation:
    """One typed runtime intent.  ``kind`` is the sole dispatch discriminant."""

    kind: OperationKind
    fields: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, kind: OperationKind, **fields: Any) -> "Operation":
        kind = OperationKind(kind)
        allowed = set(_OPERATION_FIELDS[kind])
        supplied = {key: value for key, value in fields.items() if value is not None}
        unknown = set(supplied) - allowed
        if unknown:
            raise ValueError(f"unsupported fields for {kind.value}: {', '.join(sorted(unknown))}")
        return cls(kind=kind, fields=supplied)


@dataclass(frozen=True)
class Action:
    """Canonical decision IR.  Runtime code dispatches only on ``operation.kind``."""

    id: str
    operation: Operation
    reason: str = ""

    @classmethod
    def create_operation(cls, kind: OperationKind, *, reason: str = "", **fields: Any) -> "Action":
        return cls(id=uuid.uuid4().hex, operation=Operation.create(kind, **fields), reason=reason)

    @classmethod
    def create(cls, action_type: ActionType, target: str = "", input: Dict[str, Any] = None, reason: str = "") -> "Action":
        """Deprecated compatibility ingress; Runtime never calls this method."""
        from .legacy_action_v1 import LegacyActionV1Normalizer
        return LegacyActionV1Normalizer.normalize({
            "id": uuid.uuid4().hex,
            "action_type": ActionType(action_type).value,
            "target": target,
            "input": dict(input or {}),
            "reason": reason,
        })

    def to_dict(self) -> dict:
        return {"id": self.id, "operation": self.operation.kind.value, **self.operation.fields, "reason": self.reason}

    @classmethod
    def model_schema(cls, *, include_run_code: bool = True) -> dict:
        """Provider-safe flattened transport schema; no composition keywords are used."""
        operations = [item.value for item in OperationKind if include_run_code or item is not OperationKind.PTC_RUN]
        nullable_string = {"type": ["string", "null"]}
        properties = {
            "id": {"type": "string", "minLength": 1},
            "operation": {"type": "string", "enum": operations},
            "path": nullable_string,
            "start_line": {"type": ["integer", "null"], "minimum": 1},
            "line_count": {"type": ["integer", "null"], "minimum": 1},
            "cursor": nullable_string,
            "name_query": nullable_string,
            "directory": nullable_string,
            "query": nullable_string,
            "expected_text": nullable_string,
            "replacement": nullable_string,
            "observed_file_hash": nullable_string,
            "content": nullable_string,
            "argv": {"type": ["array", "null"], "items": {"type": "string"}},
            "program": nullable_string,
            "reason": {"type": "string"},
        }
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    @classmethod
    def operation_descriptions(cls, *, include_run_code: bool = True) -> tuple[dict, ...]:
        return tuple(
            {
                "operation": kind.value,
                "fields": _OPERATION_FIELDS[kind],
                "description": _OPERATION_DESCRIPTIONS[kind],
                "tool": _OPERATION_ROUTES[kind],
                **_OPERATION_AGENT_GUIDANCE.get(kind, _DEFAULT_AGENT_GUIDANCE),
                "provider_arguments": tuple(
                    {
                        "name": name,
                        "canonical_field": canonical_field,
                        "type": field_type,
                        "required": required,
                    }
                    for name, canonical_field, field_type, required in _PROVIDER_ARGUMENTS[kind]
                ),
                "provider_example": cls.provider_transport_example(kind),
            }
            for kind in OperationKind
            if include_run_code or kind is not OperationKind.PTC_RUN
        )

    @classmethod
    def provider_arguments(cls, kind: OperationKind) -> tuple[tuple[str, str, str, bool], ...]:
        """Return catalog-owned model transport fields for one operation."""
        return _PROVIDER_ARGUMENTS[OperationKind(kind)]

    @classmethod
    def agent_guidance(cls, kind: OperationKind) -> dict:
        """Return catalog-owned model guidance for one operation."""
        kind = OperationKind(kind)
        return {
            "description": _OPERATION_DESCRIPTIONS[kind],
            **_OPERATION_AGENT_GUIDANCE.get(kind, _DEFAULT_AGENT_GUIDANCE),
        }

    @classmethod
    def provider_transport_example(cls, kind: OperationKind) -> dict:
        """Return a catalog-derived, non-task-specific provider payload example."""
        kind = OperationKind(kind)
        values = {
            "string": "text",
            "integer": 1,
            "array": ["command"],
        }
        arguments = {
            name: values[field_type] if required else None
            for name, _, field_type, required in _PROVIDER_ARGUMENTS[kind]
        }
        reason = "search workspace content" if kind is OperationKind.FILE_SEARCH else f"perform {kind.value}"
        return {"operation": kind.value, "arguments": arguments, "reason": reason}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Action":
        if "operation" not in value:
            raise ValueError("Action V2 requires an operation")
        try:
            kind = OperationKind(str(value["operation"]))
        except (KeyError, ValueError) as error:
            raise ValueError("operation must be a supported Action V2 operation") from error
        if not value.get("id"):
            raise ValueError("Action id must not be empty")
        allowed = set(_OPERATION_FIELDS[kind])
        fields = {key: value[key] for key in allowed if value.get(key) is not None}
        unexpected = [key for key, item in value.items() if key not in {"id", "operation", "reason"} | allowed and item is not None]
        if unexpected:
            raise ValueError(f"fields do not belong to {kind.value}: {', '.join(sorted(unexpected))}")
        return cls(id=str(value["id"]), operation=Operation.create(kind, **fields), reason=str(value.get("reason", "")))
