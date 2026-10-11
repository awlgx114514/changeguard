from pathlib import Path

from code_governance.context.builder import ContextBuilder
from code_governance.context.models import AgentContext
from code_governance.memory.retriever import TaskContext
from code_governance.repo.analyzer import RepoContext
from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.loop import AgentLoop
from code_governance.runtime.state import AgentState
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.policy import ToolPolicy
from code_governance.tools.registry import ToolRegistry
from code_governance.trace.writer import TraceManager


class _OutsideWorkspaceWriteAgent:
    def __init__(self):
        self.decision_count = 0

    def decide(self, state, context):
        self.decision_count += 1
        if self.decision_count == 1:
            return Action.create_operation(
                OperationKind.FILE_CREATE,
                path="../outside.py",
                content="EXPECTED_WRITE_CONTENT",
                reason="write outside the workspace",
            )
        return Action.create_operation(OperationKind.WORKFLOW_FINISH)


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


def test_outside_workspace_write_stops_at_target_resolution(tmp_path):
    registry = ToolRegistry(policy=ToolPolicy())
    registry.register(FilesystemTool())
    agent = _OutsideWorkspaceWriteAgent()
    state = AgentState.initialize("Update app.py", workspace=tmp_path)

    AgentLoop(
        agent,
        registry,
        TraceManager("write-feedback-test"),
        context_builder=ContextBuilder(),
    ).run(state, _context(tmp_path), tmp_path)

    assert state.status == "MODEL_FINISHED"
    assert state.recent_observations[0].metadata["failure_stage"] == "ACTION_VALIDATION"
    assert state.recent_observations[0].error_category == "INVALID_ACTION"
