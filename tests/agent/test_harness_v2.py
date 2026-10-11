import json
from pathlib import Path
import subprocess

from code_governance.agent.contract_agent import ContractAgent, ContractGenerator
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.agent.orchestrator import Orchestrator
from code_governance.agent.planner import PlannerAgent
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
from code_governance.repo.analyzer import RepositoryAnalyzer
from code_governance.trace.writer import TraceManager
from code_governance.workspace.manager import WorkspaceManager
from code_governance.memory.models import MemoryRule
from code_governance.runtime.action import Action, ActionType
from code_governance.tools import ApprovalOutcome


def run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def prepare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    package = repo / "src" / "demo"
    package.mkdir(parents=True)
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test User")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text(
        "from demo import helper\n",
        encoding="utf-8",
    )
    (package / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "initial")
    return repo


def make_contract(expected_files=("src/demo/core.py",), forbidden=()):
    claim = VerificationClaim(
        id="diff_check",
        description="The workspace diff is structurally valid.",
        request={"argv": ("git", "diff", "--check")},
    )
    return ChangeContract(
        metadata=Metadata(project="demo"),
        goal="update the demo module",
        scope=Scope(
            paths=list(expected_files),
            change_level="L1",
            description="Expected impact in the demo module.",
        ),
        changes=[
            ExpectedChange(
                id="demo_module_behavior",
                description="The requested demo module behavior is updated.",
            )
        ],
        constraints=Constraints(forbidden=list(forbidden)),
        verification=Verification(
            claims=[claim],
            coverage=[
                ClaimCoverage(
                    claim_id=claim.id,
                    change_id="demo_module_behavior",
                    coverage_type="direct",
                )
            ],
        ),
    )


def test_repository_analyzer_writes_basic_repo_map(tmp_path):
    repo = prepare_repo(tmp_path)

    context = RepositoryAnalyzer().analyze(repo)

    assert "src/demo/core.py" in context.files
    assert "src.demo.core" in context.python_modules
    assert "src/demo/core.py" in context.core_files
    assert context.module_relationships["src/demo/core.py"] == ("demo",)
    repo_map = repo / ".agent" / "repo-map.json"
    assert json.loads(repo_map.read_text(encoding="utf-8"))["root"] == str(repo)


class RecordingGenerator(ContractGenerator):
    def __init__(self, contract):
        self.contract = contract
        self.contexts = []

    def generate(self, task, repo_context):
        self.contexts.append(repo_context)
        return self.contract


def test_contract_generator_receives_repo_context_and_planner_serializes(tmp_path):
    repo_context = RepositoryAnalyzer().analyze(prepare_repo(tmp_path))
    generator = RecordingGenerator(make_contract())

    contract = ContractAgent(generator=generator).generate(
        "Update the core module",
        repo_context,
    )
    plan = PlannerAgent().plan("Update the core module", contract, repo_context)

    assert generator.contexts == [repo_context]
    assert plan.steps
    assert plan.to_dict()["steps"] == list(plan.steps)


def test_planner_uses_relevant_active_memory_when_available(tmp_path):
    repo_context = RepositoryAnalyzer().analyze(prepare_repo(tmp_path))

    class Memory:
        def active_rules(self):
            return (
                MemoryRule(
                    id="rule-1",
                    type="avoid_path",
                    rule="Avoid modifying core.py",
                    scope="general",
                    confidence=0.8,
                    source_runs=("run-1",),
                    created_at="2026-08-23T00:00:00+00:00",
                    updated_at="2026-08-23T00:00:00+00:00",
                ),
            )

    plan = PlannerAgent().plan(
        "Update the core module",
        make_contract(),
        repo_context,
        Memory(),
    )

    assert plan.memory_constraints == ("Avoid modifying core.py",)


def test_contract_agent_retries_a_failed_generator(tmp_path):
    repo_context = RepositoryAnalyzer().analyze(prepare_repo(tmp_path))
    contract = make_contract()

    class RetryingGenerator(ContractGenerator):
        def __init__(self):
            self.attempts = 0

        def generate(self, task, context):
            self.attempts += 1
            if self.attempts == 1:
                raise ValueError("invalid generated contract")
            return contract

    generator = RetryingGenerator()

    result = ContractAgent(generator=generator, max_attempts=2).generate(
        "Update the core module",
        repo_context,
    )

    assert result == contract
    assert generator.attempts == 2


def test_workspace_keeps_source_unchanged_and_saves_patch(tmp_path):
    repo = prepare_repo(tmp_path)
    manager = WorkspaceManager()
    workspace = manager.create(repo)
    try:
        changed_file = workspace.path / "src" / "demo" / "core.py"
        changed_file.write_text("VALUE = 2\n", encoding="utf-8")

        patch = manager.collect_patch(workspace)
        patch_path = manager.save_patch(workspace)

        assert "src/demo/core.py" in patch
        assert "VALUE = 2" not in (repo / "src" / "demo" / "core.py").read_text(
            encoding="utf-8"
        )
        assert patch_path.is_file()
    finally:
        manager.cleanup(workspace)


