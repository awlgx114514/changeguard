from pathlib import Path

from code_governance.runtime.action import Action, OperationKind
from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.codex_agent import CodexAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.agent.planner import PlannerAgent
from code_governance.context.builder import ContextBuilder
from code_governance.context.compressor import ContextCompressor
from code_governance.context.models import AgentContext, ContextFile, MemoryContext
from code_governance.context.selector import ContextSelector
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
from code_governance.memory.models import Strategy
from code_governance.memory.retriever import TaskContext
from code_governance.repo.analyzer import RepoContext
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.registry import ToolRegistry
from code_governance.tools.approval import ApprovalOutcome
from code_governance.runtime.history import Issue, IssueStatus, WorkingMemoryItem
from code_governance.runtime.state import AgentState


def repo_context() -> RepoContext:
    return RepoContext(
        root=Path("/repository"),
        files=(
            "src/demo/channels.py",
            "src/demo/core.py",
            "tests/test_channels.py",
            "README.md",
        ),
        python_modules=("src.demo.channels", "src.demo.core", "tests.test_channels"),
        git_status=(),
        core_files=("src/demo/core.py",),
        module_relationships={},
    )


def task_context() -> TaskContext:
    return TaskContext(
        task_text="Extend src/demo/channels.py with a GitHub channel",
        task_type="channel_extension",
        target_files=("src/demo/channels.py",),
        modules=("src.demo.channels",),
        keywords=("channel", "github"),
    )


def memory_rule() -> MemoryRule:
    return MemoryRule(
        id="rule-1",
        type="avoid_path",
        rule="Do not modify core.py while extending channels.",
        scope="channel_extension",
        confidence=0.9,
        source_runs=("run-1",),
        created_at="2026-08-24T00:00:00+00:00",
        updated_at="2026-08-24T00:00:00+00:00",
    )


class MemorySource:
    def active_rules(self):
        return (memory_rule(),)

    def active_strategies(self):
        return (
            Strategy(
                id="strategy-1",
                strategy="Inspect channels.py before implementation.",
                source_failure="scope_violation",
                confidence=0.8,
                status="active",
                scope="channel_extension",
            ),
        )


class ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def contract() -> ChangeContract:
    return ChangeContract(
        metadata=Metadata(project="demo"),
        intent=Intent(goal="extend the channel"),
        expected=Expected(files=["src/demo/channels.py"], change_level="L1"),
        constraints=Constraints(forbidden=[]),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )


def test_context_builder_creates_serializable_context():
    context = ContextBuilder().build(
        task_context().task_text,
        repo_context(),
        MemorySource(),
        project_rules=("Keep the public Channel interface compatible.",),
    )

    assert context.version == "v1"
    assert context.task_context.task_text == task_context().task_text
    assert context.task_context.target_files == ("src/demo/channels.py",)
    assert [item.path for item in context.relevant_files] == ["src/demo/channels.py"]
    assert [item.id for item in context.memory_context] == ["rule-1"]
    assert [item.id for item in context.strategies] == ["strategy-1"]
    assert context.available_tools == ()
    assert context.to_dict()["repository_context"]["root"] == str(Path("/repository"))


def test_selector_filters_unrelated_files_and_applies_limits():
    selector = ContextSelector()

    selected = selector.select_files(task_context(), repo_context(), limit=1)

    assert [item.path for item in selected] == ["src/demo/channels.py"]


def test_context_builder_includes_registered_tool_names():
    registry = ToolRegistry()
    registry.register(FilesystemTool())

    context = ContextBuilder().build(
        task_context().task_text,
        repo_context(),
        MemorySource(),
        tool_registry=registry,
    )

    assert context.available_tools == ("filesystem",)


def test_context_builder_injects_only_compressed_runtime_working_memory():
    state = AgentState.initialize(task_context().task_text, {}, {})
    state.working_memory_summary.append(
        WorkingMemoryItem(
            key="file_content:src/demo/channels.py",
            type="file_content",
            summary="Read src/demo/channels.py",
            target="src/demo/channels.py",
            importance=20,
        )
    )
    state.issues.append(
        Issue("issue-1", "runtime_error", "missing channel registration", IssueStatus.OPEN)
    )
    state.decisions.append("Inspect the channel interface first")

    context = ContextBuilder().build(
        task_context().task_text,
        repo_context(),
        MemorySource(),
        agent_state=state,
    )

    assert context.current_goal == task_context().task_text
    assert context.working_memory == ("Read src/demo/channels.py",)
    assert context.active_issues == ("missing channel registration",)
    assert context.decisions == ("Inspect the channel interface first",)

    prompt = CodexAgent()._build_prompt(context.task_context.task_text, contract(), None, context)

    assert "Working Memory:" in prompt
    assert "Read src/demo/channels.py" in prompt
    assert "Active Issues:" in prompt
    assert "missing channel registration" in prompt


