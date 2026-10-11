from abc import ABC, abstractmethod
from pathlib import Path
from typing import Mapping


class Tool(ABC):
    """A descriptive, workspace-bounded runtime tool."""

    name: str
    description: str
    input_schema: dict

    @abstractmethod
    def execute(self, payload: Mapping[str, object], workspace: Path) -> dict:
        """Run a supported operation inside the supplied workspace."""
