from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, List

from .action import Action
from .history import Issue, IssueStatus, WorkingMemoryItem
from .observation import Observation
from .execution import FailureRecord


@dataclass
class AgentState:
    """Serializable, compact state needed to resume one Runtime task."""

    run_id: str = ""
    task_id: str = ""
    task_description: str = ""
    status: str = "CREATED"
    current_step: str = ""
    current_goal: str = ""
    workspace_reference: str = ""
    plan_reference: str = "initial_plan"
    context_reference: str = "initial_context"
    working_memory_summary: List[WorkingMemoryItem] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    recent_actions: List[Action] = field(default_factory=list)
    recent_observations: List[Observation] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    failure: FailureRecord = None
    termination_reason: str = ""
    validation_result: str = "NOT_RUN"
    final_outcome: str = ""
    step_count: int = 0
    max_steps: int = 10
    retry_count: int = 0
    workspace_revision: int = 0
    contract: dict = field(default_factory=dict)

    @classmethod
    def initialize(
        cls,
        task_description,
        context=None,
        current_plan=None,
        workspace: Path = None,
        run_id: str = "",
        max_steps: int = 10,
        task_id: str = "",
        workspace_reference: str = "",
        plan_reference: str = "initial_plan",
        context_reference: str = "initial_context",
        contract: Any = None,
    ) -> "AgentState":
        """Create State without retaining supplied Context or Plan objects."""
        del context, current_plan
        description = str(task_description)
        contract_payload = (
            contract.model_dump(mode="json")
            if hasattr(contract, "model_dump")
            else (dict(contract) if isinstance(contract, dict) else {})
        )
        return cls(
            run_id=run_id,
            task_id=task_id or run_id or "task",
            task_description=description,
            current_goal=description,
            workspace_reference=workspace_reference or (str(workspace) if workspace is not None else ""),
            plan_reference=plan_reference,
            context_reference=context_reference,
            max_steps=max_steps,
            contract=contract_payload,
        )

    @property
    def open_issues(self):
        return tuple(issue for issue in self.issues if issue.status == IssueStatus.OPEN)

    def record_action(self, action: Action) -> None:
        self.recent_actions.append(action)
        self.recent_actions[:] = self.recent_actions[-self.max_steps :]
        self.step_count += 1

    def record_observation(self, observation: Observation) -> None:
        observation.metadata.setdefault("workspace_revision_before", self.workspace_revision)
        self.recent_observations.append(observation)
        if not observation.success and observation.error:
            self.errors.append(observation.error)
        self.recent_observations[:] = self.recent_observations[-self.max_steps :]
        if observation.success and observation.metadata.get("state_changed"):
            self.workspace_revision += 1
        observation.metadata["workspace_revision_after"] = self.workspace_revision

    def set_failure(self, failure: FailureRecord) -> None:
        if self.failure is None:
            self.failure = failure

    def transition(self, status: str) -> None:
        status = str(status).upper()
        if status not in {"CREATED", "PLANNING", "EXECUTING", "STOPPED", "MODEL_FINISHED", "VERIFYING", "VALIDATING", "REPAIRING", "SUCCEEDED", "FINISHED", "FAILED"}:
            raise ValueError(f"unsupported AgentState status: {status}")
        self.status = status

    def resolve_issue(self, issue_id: str) -> None:
        for index, issue in enumerate(self.issues):
            if issue.id == issue_id and issue.status == IssueStatus.OPEN:
                self.issues[index] = Issue(
                    id=issue.id,
                    type=issue.type,
                    description=issue.description,
                    status=IssueStatus.RESOLVED,
                )
                self.decisions.append(f"Resolved issue: {issue.description}")
                self.decisions[:] = self.decisions[-10:]
                return

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "task_description": self.task_description,
            "status": self.status,
            "current_goal": self.current_goal,
            "current_step": self.current_step,
            "workspace_reference": self.workspace_reference,
            "plan_reference": self.plan_reference,
            "context_reference": self.context_reference,
            "working_memory_summary": [item.to_dict() for item in self.working_memory_summary],
            "issues": [issue.to_dict() for issue in self.issues],
            "decisions": list(self.decisions),
            "recent_actions": [action.to_dict() for action in self.recent_actions],
            "recent_observations": [item.to_dict() for item in self.recent_observations],
            "errors": list(self.errors),
            "failure": None if self.failure is None else self.failure.to_dict(),
            "termination_reason": self.termination_reason,
            "validation_result": self.validation_result,
            "final_outcome": self.final_outcome,
            "step_count": self.step_count,
            "max_steps": self.max_steps,
            "retry_count": self.retry_count,
            "workspace_revision": self.workspace_revision,
            "contract": dict(self.contract),
        }

    @classmethod
    def from_dict(cls, value: dict) -> "AgentState":
        legacy_issues = [
            Issue.create("runtime_error", str(item)) for item in value.get("active_issues", [])
        ]
        return cls(
            run_id=str(value.get("run_id", "")),
            task_id=str(value.get("task_id", value.get("run_id", "task"))),
            task_description=str(value.get("task_description", value.get("task", ""))),
            status=str(value.get("status", "CREATED")).upper(),
            current_goal=str(value.get("current_goal", value.get("task_description", value.get("task", "")))),
            current_step=str(value.get("current_step", "")),
            workspace_reference=str(value.get("workspace_reference", value.get("workspace", ""))),
            plan_reference=str(value.get("plan_reference", "initial_plan")),
            context_reference=str(value.get("context_reference", "initial_context")),
            working_memory_summary=[
                WorkingMemoryItem.from_dict(item)
                for item in value.get("working_memory_summary", value.get("working_memory", []))
            ],
            issues=[Issue.from_dict(item) for item in value.get("issues", [])] or legacy_issues,
            decisions=[str(item) for item in value.get("decisions", [])],
            recent_actions=[
                Action.from_dict(item) for item in value.get("recent_actions", value.get("actions", []))
            ],
            recent_observations=[
                Observation.from_dict(item)
                for item in value.get("recent_observations", value.get("observations", []))
            ],
            errors=[str(item) for item in value.get("errors", [])],
            failure=(
                FailureRecord.from_dict(value["failure"])
                if isinstance(value.get("failure"), dict)
                else None
            ),
            termination_reason=str(value.get("termination_reason", "")),
            validation_result=str(value.get("validation_result", "NOT_RUN")),
            final_outcome=str(value.get("final_outcome", "")),
            step_count=int(value.get("step_count", len(value.get("recent_actions", value.get("actions", []))))),
            max_steps=int(value.get("max_steps", 10)),
            retry_count=int(value.get("retry_count", 0)),
            workspace_revision=int(value.get("workspace_revision", 0)),
            contract=dict(value.get("contract", {})),
        )

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "AgentState":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
