"""Deterministic, evaluation-only Native versus explicit PTC comparison.

This module deliberately does not add a runtime mode or alter the model-facing
capability set.  It supplies a scripted agent so orchestration depth and
measurement can be compared without depending on a live model.
"""

from dataclasses import dataclass, asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Dict, Iterable, Mapping, Tuple

from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.action_validator import ActionValidator
from code_governance.runtime.loop import AgentLoop
from code_governance.runtime.ptc import PTCCodeRuntime
from code_governance.runtime.state import AgentState
from code_governance.trace.writer import TraceManager
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.git import GitTool
from code_governance.tools.policy import ToolPolicy, ToolRuntimeConfig
from code_governance.tools.registry import ToolRegistry


@dataclass(frozen=True)
class ComparisonMetrics:
    mode: str
    orchestration_depth: int
    task_success: bool
    model_request_count: int
    input_tokens: None = None
    output_tokens: None = None
    total_tokens: None = None
    model_visible_context_bytes: int = 0
    tool_call_count: int = 0
    action_count: int = 0
    wall_clock_seconds: float = 0.0
    time_in_model_seconds: None = None
    time_in_tools_seconds: None = None
    time_in_code_runtime_seconds: None = None
    raw_child_result_bytes: int = 0
    curated_result_bytes: int = 0

    @property
    def context_compression_ratio(self):
        if self.raw_child_result_bytes <= 0:
            return None
        return self.curated_result_bytes / self.raw_child_result_bytes

    def to_dict(self) -> dict:
        result = asdict(self)
        result["context_compression_ratio"] = self.context_compression_ratio
        return result


@dataclass(frozen=True)
class ComparisonResult:
    workload: str
    native: ComparisonMetrics
    ptc: ComparisonMetrics

    def to_dict(self) -> dict:
        return {
            "workload": self.workload,
            "native": self.native.to_dict(),
            "ptc": self.ptc.to_dict(),
            "fair_comparison": {
                "same_workspace_snapshot": True,
                "same_tool_surface": ["search_files", "git_status", "git_diff"],
                "same_governance": True,
                "same_scripted_workload": True,
            },
        }


class _ScriptedAgent:
    def __init__(self, actions: Iterable[Action]):
        self._actions = tuple(actions)
        self.requests = 0
        self.context_bytes = []

    def decide(self, state, context):
        self.requests += 1
        payload = context.to_dict() if hasattr(context, "to_dict") else context
        self.context_bytes.append(len(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")))
        return self._actions[self.requests - 1]


class _DecisionContext:
    def with_state(self, context, state):
        return {
            "base": context,
            "working_memory": [item.summary for item in state.working_memory_summary],
            "observations": [observation.summary for observation in state.recent_observations],
        }


def _registry(trace):
    registry = ToolRegistry(
        trace_manager=trace,
        policy=ToolPolicy(ToolRuntimeConfig()),
    )
    registry.register(FilesystemTool())
    registry.register(GitTool())
    return registry


def _native_actions(depth: int) -> Tuple[Action, ...]:
    actions = [
        Action.create_operation(OperationKind.FILE_FIND, name_query=f"needle{index}")
        for index in range(depth)
    ]
    actions.append(Action.create_operation(OperationKind.GIT_STATUS))
    actions.append(Action.create_operation(OperationKind.WORKFLOW_FINISH))
    return tuple(actions)


def _ptc_actions(depth: int) -> Tuple[Action, ...]:
    statements = [
        f"paths_{index} = tools.search_files('needle{index}')"
        for index in range(depth)
    ]
    statements.append("status = tools.git_status()")
    statements.append("result = {'match_count': len(paths_0), 'status_seen': len(status['stdout']) >= 0}")
    return (
        Action.create_operation(OperationKind.PTC_RUN, program="\n".join(statements)),
        Action.create_operation(OperationKind.WORKFLOW_FINISH),
    )


def _run_mode(workspace: Path, mode: str, depth: int) -> ComparisonMetrics:
    trace = TraceManager(f"ptc-compare-{mode}-{depth}")
    registry = _registry(trace)
    agent = _ScriptedAgent(_native_actions(depth) if mode == "native" else _ptc_actions(depth))
    state = AgentState.initialize("inspect the repository", workspace=workspace)
    started = perf_counter()
    result = AgentLoop(
        agent,
        registry,
        trace,
        context_builder=_DecisionContext(),
        action_validator=ActionValidator(execution_mode="PTC" if mode == "ptc" else "NATIVE"),
        ptc_runtime=(PTCCodeRuntime(tool_registry=registry, timeout_seconds=5) if mode == "ptc" else None),
        max_steps=max(depth + 3, 5),
    ).run(state, {"task": "inspect"}, workspace)
    elapsed = perf_counter() - started
    events = trace.get_events()
    tool_calls = sum(event.type == "tool_called" for event in events)
    action_count = sum(event.type == "action_decided" for event in events)
    child_bytes = sum(
        len(json.dumps(event.data.get("result", {}), sort_keys=True, default=str).encode("utf-8"))
        for event in events if event.type == "ptc_child_result"
    )
    curated_bytes = sum(
        len(json.dumps(event.data.get("metadata", {}), sort_keys=True, default=str).encode("utf-8"))
        for event in events if event.type == "observation_received"
    )
    return ComparisonMetrics(
        mode=mode,
        orchestration_depth=depth,
        task_success=result.status == "MODEL_FINISHED" and not result.errors,
        model_request_count=agent.requests,
        model_visible_context_bytes=sum(agent.context_bytes),
        tool_call_count=tool_calls,
        action_count=action_count,
        wall_clock_seconds=elapsed,
        raw_child_result_bytes=child_bytes,
        curated_result_bytes=curated_bytes,
    )


def compare_depth(depth: int) -> ComparisonResult:
    """Compare one orchestration depth in one identical temporary snapshot."""
    if depth < 1:
        raise ValueError("depth must be positive")
    with TemporaryDirectory(prefix="changeguard-ptc-compare-") as directory:
        workspace = Path(directory)
        for index in range(depth):
            (workspace / f"needle{index}.txt").write_text("needle\n", encoding="utf-8")
        native = _run_mode(workspace, "native", depth)
        ptc = _run_mode(workspace, "ptc", depth)
        return ComparisonResult(f"search×{depth}+git_status", native, ptc)


def compare_scaling(depths=(1, 2, 3, 5, 10)) -> Tuple[ComparisonResult, ...]:
    """Run the required 1–2, 3–5, and 10+ depth points."""
    return tuple(compare_depth(depth) for depth in depths)
