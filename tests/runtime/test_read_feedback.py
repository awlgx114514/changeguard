from pathlib import Path

from code_governance.agent.codex_agent import CodexAgent
from code_governance.context.builder import ContextBuilder
from code_governance.context.models import AgentContext
from code_governance.memory.retriever import TaskContext
from code_governance.repo.analyzer import RepoContext
from code_governance.runtime.action import Action, ActionType
from code_governance.runtime.loop import AgentLoop
from code_governance.runtime.state import AgentState
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.registry import ToolRegistry
from code_governance.trace.writer import TraceManager


class _ReadThenFinishAgent:
    def __init__(self):
        self.contexts = []
        self.second_decision_prompt = ""

    def decide(self, state, context):
        self.contexts.append(context)
        if len(self.contexts) == 1:
            return Action.create(ActionType.READ, target="app.py", reason="inspect app")
        self.second_decision_prompt = CodexAgent()._build_decision_prompt(state, context)
        return Action.create(ActionType.FINISH, reason="complete")


def _context(workspace: Path) -> AgentContext:
    return AgentContext(
        version="v1",
        task_context=TaskContext(
            task_text="Update app.py",
            task_type="update",
            target_files=("app.py",),
            modules=("app",),
            keywords=("app",),
        ),
        repository_context=RepoContext(
            root=workspace,
            files=("app.py",),
            python_modules=("app",),
            git_status=(),
            core_files=("app.py",),
            module_relationships={"app.py": ()},
        ),
        memory_context=(),
        project_rules=(),
        relevant_files=(),
        constraints=(),
    )


def test_successful_read_content_reaches_the_next_decision_prompt(tmp_path):
    marker = "EXPECTED_APP_CONTENT"
    (tmp_path / "app.py").write_text(f"marker = '{marker}'\n", encoding="utf-8")
    agent = _ReadThenFinishAgent()
    registry = ToolRegistry()
    registry.register(FilesystemTool())
    state = AgentState.initialize("Update app.py", workspace=tmp_path)

    AgentLoop(
        agent,
        registry,
        TraceManager("feedback-test"),
        context_builder=ContextBuilder(),
    ).run(state, _context(tmp_path), tmp_path)

    assert marker in agent.second_decision_prompt
