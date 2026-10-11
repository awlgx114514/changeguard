from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Iterable, Tuple

from .analyzer import FailureAnalyzer
from .models import FailureAnalysis, FailureRecord, MemoryRule, Strategy
from .resolver import MemoryResolver
from .store import MemoryStore


class MemoryManager:
    """Coordinates fact recording, analysis, rule resolution, and trace storage."""

    def __init__(
        self,
        repo_path: Path,
        analyzer: FailureAnalyzer = None,
        resolver: MemoryResolver = None,
        store: MemoryStore = None,
    ):
        self._repo_path = Path(repo_path)
        self._analyzer = analyzer or FailureAnalyzer()
        self._resolver = resolver or MemoryResolver()
        self._store = store or MemoryStore(repo_path)

    def load(self, name: str):
        return self._store.load(name)

    def active_rules(self) -> Tuple[MemoryRule, ...]:
        return tuple(rule for rule in self._store.load_rules() if rule.status == "active")

    def active_strategies(self) -> Tuple[Strategy, ...]:
        return tuple(
            strategy
            for strategy in self._store.load_strategies()
            if strategy.status == "active"
        )

    def record_failure(self, record: FailureRecord) -> FailureAnalysis:
        analysis = self._analyzer.analyze(record)
        self._store.record_failure(record.to_dict())
        self._write_analysis(record.run_id, analysis)
        candidate = self._candidate(record, analysis)
        self._store.save_rules(
            self._resolver.resolve(candidate, self._store.load_rules())
        )
        self._save_strategy(self._strategy_candidate(record, analysis))
        return analysis

    def record_decision(self, value: dict) -> None:
        self._store.record_decision(value)

    def confirm_success(
        self,
        run_id: str,
        changed_files: Iterable[str],
    ) -> Tuple[MemoryRule, ...]:
        changed = set(changed_files)
        confirmed = []
        updated = []
        for rule in self._store.load_rules():
            if rule.status == "active" and rule.scope and rule.scope not in changed:
                rule = MemoryRule(
                    id=rule.id,
                    type=rule.type,
                    rule=rule.rule,
                    scope=rule.scope,
                    confidence=min(1.0, rule.confidence + 0.05),
                    source_runs=tuple(sorted(set(rule.source_runs + (run_id,)))),
                    created_at=rule.created_at,
                    updated_at=self._now(),
                    status=rule.status,
                )
                confirmed.append(rule)
            updated.append(rule)
        self._store.save_rules(updated)
        if confirmed:
            self._store.record_successful_pattern(
                {
                    "type": "constraint_confirmed",
                    "run_id": run_id,
                    "rule_ids": [rule.id for rule in confirmed],
                }
            )
        return tuple(updated)

    def write_trace(self, run_id: str, trace: dict) -> Path:
        directory = self._repo_path / ".agent" / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "trace.json"
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _write_analysis(self, run_id: str, analysis: FailureAnalysis) -> Path:
        directory = self._repo_path / ".agent" / "analysis" / "failures"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{run_id}-{self._now().replace(':', '-')}.json"
        path.write_text(
            json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def _candidate(self, record: FailureRecord, analysis: FailureAnalysis) -> MemoryRule:
        finding = record.validator_findings[0] if record.validator_findings else {}
        scope = str(finding.get("path") or "repository")
        rule_text = f"Avoid modifying {scope} unless it is listed in the Change Contract."
        rule_id = hashlib.sha256(
            f"{analysis.failure_type}:{scope}".encode("utf-8")
        ).hexdigest()[:12]
        now = self._now()
        return MemoryRule(
            id=rule_id,
            type="avoid_path",
            rule=rule_text,
            scope=scope,
            confidence=analysis.confidence,
            source_runs=(record.run_id,),
            created_at=now,
            updated_at=now,
        )

    def _strategy_candidate(self, record: FailureRecord, analysis: FailureAnalysis) -> Strategy:
        finding = record.validator_findings[0] if record.validator_findings else {}
        scope = str(finding.get("path") or "repository")
        return Strategy(
            id=hashlib.sha256(
                f"strategy:{analysis.failure_type}:{scope}".encode("utf-8")
            ).hexdigest()[:12],
            strategy=(
                f"Before executing, inspect {scope} and its focused tests before "
                "modifying the implementation."
            ),
            source_failure=analysis.failure_type,
            confidence=analysis.confidence,
            status="active",
            scope=scope,
        )

    def _save_strategy(self, candidate: Strategy) -> None:
        strategies = self._store.load_strategies()
        updated = [strategy for strategy in strategies if strategy.id != candidate.id]
        updated.append(candidate)
        self._store.save_strategies(updated)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