def test_runtime_context_uses_the_active_workspace_as_its_repository_root(tmp_path):
    context = ContextBuilder().build(task_context().task_text, repo_context(), MemorySource())
    state = AgentState.initialize(task_context().task_text, workspace=tmp_path)

    runtime_context = ContextBuilder().with_state(context, state)

    assert runtime_context.repository_context.root == tmp_path


def test_compressor_reduces_file_and_memory_text():
    repeated = "important context " * 30
    context = AgentContext(
        version="v1",
        task_context=task_context(),
        repository_context=repo_context(),
        memory_context=(
            MemoryContext(
                id="rule-1",
                type="constraint",
                rule=repeated,
                scope="general",
                reason=repeated,
                confidence=0.8,
            ),
        ),
        project_rules=(),
        relevant_files=(
            ContextFile(
                path="src/demo/channels.py",
                module="src.demo.channels",
                role=repeated,
                summary=repeated,
            ),
        ),
        constraints=(),
    )

    compressed = ContextCompressor(text_limit=40).compress(context)

    assert len(compressed.relevant_files[0].role) <= 40
    assert len(compressed.relevant_files[0].summary) <= 40
    assert len(compressed.memory_context[0].rule) <= 40
    assert len(compressed.memory_context[0].reason) <= 40


def test_contract_agent_uses_prebuilt_context_without_retrieval():
    class FailingRetriever:
        def task_context(self, *args):
            raise AssertionError("ContractAgent must not retrieve context")

    context = ContextBuilder().build(task_context().task_text, repo_context(), MemorySource())

    result = ContractAgent(
        generator=lambda task, repo: contract(),
        retriever=FailingRetriever(),
    ).generate(context)

    assert result.constraints.forbidden == ["channel_extension"]


def test_planner_uses_prebuilt_context_without_retrieval():
    class FailingRetriever:
        def task_context(self, *args):
            raise AssertionError("PlannerAgent must not retrieve context")

    context = ContextBuilder().build(task_context().task_text, repo_context(), MemorySource())

    plan = PlannerAgent(retriever=FailingRetriever()).plan(context, contract())

    assert plan.memory_constraints == (memory_rule().rule,)
    assert "Keep context constraints in scope." in plan.steps
    assert plan.context_constraints


def test_codex_prompt_uses_compact_static_context():
    registry = ToolRegistry()
    registry.register(FilesystemTool())
    context = ContextBuilder().build(
        task_context().task_text,
        repo_context(),
        MemorySource(),
        tool_registry=registry,
    )

    prompt = CodexAgent()._build_prompt(context.task_context.task_text, contract(), None, context)

    assert "Context:" in prompt
    assert "Relevant Files:" in prompt
    assert "src/demo/channels.py" in prompt
    assert "[rule-1]" in prompt
    assert "Strategies:" in prompt
    assert "[strategy-1]" in prompt
    assert "Available Tools:" in prompt
    assert "- filesystem" in prompt
    assert "src/demo/core.py" not in prompt


class ContextAwareWritingAgent:
    def __init__(self):
        self.contexts = []

    def decide(self, state, context):
        self.contexts.append(context)
        if len(self.contexts) == 1:
            import hashlib
            path = Path(state.workspace_reference) / "src" / "demo" / "core.py"
            return Action.create_operation(
                OperationKind.FILE_WRITE,
                path="src/demo/core.py",
                content="VALUE = 2\n",
                observed_file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return Action.create_operation(OperationKind.WORKFLOW_FINISH)


def test_harness_records_compact_context_event(tmp_path):
    from tests.agent.test_harness_v2 import prepare_repo

    repo = prepare_repo(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Update src/demo/core.py", encoding="utf-8")
    agent = ContextAwareWritingAgent()
    orchestrator = HarnessOrchestrator(
        contract_agent=ContractAgent(
            generator=lambda task, repo: ChangeContract(
                metadata=Metadata(project="demo"),
                intent=Intent(goal=task),
                expected=Expected(files=["src/demo/core.py"], change_level="L1"),
                constraints=Constraints(forbidden=[]),
                verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
            )
        ),
        coding_agent=agent,
        approval_provider=ApprovedProvider(),
    )

    result = orchestrator.run(task_file, repo)
    trace_path = next((repo / ".agent" / "runs").glob("*/trace.json"))
    trace = __import__("json").loads(trace_path.read_text(encoding="utf-8"))

    assert result.success is True
    assert agent.contexts[0].version == "v1"
    context_event = next(event for event in trace["events"] if event["type"] == "context_created")
    assert context_event["data"] == {
        "version": "v1",
        "files": ["src/demo/core.py"],
        "memories": [],
        "strategies": [],
        "tools": ["filesystem", "git", "shell"],
    }
