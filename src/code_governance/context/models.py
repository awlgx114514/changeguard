from dataclasses import dataclass
from typing import Any, Tuple

from code_governance.memory.retriever import TaskContext
from code_governance.memory.models import Strategy
from code_governance.repo.analyzer import RepoContext
from code_governance.validator.repair_context import RepairFailureContext


@dataclass(frozen=True)
class ContextFile:
    """A compact file fact selected for one task."""

    path: str
    module: str
    role: str
    summary: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "module": self.module,
            "role": self.role,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class MemoryContext:
    """A compact, selected long-term Memory rule for one task."""

    id: str
    type: str
    rule: str
    scope: str
    reason: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "rule": self.rule,
            "scope": self.scope,
            "reason": self.reason,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class StrategyContext:
    """A compact Strategy reference selected for one task."""

    id: str
    strategy: str
    source_failure: str
    confidence: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "strategy": self.strategy,
            "source_failure": self.source_failure,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AgentContext:
    """Serializable static working context for one Harness task."""

    version: str
    task_context: TaskContext
    repository_context: RepoContext
    memory_context: Tuple[MemoryContext, ...]
    project_rules: Tuple[str, ...]
    relevant_files: Tuple[ContextFile, ...]
    constraints: Tuple[str, ...]
    strategies: Tuple[StrategyContext, ...] = ()
    available_tools: Tuple[str, ...] = ()
    available_operations: Tuple[dict, ...] = ()
    current_goal: str = ""
    working_memory: Tuple[str, ...] = ()
    active_issues: Tuple[str, ...] = ()
    decisions: Tuple[str, ...] = ()
    contract: Any = None
    repair_context: Tuple[RepairFailureContext, ...] = ()

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "task_context": self.task_context.to_dict(),
            "repository_context": self.repository_context.to_dict(),
            "memory_context": [item.to_dict() for item in self.memory_context],
            "project_rules": list(self.project_rules),
            "relevant_files": [item.to_dict() for item in self.relevant_files],
            "constraints": list(self.constraints),
            "strategies": [item.to_dict() for item in self.strategies],
            "available_tools": list(self.available_tools),
            "available_operations": [dict(item) for item in self.available_operations],
            "current_goal": self.current_goal,
            "working_memory": list(self.working_memory),
            "active_issues": list(self.active_issues),
            "decisions": list(self.decisions),
            "contract": self.contract,
            "repair_context": [item.to_dict() for item in self.repair_context],
        }
