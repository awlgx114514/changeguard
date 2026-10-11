import json
from pathlib import Path
from typing import Any, Iterable, List

from .models import MemoryRule, Strategy


class MemoryStore:
    """JSON persistence for trace references, raw facts, and long-term rules."""

    def __init__(self, repo_path: Path):
        self._directory = Path(repo_path) / ".agent" / "memory"

    def record_decision(self, value: dict) -> None:
        self._append("decisions.json", value)

    def record_failure(self, value: dict) -> None:
        self._append("failures.json", value)

    def record_successful_pattern(self, value: dict) -> None:
        self._append("successful_patterns.json", value)

    def load_rules(self) -> List[MemoryRule]:
        rules = []
        for value in self.load("constraints.json"):
            try:
                rules.append(MemoryRule.from_dict(value))
            except (KeyError, TypeError, ValueError):
                continue
        return rules

    def save_rules(self, rules: Iterable[MemoryRule]) -> None:
        self._write("constraints.json", [rule.to_dict() for rule in rules])

    def load_strategies(self) -> List[Strategy]:
        strategies = []
        for value in self.load("strategies.json"):
            try:
                strategies.append(Strategy.from_dict(value))
            except (KeyError, TypeError, ValueError):
                continue
        return strategies

    def save_strategies(self, strategies: Iterable[Strategy]) -> None:
        self._write("strategies.json", [strategy.to_dict() for strategy in strategies])

    def load(self, name: str) -> List[Any]:
        path = self._directory / name
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _append(self, name: str, value: dict) -> None:
        values = self.load(name)
        values.append(value)
        self._write(name, values)

    def _write(self, name: str, values: list) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        (self._directory / name).write_text(
            json.dumps(values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
