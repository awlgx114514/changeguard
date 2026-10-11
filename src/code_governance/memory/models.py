from dataclasses import dataclass
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class FailureRecord:
    """Raw facts observed when one Harness run fails validation."""

    run_id: str
    timestamp: str
    task: str
    validator_findings: Tuple[Dict[str, Any], ...]
    changed_files: Tuple[str, ...]
    contract_summary: Dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "task": self.task,
            "validator_findings": [dict(item) for item in self.validator_findings],
            "changed_files": list(self.changed_files),
            "contract_summary": dict(self.contract_summary),
        }


@dataclass(frozen=True)
class FailureAnalysis:
    """A rule-based explanation derived from one FailureRecord."""

    failure_type: str
    symptom: str
    root_cause: str
    reason: str
    prevention: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "failure_type": self.failure_type,
            "symptom": self.symptom,
            "root_cause": self.root_cause,
            "reason": self.reason,
            "prevention": self.prevention,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class MemoryRule:
    """A long-term, reviewable constraint learned from validated outcomes."""

    id: str
    type: str
    rule: str
    scope: str
    confidence: float
    source_runs: Tuple[str, ...]
    created_at: str
    updated_at: str
    status: str = "active"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "rule": self.rule,
            "scope": self.scope,
            "confidence": self.confidence,
            "source_runs": list(self.source_runs),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "MemoryRule":
        return cls(
            id=str(value["id"]),
            type=str(value["type"]),
            rule=str(value["rule"]),
            scope=str(value["scope"]),
            confidence=float(value["confidence"]),
            source_runs=tuple(str(item) for item in value["source_runs"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            status=str(value.get("status", "active")),
        )


@dataclass(frozen=True)
class Strategy:
    """A reviewable execution behavior learned from a validated failure."""

    id: str
    strategy: str
    source_failure: str
    confidence: float
    status: str
    scope: str = "general"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "strategy": self.strategy,
            "source_failure": self.source_failure,
            "confidence": self.confidence,
            "status": self.status,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Strategy":
        return cls(
            id=str(value["id"]),
            strategy=str(value["strategy"]),
            source_failure=str(value["source_failure"]),
            confidence=float(value["confidence"]),
            status=str(value.get("status", "active")),
            scope=str(value.get("scope", "general")),
        )
