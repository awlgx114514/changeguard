from abc import ABC, abstractmethod
import inspect
import re
from typing import Callable, Optional, Union

from code_governance.contract.schema import (
    ChangeContract,
    ClaimCoverage,
    Constraints,
    ExpectedChange,
    Metadata,
    Scope,
    Verification,
    VerificationClaim,
)
from code_governance.repo.analyzer import RepoContext
from code_governance.memory.retriever import MemoryRetrieval, MemoryRetriever
from code_governance.context.models import AgentContext as StaticAgentContext

from .base import Agent, AgentContext


class ContractGenerationError(ValueError):
    """Raised when a task cannot be converted into a Change Contract."""


class ContractGenerator(ABC):
    """Generates a schema-valid Contract from a task and repository facts."""

    @abstractmethod
    def generate(self, task: str, repo_context: RepoContext) -> ChangeContract:
        """Return a Contract without changing the target repository."""


class ContractAgent(Agent):
    """Converts task text to a Contract through an optional external generator."""

    def __init__(
        self,
        generator: Optional[Union[ContractGenerator, Callable[..., ChangeContract]]] = None,
        max_attempts: int = 2,
        retriever=None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._generator = generator
        self._max_attempts = max_attempts
        self._retriever = retriever
        self.last_retrieval = None

    def generate(
        self,
        task: Union[str, StaticAgentContext],
        repo_context: Optional[RepoContext] = None,
        memory=None,
    ) -> ChangeContract:
        if isinstance(task, StaticAgentContext):
            return self._generate_with_context(task)
        if not task.strip():
            raise ContractGenerationError("task file must not be empty")

        if self._generator is None:
            rules = self._retrieve(task, repo_context, memory)
            return self._apply_memory_rules(self._derive_contract(task, repo_context), rules)

        last_error = None
        for _ in range(self._max_attempts):
            try:
                contract = self._call_generator(task, repo_context)
            except (TypeError, ValueError) as error:
                last_error = error
                continue
            if isinstance(contract, ChangeContract):
                rules = self._retrieve(task, repo_context, memory)
                return self._apply_memory_rules(contract, rules)
            last_error = TypeError("ContractGenerator must return ChangeContract")

        raise ContractGenerationError(
            f"Contract generation failed after {self._max_attempts} attempts: {last_error}"
        )

    def run(self, context: AgentContext) -> ChangeContract:
        return self.generate(context.task, context.repo_context, context.memory)

    def _generate_with_context(self, context: StaticAgentContext) -> ChangeContract:
        task = context.task_context.task_text
        if not task.strip():
            raise ContractGenerationError("task file must not be empty")
        self.last_retrieval = None
        if self._generator is None:
            return self._apply_memory_rules(
                self._derive_contract(task, context.repository_context), context.memory_context
            )
        last_error = None
        for _ in range(self._max_attempts):
            try:
                contract = self._call_generator(task, context.repository_context)
            except (TypeError, ValueError) as error:
                last_error = error
                continue
            if isinstance(contract, ChangeContract):
                return self._apply_memory_rules(contract, context.memory_context)
            last_error = TypeError("ContractGenerator must return ChangeContract")
        raise ContractGenerationError(
            f"Contract generation failed after {self._max_attempts} attempts: {last_error}"
        )

    def _call_generator(
        self,
        task: str,
        repo_context: Optional[RepoContext],
    ) -> ChangeContract:
        generator = self._generator
        if isinstance(generator, ContractGenerator):
            return generator.generate(task, repo_context)
        if hasattr(generator, "generate"):
            return generator.generate(task, repo_context)
        parameter_count = len(inspect.signature(generator).parameters)
        if parameter_count == 1:
            return generator(task)
        return generator(task, repo_context)

    def _derive_contract(self, task: str, repo_context: Optional[RepoContext]) -> ChangeContract:
        """Build only Contract facts supported by task text and repository facts.

        This deterministic baseline deliberately requests clarification instead
        of fabricating paths or verification commands for underspecified work.
        """
        goal = task.strip()
        files = tuple(getattr(repo_context, "files", ()) or ())
        paths = self._scope_paths(goal, files)
        verification = self._verification(goal, files, paths)
        vague = self._is_underspecified(goal)
        needs_clarification = vague or not paths or not verification.claims
        non_goals = []
        if "without behavior change" in goal.lower() or "no behavior change" in goal.lower():
            non_goals.append("Do not change observable behavior.")
        changes = []
        if not needs_clarification:
            change_id = self._change_id(paths[0])
            changes = [
                ExpectedChange(
                    id=change_id,
                    description=self._expected_change(goal),
                    requirement="required",
                )
            ]
            verification = verification.model_copy(
                update={
                    "coverage": [
                        ClaimCoverage(
                            claim_id=verification.claims[0].id,
                            change_id=change_id,
                            coverage_type="direct",
                        )
                    ]
                }
            )
        scope = Scope(
            paths=paths if not needs_clarification else [],
            modules=[],
            change_level="L1",
            description=(f"Expected impact for: {goal}" if not needs_clarification else ""),
        )
        return ChangeContract(
            metadata=Metadata(),
            goal=goal,
            non_goals=non_goals,
            scope=scope,
            changes=changes,
            constraints=Constraints(forbidden=[]),
            verification=verification,
            needs_clarification=needs_clarification,
        )

    @staticmethod
    def _is_underspecified(task: str) -> bool:
        normalized = task.strip().lower().rstrip(".")
        return normalized in {
            "improve performance",
            "improve reliability",
            "make it better",
            "fix it",
        }

    @staticmethod
    def _explicit_paths(task: str, files) -> list[str]:
        known = set(files)
        candidates = re.findall(r"(?<!\w)(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+", task)
        return list(dict.fromkeys(path for path in candidates if path in known))

    def _scope_paths(self, task: str, files) -> list[str]:
        explicit = self._explicit_paths(task, files)
        if explicit:
            return [path for path in explicit if not path.startswith(("tests/", "test/"))] or explicit
        tokens = {token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", task)}
        source_files = [
            path for path in files
            if path.endswith(".py") and not path.startswith(("tests/", "test/"))
        ]
        matches = []
        for path in source_files:
            stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            if stem in tokens:
                matches.append(path)
        return list(dict.fromkeys(matches))

    def _verification(self, task: str, files, paths: list[str]) -> Verification:
        test_files = [path for path in files if path.startswith("tests/") and path.endswith(".py")]
        for path in paths:
            stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            matching = [test for test in test_files if stem in test]
            if matching:
                argv = ("uv", "run", "pytest", matching[0]) if "uv.lock" in files else ("pytest", matching[0])
                return Verification(
                    claims=[
                        VerificationClaim(
                            id=f"test_{stem}",
                            description=f"Targeted regression test for {path} passes.",
                            request={"argv": argv},
                        )
                    ]
                )
        return Verification()

    @staticmethod
    def _expected_change(goal: str) -> str:
        normalized = goal.strip().rstrip(".")
        match = re.match(r"fix\s+(.+?)\s+crash\s+on\s+(.+)", normalized, re.IGNORECASE)
        if match:
            return f"{match.group(1).capitalize()} handles {match.group(2)} without crashing."
        return normalized + "."

    @staticmethod
    def _change_id(path: str) -> str:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return f"change_{stem.lower()}"

    def _apply_memory_rules(self, contract: ChangeContract, rules) -> ChangeContract:
        if not rules:
            return contract
        forbidden = list(contract.constraints.forbidden)
        for rule in rules:
            if rule.type == "avoid_path" and rule.scope not in forbidden:
                forbidden.append(rule.scope)
        if forbidden == contract.constraints.forbidden:
            return contract
        return contract.model_copy(
            update={"constraints": Constraints(forbidden=forbidden)}
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
