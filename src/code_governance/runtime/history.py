from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Tuple
import hashlib
import uuid

from .action import OperationKind
from .execution import evidence_qualifiers


class IssueStatus(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True)
class Issue:
    """A compact, state-owned lifecycle record for one Runtime problem."""

    id: str
    type: str
    description: str
    status: IssueStatus = IssueStatus.OPEN

    @classmethod
    def create(cls, type: str, description: str) -> "Issue":
        return cls(uuid.uuid4().hex, type, description)

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "description": self.description, "status": self.status.value}

    @classmethod
    def from_dict(cls, value: dict) -> "Issue":
        return cls(
            id=str(value["id"]),
            type=str(value.get("type", "runtime_error")),
            description=str(value["description"]),
            status=IssueStatus(value.get("status", IssueStatus.OPEN.value)),
        )


@dataclass(frozen=True)
class WorkingMemoryItem:
    """A compact, agent-visible fact from the current run only."""

    key: str
    type: str
    summary: str
    target: str
    importance: int
    status: str = "active"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "type": self.type,
            "summary": self.summary,
            "target": self.target,
            "importance": self.importance,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "WorkingMemoryItem":
        return cls(
            key=str(value["key"]),
            type=str(value["type"]),
            summary=str(value["summary"]),
            target=str(value.get("target", "")),
            importance=int(value.get("importance", 0)),
            status=str(value.get("status", "active")),
        )


class SummaryProvider(ABC):
    """Future boundary for replacing rule-based compression with a model."""

    @abstractmethod
    def summarize(self, items: Iterable[WorkingMemoryItem]) -> Tuple[WorkingMemoryItem, ...]:
        """Return compact, importance-ordered working facts."""


class RuleBasedSummaryProvider(SummaryProvider):
    """Deterministically deduplicates and prioritizes short task history."""

    def summarize(self, items: Iterable[WorkingMemoryItem]) -> Tuple[WorkingMemoryItem, ...]:
        by_key = {}
        for item in items:
            existing = by_key.get(item.key)
            if existing is None or item.importance >= existing.importance:
                by_key[item.key] = item
        return tuple(
            sorted(
                (item for item in by_key.values() if item.status == "active"),
                key=lambda item: (-item.importance, item.key),
            )[:10]
        )

    def merge_observations(self, observations) -> WorkingMemoryItem:
        targets = sorted(
            observation.artifact_reference
            for observation in observations
            if observation.artifact_reference
        )
        return WorkingMemoryItem(
            key="merged:file_content",
            type="module_summary",
            summary=f"Reviewed {len(targets)} related files",
            target=", ".join(targets),
            importance=60,
        )


class HistoryManager:
    """Maintains compact per-run working history without touching long-term Memory."""

    def __init__(self, summary_provider: SummaryProvider = None):
        self._summary_provider = summary_provider or RuleBasedSummaryProvider()

    def record(self, state, action, observation) -> dict:
        item = WorkingMemoryItem(
            key=(
                f"{observation.type}:{self._target(action)}"
                if observation.success
                else f"failure:{observation.type}:{self._target(action)}"
            ),
            type=observation.type,
            summary=self._summary(action, observation),
            target=self._target(action),
            importance=self._importance(action, observation),
        )
        state.working_memory_summary = list(
            self._summary_provider.summarize(
                tuple(state.working_memory_summary) + (item,)
            )
        )
        if observation.error and not any(
            issue.description == observation.error and issue.status == IssueStatus.OPEN
            for issue in state.issues
        ):
            state.issues.append(Issue.create(observation.error_category or observation.type, observation.error))
        if action.reason and action.reason not in state.decisions:
            state.decisions.append(action.reason)
            state.decisions[:] = state.decisions[-10:]
        working_summaries = tuple(item.summary for item in state.working_memory_summary)
        active_issue_descriptions = tuple(issue.description for issue in state.open_issues)
        return {
            "working_items": len(state.working_memory_summary),
            "active_issues": len(state.open_issues),
            "working_summary_lengths": [len(summary) for summary in working_summaries],
            "working_summary_hashes": [
                hashlib.sha256(summary.encode("utf-8")).hexdigest()
                for summary in working_summaries
            ],
            "observation_retained": item.key in {
                memory_item.key for memory_item in state.working_memory_summary
            },
            "active_issue_hashes": [
                hashlib.sha256(description.encode("utf-8")).hexdigest()
                for description in active_issue_descriptions
            ],
        }

    def resolve_issue(self, state, issue_id: str) -> None:
        state.resolve_issue(issue_id)

    def _importance(self, action, observation) -> int:
        if not observation.success:
            return 100
        if action.operation.kind in {OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE}:
            return 80
        if action.operation.kind is OperationKind.COMMAND_RUN:
            return 70
        if action.operation.kind in {OperationKind.FILE_READ, OperationKind.FILE_FIND, OperationKind.FILE_SEARCH}:
            return 20
        return 50

    @staticmethod
    def _target(action) -> str:
        return str(action.operation.fields.get("path", action.operation.fields.get("name_query", "")))

    @staticmethod
    def _summary(action, observation) -> str:
        summary = observation.summary or action.reason or action.operation.kind.value
        qualifiers = evidence_qualifiers(observation.metadata)
        for qualifier in qualifiers:
            if qualifier not in summary:
                summary = f"{qualifier}\n{summary}"
        paths = observation.metadata.get("paths")
        if action.operation.kind not in {OperationKind.FILE_FIND, OperationKind.FILE_SEARCH} or not isinstance(paths, list):
            return summary
        visible_paths = [path for path in paths[:10] if isinstance(path, str)]
        if not visible_paths:
            return summary
        return f"{summary}; paths: {', '.join(visible_paths)}"
