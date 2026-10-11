from typing import Iterable, Optional
from dataclasses import replace
from pathlib import Path

from code_governance.memory.retriever import MemoryRetriever
from code_governance.repo.analyzer import RepoContext
from code_governance.runtime.action import Action

from .compressor import ContextCompressor
from .models import AgentContext, StrategyContext
from .selector import ContextSelector


class ContextBuilder:
    """Combines already available task, repository, and Memory facts once."""

    def __init__(
        self,
        selector: Optional[ContextSelector] = None,
        compressor: Optional[ContextCompressor] = None,
    ):
        self._selector = selector or ContextSelector()
        self._compressor = compressor or ContextCompressor()

    def build(
        self,
        task: str,
        repository_context: RepoContext,
        memory_retriever,
        project_rules: Iterable[str] = (),
        tool_registry=None,
        agent_state=None,
    ) -> AgentContext:
        retriever = self._retriever(memory_retriever)
        if hasattr(retriever, "retrieve_context"):
            retrieval = retriever.retrieve_context(task, repository_context, limit=10)
            task_context = retrieval.query
            candidate_rules = retrieval.selected_rules
            candidate_strategies = retrieval.selected_strategies
        else:
            task_context = retriever.task_context(task, repository_context)
            candidate_rules = retriever.retrieve(task_context, limit=10)
            candidate_strategies = ()
        memory_context = self._selector.select_memories(task_context, candidate_rules)
        constraints = self._unique(
            tuple(project_rules) + tuple(item.rule for item in memory_context)
        )
        context = AgentContext(
            version="v1",
            task_context=task_context,
            repository_context=repository_context,
            memory_context=memory_context,
            project_rules=tuple(project_rules),
            relevant_files=self._selector.select_files(task_context, repository_context),
            constraints=constraints,
            strategies=tuple(
                StrategyContext(
                    id=strategy.id,
                    strategy=strategy.strategy,
                    source_failure=strategy.source_failure,
                    confidence=strategy.confidence,
                    reason="relevant Strategy retrieval",
                )
                for strategy in candidate_strategies[:10]
            ),
            available_tools=(
                tuple(tool_registry.list_tools()) if tool_registry is not None else ()
            ),
            available_operations=Action.operation_descriptions(),
            current_goal=(getattr(agent_state, "current_goal", "") or task),
            working_memory=tuple(
                item.summary
                for item in getattr(agent_state, "working_memory_summary", ())
            )[:10],
            active_issues=tuple(
                issue.description for issue in getattr(agent_state, "open_issues", ())
            )[:10],
            decisions=tuple(getattr(agent_state, "decisions", ()))[:10],
        )
        return self._compressor.compress(context)

    def _retriever(self, source):
        if hasattr(source, "task_context") and hasattr(source, "retrieve"):
            return source
        return MemoryRetriever(source)

    def _unique(self, values):
        return tuple(dict.fromkeys(value for value in values if value))

    def with_state(self, context: AgentContext, state) -> AgentContext:
        """Inject current-run facts and the active workspace without rebuilding Memory."""
        if not isinstance(context, AgentContext):
            return context
        workspace_reference = getattr(state, "workspace_reference", "")
        repository_context = context.repository_context
        if workspace_reference:
            repository_context = replace(repository_context, root=Path(workspace_reference))
        return replace(
            context,
            repository_context=repository_context,
            current_goal=getattr(state, "current_goal", context.current_goal),
            working_memory=tuple(
                item.summary for item in getattr(state, "working_memory_summary", ())
            )[:10],
            active_issues=tuple(
                issue.description for issue in getattr(state, "open_issues", ())
            )[:10],
            decisions=tuple(getattr(state, "decisions", ()))[:10],
        )

    def with_contract(self, context: AgentContext, contract) -> AgentContext:
        """Attach the validated expected-change Contract to runtime Context."""
        if not isinstance(context, AgentContext):
            return context
        payload = contract.model_dump(mode="json") if hasattr(contract, "model_dump") else contract
        return replace(context, contract=payload)

    def with_repair_context(self, context: AgentContext, failures) -> AgentContext:
        """Expose bounded validation facts, without generating implementation advice."""
        if not isinstance(context, AgentContext):
            return context
        return replace(context, repair_context=tuple(failures))
