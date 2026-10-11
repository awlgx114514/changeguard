from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class TraceEvent:
    type: str
    data: dict
    sequence: int = 0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "data": dict(self.data),
            "sequence": self.sequence,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class Trace:
    run_id: str
    events: Tuple[TraceEvent, ...]
    schema_version: str = "v2"
    runtime_version: str = "runtime-v1"
    context_version: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "runtime_version": self.runtime_version,
            "context_version": self.context_version,
            "events": [event.to_dict() for event in self.events],
        }
