from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from code_governance.contract.schema import ChangeContract


@dataclass(frozen=True)
class CodingAgentResult:
    stdout: str
    stderr: str
    returncode: Optional[int]
    prompt: str = ""

    @property
    def success(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class AgentContext:
    task: str
    repo_context: Any = None
    contract: Optional[ChangeContract] = None
    plan: Any = None
    workspace: Optional[Path] = None
    report: Any = None
    memory: Any = None


class Agent(ABC):
    """Shared adapter boundary for Harness agents."""

    @abstractmethod
    def run(self, context: AgentContext) -> Any:
        """Execute one bounded agent operation."""


class CodingAgent(ABC):
    """Canonical provider boundary: one model decision produces one Action."""

    @abstractmethod
    def decide(self, state: Any, context: Any):
        """Return the next Runtime Action; execution belongs to AgentLoop."""
