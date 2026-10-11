from dataclasses import dataclass
from typing import Optional, Tuple

from code_governance.contract.schema import ChangeContract
from code_governance.repo.analyzer import RepoContext
from code_governance.memory.retriever import MemoryRetrieval, MemoryRetriever
from code_governance.context.models import AgentContext as StaticAgentContext

from .base import Agent, AgentContext


@dataclass(frozen=True)
class Plan:
    steps: Tuple[str, ...]
    memory_constraints: Tuple[str, ...] = ()
    context_constraints: Tuple[str, ...] = ()
    strategy_guidance: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "steps": list(self.steps),
            "memory_constraints": list(self.memory_constraints),
            "context_constraints": list(self.context_constraints),
            "strategy_guidance": list(self.strategy_guidance),
        }


class PlannerAgent(Agent):
    """Produces a small, serializable plan from known repository facts."""

    def __init__(self, retriever=None):
        self._retriever = retriever
        self.last_retrieval = None

    def plan(
        self,
        task: object,
        contract: ChangeContract,
        repo_context: Optional[RepoContext] = None,
        memory=None,
    ) -> Plan:
        if isinstance(task, StaticAgentContext):
            return self._plan_with_context(task, contract)
        expected_files = ", ".join(contract.scope.paths) or "the Contract scope"
        steps = []
        rules = self._retrieve(task, repo_context, memory)
        if rules:
            steps.append("Follow relevant Experience Memory constraints.")
        steps.extend(
            (
                f"Review repository context for: {task.strip()}",
                f"Modify only: {expected_files}",
                "Add or update focused tests for the requested behavior.",
                "Run the Contract verification commands.",
            )
        )
        return Plan(
            steps=tuple(steps),
            memory_constraints=tuple(rule.rule for rule in rules),
        )

    def run(self, context: AgentContext) -> Plan:
        return self.plan(context.task, context.contract, context.repo_context, context.memory)

    def _plan_with_context(
        self,
        context: StaticAgentContext,
        contract: ChangeContract,
    ) -> Plan:
        expected_files = ", ".join(contract.scope.paths) or "the Contract scope"
        steps = []
        if context.constraints:
            steps.append("Keep context constraints in scope.")
        if context.strategies:
            steps.append("Apply selected execution strategies before modifying files.")
        steps.extend(
            (
                f"Review repository context for: {context.task_context.task_text}",
                f"Modify only: {expected_files}",
                "Add or update focused tests for the requested behavior.",
                "Run the Contract verification commands.",
            )
        )
        self.last_retrieval = None
        return Plan(
            steps=tuple(steps),
            memory_constraints=tuple(item.rule for item in context.memory_context),
            context_constraints=context.constraints,
            strategy_guidance=tuple(item.strategy for item in context.strategies),
        )

    def _retrieve(self, task, repo_context, memory):
        self.last_retrieval = None
        if memory is None or repo_context is None:
            return ()
        retriever = self._retriever or MemoryRetriever(memory)
        context = retriever.task_context(task, repo_context)
        rules = tuple(retriever.retrieve(context))
        self.last_retrieval = MemoryRetrieval(context, rules)
        return rules