def test_harness_uses_trace_manager_factory_for_run_trace(tmp_path):
    repo = prepare_repo(tmp_path)
    created = []

    def trace_manager_factory(run_id, writer):
        trace = TraceManager(run_id, writer)
        created.append((run_id, writer, trace))
        return trace

    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: make_contract()),
        coding_agent=ScriptedRuntimeAgent((Action.create(ActionType.FINISH),)),
        trace_manager_factory=trace_manager_factory,
        approval_provider=ApprovedProvider(),
    )

    result = harness.run(write_task(tmp_path), repo)

    assert result.success is True
    assert len(created) == 1
    run_id, writer, trace = created[0]
    assert isinstance(run_id, str) and run_id
    assert writer.__class__.__name__ == "TraceWriter"
    assert [event.type for event in trace.get_events()][0] == "task_started"


def write_task(tmp_path: Path) -> Path:
    task = tmp_path / "task.md"
    task.write_text("Update the demo module", encoding="utf-8")
    return task


class ScriptedRuntimeAgent:
    def __init__(self, actions):
        self._actions = list(actions)

    def decide(self, state, context):
        return self._actions.pop(0)


class ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def test_harness_verification_uses_the_canonical_command_executor(tmp_path, monkeypatch):
    repo = prepare_repo(tmp_path)
    invocations = []

    def fake_run(command, **kwargs):
        invocations.append((command, kwargs))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("code_governance.tools.shell.subprocess.run", fake_run)
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: make_contract()),
        coding_agent=ScriptedRuntimeAgent((Action.create(ActionType.FINISH),)),
        approval_provider=ApprovedProvider(),
    )

    result = harness.run(write_task(tmp_path), repo)

    assert result.success is True
    assert result.report.verification.commands[0].command == "git diff --check"
    assert [command for command, _ in invocations if command == ["git", "diff", "--check"]] == [
        ["git", "diff", "--check"]
    ]


def test_default_harness_uses_decide_only_canonical_agent(tmp_path):
    repo = prepare_repo(tmp_path)
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: make_contract(expected_files=())),
        coding_agent=ScriptedRuntimeAgent((Action.create(ActionType.FINISH),)),
        approval_provider=ApprovedProvider(),
    )

    result = harness.run(write_task(tmp_path), repo)

    assert result.success is True
    assert result.termination_reason == "MODEL_FINISH"


def test_default_contract_agent_requires_clarification_for_opaque_shell_text(tmp_path):
    repo = prepare_repo(tmp_path)

    class ContextCapturingAgent(ScriptedRuntimeAgent):
        def __init__(self):
            super().__init__((Action.create(ActionType.FINISH),))
            self.context = None

        def decide(self, state, context):
            self.context = context
            return super().decide(state, context)

    task = tmp_path / "explicit-task.md"
    task.write_text(
        "Update src/demo/core.py. Run git diff --check.",
        encoding="utf-8",
    )
    agent = ContextCapturingAgent()
    result = HarnessOrchestrator(coding_agent=agent).run(task, repo)

    assert result.success is False
    assert result.contract.version == 3
    assert result.contract.scope.paths == []
    assert result.contract.verification.claims == []
    assert result.contract.needs_clarification is True
    assert agent.context is None


def make_runtime_harness(actions, trace_manager_factory=None):
    return HarnessOrchestrator(
        contract_agent=ContractAgent(
            generator=lambda task, context: make_contract(expected_files=())
        ),
        coding_agent=ScriptedRuntimeAgent(actions),
        runtime_loop=True,
        trace_manager_factory=trace_manager_factory,
        approval_provider=ApprovedProvider(),
    )


def test_runtime_harness_native_read_does_not_request_approval(tmp_path):
    repo = prepare_repo(tmp_path)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    run_git(repo, "add", "seed.txt")
    run_git(repo, "commit", "-m", "add seed")
    traces = []
    harness = make_runtime_harness(
        (
            Action.create(ActionType.READ, "seed.txt"),
            Action.create(ActionType.FINISH),
        ),
        trace_manager_factory=lambda run_id, writer: traces.append(
            TraceManager(run_id, writer)
        ) or traces[-1],
    )

    result = harness.run(write_task(tmp_path), repo)

    assert result.success is True
    assert result.report.status == "PASS"
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "seed\n"
    event_types = [event.type for event in traces[0].get_events()]
    assert not any(
        event.type == "approval_requested" and event.data["effect"] == "file.read"
        for event in traces[0].get_events()
    )
    assert next(
        event.data["decision"]
        for event in traces[0].get_events()
        if event.type == "tool_policy_decided"
    ) == "ALLOW"


def test_orchestrator_full_loop_records_artifacts(tmp_path):
    repo = prepare_repo(tmp_path)
    orchestrator = Orchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: make_contract()),
        coding_agent=ScriptedRuntimeAgent((Action.create(ActionType.FINISH),)),
        approval_provider=ApprovedProvider(),
    )

    result = orchestrator.run(write_task(tmp_path), repo)

    assert result.success is True
    assert result.report.status == "PASS"
    assert result.patch_path.is_file()
    decisions = repo / ".agent" / "memory" / "decisions.json"
    assert json.loads(decisions.read_text(encoding="utf-8"))[0]["task"] == "Update the demo module"
    traces = list((repo / ".agent" / "runs").glob("*/trace.json"))
    assert traces
    trace = json.loads(traces[0].read_text(encoding="utf-8"))
    events = {event["type"]: event["data"] for event in trace["events"]}
    assert events["context_created"] == {
        "version": "v1",
        "files": [],
        "memories": [],
        "strategies": [],
        "tools": ["filesystem", "git", "shell"],
    }
    assert {"task_started", "plan_created", "task_finished"} <= set(events)
