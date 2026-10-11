from typing import Iterable, Tuple

from code_governance.memory.models import MemoryRule
from code_governance.memory.retriever import TaskContext
from code_governance.repo.analyzer import RepoContext

from .models import ContextFile, MemoryContext


class ContextSelector:
    """Selects bounded task context with deterministic rule scoring."""

    def select_files(
        self,
        task_context: TaskContext,
        repository_context: RepoContext,
        limit: int = 10,
    ) -> Tuple[ContextFile, ...]:
        if limit <= 0:
            return ()
        module_by_path = self._module_by_path(repository_context)
        target_files = {path.lower() for path in task_context.target_files}
        target_modules = {module.lower() for module in task_context.modules}
        ranked = []
        for path in repository_context.files:
            module = module_by_path.get(path, "")
            score = 0
            reasons = []
            if path.lower() in target_files:
                score += 5
                reasons.append("direct task file match")
            if module.lower() in target_modules:
                score += 3
                reasons.append("same task module")
            if score:
                ranked.append((score, path, module, "; ".join(reasons)))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            ContextFile(
                path=path,
                module=module,
                role=self._role(path, repository_context),
                summary=reason,
            )
            for _, path, module, reason in ranked[:limit]
        )

    def select_memories(
        self,
        task_context: TaskContext,
        rules: Iterable[MemoryRule],
        limit: int = 10,
    ) -> Tuple[MemoryContext, ...]:
        if limit <= 0:
            return ()
        ranked = []
        target_files = {path.lower() for path in task_context.target_files}
        target_modules = {module.lower() for module in task_context.modules}
        for rule in rules:
            scope = rule.scope.lower()
            score = 0
            reasons = []
            if scope == task_context.task_type.lower():
                score += 5
                reasons.append("task scope match")
            if scope in target_files or scope in target_modules:
                score += 5
                reasons.append("target scope match")
            rule_text = f"{rule.rule} {rule.scope}".lower()
            if any(keyword.lower() in rule_text for keyword in task_context.keywords):
                score += 1
                reasons.append("keyword match")
            if score:
                ranked.append((score * rule.confidence, rule.id, rule, "; ".join(reasons)))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            MemoryContext(
                id=rule.id,
                type=rule.type,
                rule=rule.rule,
                scope=rule.scope,
                reason=reason,
                confidence=rule.confidence,
            )
            for _, _, rule, reason in ranked[:limit]
        )

    def _module_by_path(self, repository_context: RepoContext) -> dict:
        python_paths = [path for path in repository_context.files if path.endswith(".py")]
        return dict(zip(python_paths, repository_context.python_modules))

    def _role(self, path: str, repository_context: RepoContext) -> str:
        if path in repository_context.core_files:
            return "core module"
        if path.endswith(".py"):
            return "Python module"
        return "project file"
