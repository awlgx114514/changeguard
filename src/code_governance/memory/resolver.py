from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable, Tuple

from .models import MemoryRule


class MemoryResolver:
    """Merges duplicate rules and retains deprecated conflict history."""

    def resolve(self, candidate: MemoryRule, existing: Iterable[MemoryRule]) -> Tuple[MemoryRule, ...]:
        rules = list(existing)
        matching = next(
            (
                rule
                for rule in rules
                if rule.type == candidate.type
                and rule.scope == candidate.scope
                and rule.status == "active"
            ),
            None,
        )
        if matching is None:
            return tuple(rules + [candidate])
        if self._conflicts(matching, candidate):
            winner, loser = self._winner(matching, candidate)
            winner = replace(winner, status="active", updated_at=self._now())
            loser = replace(loser, status="deprecated", updated_at=self._now())
            return tuple(rule for rule in rules if rule.id != matching.id) + (winner, loser)

        merged = replace(
            matching,
            confidence=min(1.0, max(matching.confidence, candidate.confidence) + 0.1),
            source_runs=tuple(sorted(set(matching.source_runs + candidate.source_runs))),
            updated_at=self._now(),
        )
        return tuple(merged if rule.id == matching.id else rule for rule in rules)

    def deprecate(self, rule: MemoryRule) -> MemoryRule:
        return replace(rule, status="deprecated", updated_at=self._now())

    def _conflicts(self, first: MemoryRule, second: MemoryRule) -> bool:
        return self._polarity(first.rule) != self._polarity(second.rule)

    def _polarity(self, rule: str) -> str:
        value = rule.lower()
        if any(marker in value for marker in ("allow", "允许", "可以修改")):
            return "allow"
        return "avoid"

    def _winner(self, first: MemoryRule, second: MemoryRule):
        first_score = (first.confidence, len(first.source_runs), first.updated_at)
        second_score = (second.confidence, len(second.source_runs), second.updated_at)
        return (first, second) if first_score >= second_score else (second, first)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
