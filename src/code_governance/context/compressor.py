from dataclasses import replace

from .models import AgentContext, ContextFile, MemoryContext, StrategyContext


class ContextCompressor:
    """Bounds static Context text without generating or interpreting it."""

    def __init__(self, text_limit: int = 240, file_limit: int = 10, memory_limit: int = 10):
        if text_limit < 1 or file_limit < 0 or memory_limit < 0:
            raise ValueError("context limits must be non-negative and text_limit positive")
        self._text_limit = text_limit
        self._file_limit = file_limit
        self._memory_limit = memory_limit

    def compress(self, context: AgentContext) -> AgentContext:
        return replace(
            context,
            relevant_files=tuple(
                self._file(item)
                for item in context.relevant_files[: self._file_limit]
            ),
            memory_context=tuple(
                self._memory(item)
                for item in context.memory_context[: self._memory_limit]
            ),
            strategies=tuple(self._strategy(item) for item in context.strategies[: self._memory_limit]),
            project_rules=tuple(self._shorten(rule) for rule in context.project_rules),
            constraints=tuple(self._shorten(rule) for rule in context.constraints),
        )

    def _file(self, item: ContextFile) -> ContextFile:
        return replace(
            item,
            role=self._shorten(item.role),
            summary=self._shorten(item.summary),
        )

    def _memory(self, item: MemoryContext) -> MemoryContext:
        return replace(
            item,
            rule=self._shorten(item.rule),
            reason=self._shorten(item.reason),
        )

    def _strategy(self, item: StrategyContext) -> StrategyContext:
        return replace(
            item,
            strategy=self._shorten(item.strategy),
            reason=self._shorten(item.reason),
        )

    def _shorten(self, value: str) -> str:
        if len(value) <= self._text_limit:
            return value
        return value[: self._text_limit - 1].rstrip() + "…"
