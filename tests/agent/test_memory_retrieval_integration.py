from datetime import datetime, timezone

from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.planner import PlannerAgent
from code_governance.contract.schema import (
    ChangeContract,
    Constraints,
    Expected,
    Intent,
    Metadata,
    Verification,
    VerificationClaim,
)
from code_governance.memory.models import MemoryRule
from code_governance.memory.retriever import TaskContext
from code_governance.repo.analyzer import RepoContext


def rule(rule_id, scope, text):
    now = datetime(2026, 8, 23, tzinfo=timezone.utc).isoformat()
    return MemoryRule(
        id=rule_id,
        type="avoid_path",
        rule=text,
        scope=scope,
        confidence=0.8,
        source_runs=("run-1",),
        created_at=now,
        updated_at=now,
    )


class FakeRetriever:
    def __init__(self, selected):
        self.selected = selected

    def task_context(self, task, repo_context):
        return TaskContext(task, "channel_extension", (), (), ("channel",))

    def retrieve(self, task_context):
        return list(self.selected)


class MemorySource:
    def __init__(self, rules):
        self.rules = rules

    def active_rules(self):
        return tuple(self.rules)


def repo_context():
    return RepoContext(
        root=None,
        files=(),
        python_modules=(),
        git_status=(),
        core_files=(),
        module_relationships={},
    )


def contract():
    return ChangeContract(
        metadata=Metadata(),
        intent=Intent(goal="Add Channel"),
        expected=Expected(files=["channels.py"], change_level="L1"),
        constraints=Constraints(),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )


def test_contract_agent_applies_only_retrieved_memory_rules():
    selected = rule("selected", "core.py", "Avoid modifying core.py")
    unrelated = rule("other", "api.py", "Avoid modifying api.py")
    agent = ContractAgent(
        generator=lambda task, context: contract(),
        retriever=FakeRetriever([selected]),
    )

    result = agent.generate("Add Channel", repo_context(), MemorySource([selected, unrelated]))

    assert result.constraints.forbidden == ["core.py"]
    assert agent.last_retrieval.selected_rule_ids == ("selected",)


def test_planner_exposes_retrieved_memory_constraints():
    selected = rule("selected", "core.py", "Avoid modifying core.py")
    planner = PlannerAgent(retriever=FakeRetriever([selected]))

    plan = planner.plan("Add Channel", contract(), repo_context(), MemorySource([selected]))

    assert plan.memory_constraints == ("Avoid modifying core.py",)
    assert "Follow relevant Experience Memory constraints." in plan.steps
