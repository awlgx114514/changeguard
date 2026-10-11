import json
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timezone

from .events import EVENT_TYPES
from .models import Trace, TraceEvent


class TraceWriter:
    """Persists a compact event trace beneath the repository run directory."""

    def __init__(self, repo_path: Path):
        self._repo_path = Path(repo_path)

    def write(self, trace: Trace) -> Path:
        directory = self._repo_path / ".agent" / "runs" / trace.run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "trace.json"
        path.write_text(json.dumps(trace.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path


class TraceManager:
    """Collects factual events and optionally writes them at task completion."""

    def __init__(
        self,
        run_id: str,
        writer: Optional[TraceWriter] = None,
        runtime_version: str = "runtime-v1",
        context_version: Optional[str] = None,
    ):
        self._run_id = run_id
        self._writer = writer
        self._runtime_version = runtime_version
        self._context_version = context_version
        self._events = []

    def emit(self, event_type: str, data: dict) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported trace event: {event_type}")
        self._events.append(
            TraceEvent(
                event_type,
                dict(data),
                sequence=len(self._events) + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

    def get_events(self) -> Tuple[TraceEvent, ...]:
        return tuple(self._events)

    def set_context_version(self, context_version: str) -> None:
        self._context_version = context_version

    def write(self) -> Path:
        if self._writer is None:
            raise ValueError("TraceManager has no writer")
        return self._writer.write(
            Trace(
                self._run_id,
                self.get_events(),
                runtime_version=self._runtime_version,
                context_version=self._context_version,
            )
        )
