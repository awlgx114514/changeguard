"""Runtime dependency profiles used exclusively by evaluation runs."""

from dataclasses import dataclass
from typing import Literal, Optional, Tuple

from code_governance.runtime.action import Action
from code_governance.runtime.action_validator import ActionValidationError, ActionValidator
from code_governance.runtime.history import HistoryManager, WorkingMemoryItem
from code_governance.tools import FilesystemTool, GitTool, ShellTool, ToolPolicy, ToolRegistry, ToolRuntimeConfig
from code_governance.trace.events import EVENT_TYPES
from code_governance.trace.writer import TraceManager, TraceWriter


RuntimeMode = Literal["baseline", "hardened"]


class BaselineActionValidator:
    """Compatibility validator that only requires a Runtime Action instance."""

    def validate(self, action: Action) -> None:
        if not isinstance(action, Action):
            raise ActionValidationError("decision must be an Action")


class BaselineHistoryManager(HistoryManager):
    """Maintains compact working memory without Issue or decision lifecycle state."""

    def record(self, state, action, observation) -> dict:
        item = WorkingMemoryItem(
            key=f"{observation.type}:{action.operation.fields.get('path', action.operation.fields.get('name_query', ''))}",
            type=observation.type,
            summary=observation.summary or action.reason or action.operation.kind.value,
            target=str(action.operation.fields.get('path', action.operation.fields.get('name_query', ''))),
            importance=self._importance(action, observation),
        )
        state.working_memory_summary = list(
            self._summary_provider.summarize(
                tuple(state.working_memory_summary) + (item,)
            )
        )
        return {
            "working_items": len(state.working_memory_summary),
            "active_issues": 0,
        }


@dataclass(frozen=True)
class _BaselineTraceEvent:
    """Pre-v2 event shape without ordered-event or time metadata."""

    type: str
    data: dict

    def to_dict(self) -> dict:
        return {"type": self.type, "data": dict(self.data)}


@dataclass(frozen=True)
class _BaselineTrace:
    """Pre-v2 persisted Trace shape used by the compatibility profile."""

    run_id: str
    events: Tuple[_BaselineTraceEvent, ...]
    schema_version: str = "v1"

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "events": [event.to_dict() for event in self.events],
        }


class CompactTraceManager:
    """TraceManager adapter that persists only the comparable Action summary."""

    _ALLOWED_DATA_KEYS = frozenset(
        {"action_id", "operation", "target", "tool", "success"}
    )

    def __init__(self, run_id: str, writer: Optional[TraceWriter] = None):
        self._run_id = run_id
        self._writer = writer
        self._events = []

    def emit(self, event_type: str, data: dict) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported trace event: {event_type}")
        self._events.append(
            _BaselineTraceEvent(
                event_type,
                {key: data[key] for key in self._ALLOWED_DATA_KEYS if key in data},
            )
        )

    def get_events(self):
        return tuple(self._events)

    def set_context_version(self, context_version: str) -> None:
        del context_version

    def write(self):
        if self._writer is None:
            raise ValueError("CompactTraceManager has no writer")
        return self._writer.write(_BaselineTrace(self._run_id, self.get_events()))


@dataclass(frozen=True)
class RuntimeProfile:
    """Builds evaluation-only Runtime dependencies for one named mode."""

    mode: RuntimeMode
    runtime_repair: bool

    @classmethod
    def for_mode(cls, mode: str) -> "RuntimeProfile":
        if mode == "baseline":
            return cls(
                mode="baseline",
                runtime_repair=False,
            )
        if mode == "hardened":
            return cls(
                mode="hardened",
                runtime_repair=True,
            )
        raise ValueError(f"unsupported runtime mode: {mode}")

    @property
    def runtime_issue_lifecycle(self) -> bool:
        return self.mode == "hardened"

    def build_tool_registry(self) -> ToolRegistry:
        registry = (
            ToolRegistry()
            if self.mode == "baseline"
            else ToolRegistry(policy=ToolPolicy(ToolRuntimeConfig()))
        )
        registry.register(FilesystemTool())
        registry.register(GitTool())
        registry.register(ShellTool())
        return registry

    def trace_manager(self, run_id: str, writer: TraceWriter):
        if self.mode == "baseline":
            return CompactTraceManager(run_id, writer)
        return TraceManager(run_id, writer)

    def history_manager(self):
        if self.mode == "baseline":
            return BaselineHistoryManager()
        return HistoryManager()

    def action_validator(self):
        if self.mode == "baseline":
            return BaselineActionValidator()
        return ActionValidator()
