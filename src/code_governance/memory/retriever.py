from dataclasses import dataclass
import re
from typing import Iterable, List, Tuple

from code_governance.repo.analyzer import RepoContext

from .models import MemoryRule, Strategy


@dataclass(frozen=True)
class TaskContext:
    task_text: str
    task_type: str
    target_files: Tuple[str, ...]
    modules: Tuple[str, ...]
    keywords: Tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "task_text": self.task_text,
            "task_type": self.task_type,
            "target_files": list(self.target_files),
            "modules": list(self.modules),
            "keywords": list(self.keywords),
        }


@dataclass(frozen=True)
class MemoryRetrieval:
    query: TaskContext
    selected_rules: Tuple[MemoryRule, ...]
    selected_strategies: Tuple[Strategy, ...] = ()

    @property
    def selected_rule_ids(self) -> Tuple[str, ...]:
        return tuple(rule.id for rule in self.selected_rules)

    def to_trace(self) -> dict:
        return {
            "query": self.query.to_dict(),
            "selected_memories": list(self.selected_rule_ids),
            "selected_strategies": [strategy.id for strategy in self.selected_strategies],
        }


class MemoryRetriever:
    """Ranks active Experience Memory rules using structured task context."""

    _TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*|[\u4e00-\u9fff]+")

    def __init__(self, memory_source):
        self._memory_source = memory_source

    def retrieve(self, task_context: TaskContext, limit: int = 5) -> List[MemoryRule]:
        if limit <= 0:
            return []
        ranked = []
        for rule in self._memory_source.active_rules():
            score = self._score(rule, task_context)
            if score == 0:
                continue
            ranked.append((score * rule.confidence, rule.confidence, rule.id, rule))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [item[3] for item in ranked[:limit]]

    def retrieve_strategies(self, task_context: TaskContext, limit: int = 5) -> List[Strategy]:
        if limit <= 0 or not hasattr(self._memory_source, "active_strategies"):
            return []
        ranked = []
        for strategy in self._memory_source.active_strategies():
            score = self._score(strategy, task_context)
            if score:
                ranked.append((score * strategy.confidence, strategy.confidence, strategy.id, strategy))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [item[3] for item in ranked[:limit]]

    def retrieve_context(self, task_text: str, repo_context: RepoContext, limit: int = 10) -> MemoryRetrieval:
        context = self.task_context(task_text, repo_context)
        return MemoryRetrieval(
            query=context,
            selected_rules=tuple(self.retrieve(context, limit)),
            selected_strategies=tuple(self.retrieve_strategies(context, limit)),
        )

    def task_context(self, task_text: str, repo_context: RepoContext) -> TaskContext:
        keywords = tuple(self._keywords(task_text))
        target_files = tuple(
            path
            for path in repo_context.files
            if path.lower() in task_text.lower()
        )
        modules = tuple(
            module
            for module in repo_context.python_modules
            if module.lower() in task_text.lower()
        )
        if not modules:
            modules = tuple(
                self._module_for_path(path, repo_context)
                for path in target_files
            )
            modules = tuple(module for module in modules if module)
        return TaskContext(
            task_text=task_text.strip(),
            task_type=self._task_type(task_text),
            target_files=target_files,
            modules=modules,
            keywords=keywords,
        )

    def _score(self, rule, task_context: TaskContext) -> int:
        score = 0
        scope = rule.scope.lower()
        if scope == task_context.task_type.lower():
            score += 5
        if scope in {path.lower() for path in task_context.target_files}:
            score += 3
        if scope in {module.lower() for module in task_context.modules}:
            score += 3
        text = getattr(rule, "rule", getattr(rule, "strategy", ""))
        rule_text = f"{text} {rule.scope}".lower()
        if any(keyword.lower() in rule_text for keyword in task_context.keywords):
            score += 1
        return score

    def _task_type(self, task_text: str) -> str:
        value = task_text.lower()
        if "channel" in value or "渠道" in task_text:
            return "channel_extension"
        if "bug" in value or "fix" in value or "修复" in task_text:
            return "bugfix"
        if "api" in value or "接口" in task_text:
            return "api_change"
        return "general"

    def _keywords(self, task_text: str) -> Iterable[str]:
        return sorted(
            {
                token.lower()
                for token in self._TOKEN_PATTERN.findall(task_text)
                if len(token) > 1
            }
        )

    def _module_for_path(self, path: str, repo_context: RepoContext) -> str:
        try:
            index = list(repo_context.files).index(path)
        except ValueError:
            return ""
        python_files = [item for item in repo_context.files if item.endswith(".py")]
        if path not in python_files:
            return ""
        module_index = python_files.index(path)
        return repo_context.python_modules[module_index]
