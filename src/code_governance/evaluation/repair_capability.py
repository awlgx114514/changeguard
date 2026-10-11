"""Deterministic evidence-driven repair fixtures using the existing runner."""

from dataclasses import replace

from code_governance.agent.codex_agent import CodexAgent
from code_governance.runtime.action import Action, OperationKind
from code_governance.tools import ApprovalOutcome

from .core_capability import FIXTURE_ROOT, _CaptureHarnessFactory, core_capability_dataset
from .dataset import EvaluationDataset
from .runner import BenchmarkRunner


class _RepairScenarioAgent:
    """Test decisions depend on structured failure evidence, not a retry counter."""

    def __init__(self, task):
        self.task = task
        self.steps = {False: 0, True: 0}
        self.repair_contexts = []
        self.prompt_exposed = False

    def decide(self, state, context):
        repairing = bool(context.repair_context)
        self.steps[repairing] += 1
        step = self.steps[repairing]
        module = "auth" if self.task.id == "false_evidence" else "feature"
        path = f"src/{module}.py"
        if repairing:
            payload = [item.to_dict() for item in context.repair_context]
            self.repair_contexts.append(payload)
            # Exercise the actual provider-facing serialization without a model call.
            prompt = CodexAgent()._build_decision_prompt(state, context)
            self.prompt_exposed = all(item["failure_id"] in prompt for item in payload)
        if step == 1:
            return Action.create_operation(OperationKind.FILE_READ, path=path, reason="Inspect current code")
        if step == 2:
            observation = next(item for item in reversed(state.recent_observations)
                               if item.success and item.metadata.get("file_hash") and item.metadata.get("path") == path)
            if module == "auth":
                old, new = (('return password in ("", "123456")', 'return password == "123456"')
                            if repairing else ('return password == "" or password == "123456"',
                                               'return password in ("", "123456")'))
                if repairing:
                    assert context.repair_context[0].expected == {"accepted": False}
                    assert context.repair_context[0].observed == {"accepted": True}
            else:
                old, new = (('return "disabled"', 'return "enabled"')
                            if repairing else ('return None', 'return "disabled"'))
                if repairing:
                    assert context.repair_context[0].observed["returncode"] == 1
            return replace(Action.create_operation(
                OperationKind.FILE_REPLACE, path=path, expected_text=old, replacement=new,
                observed_file_hash=observation.metadata["file_hash"], reason="Apply observed local correction"),
                id=f"{module}:{'repair' if repairing else 'initial'}")
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="Request independent final validation")


class _RepairFixtureApproval:
    def __init__(self, task, deny_verification):
        self.task = task
        self.deny_verification = deny_verification
        self.records = []

    def request(self, request):
        module = "auth" if self.task.id == "false_evidence" else "feature"
        command_ids = {claim.to_request().id for claim in self.task.verification.claims}
        allowed = (request.effect == "command.run" and request.action_id in command_ids
                   and not self.deny_verification) or (
            request.effect == "file.replace" and request.action_id in {f"{module}:initial", f"{module}:repair"}
            and request.canonical_target.replace("\\", "/").endswith(f"/src/{module}.py"))
        outcome = ApprovalOutcome.APPROVED if allowed else ApprovalOutcome.REJECTED
        self.records.append({"action_id": request.action_id, "effect": request.effect,
                             "canonical_target": request.canonical_target, "outcome": outcome.value})
        return outcome


def run_repair_evaluation(*, context_builder=None, deny_verification=False):
    tasks = tuple(replace(task, repository_snapshot=("assertion_failure" if task.id == "false_evidence" else "verification_failure"))
                  for task in core_capability_dataset().tasks if task.id != "true_success")
    dataset = EvaluationDataset("evidence-driven-repair-v1", tasks, FIXTURE_ROOT / "repair")
    records = []
    for task in tasks:
        agent = _RepairScenarioAgent(task)
        capture = _CaptureHarnessFactory(task, max_repairs=1, context_builder=context_builder,
                                        approval_provider=_RepairFixtureApproval(task, deny_verification))
        trial = BenchmarkRunner(dataset, coding_agent_factory=lambda: agent, harness_factory=capture).run_task(task, "hardened")
        if capture.record is None:
            raise RuntimeError(f"Repair evaluation could not capture facts: {capture.error or trial.failure_category}")
        record = capture.record
        record["first_failing_boundary"] = next(
            (attempt["failure_boundary"] for attempt in record["validation_attempts"]
             if attempt["failure_boundary"]), trial.failure_category)
        record["final_failure_boundary"] = trial.failure_category
        record["agent_repair_contexts"] = agent.repair_contexts
        record["repair_prompt_exposed"] = agent.prompt_exposed
        record["benchmark_trial"] = trial.to_dict()
        attempts = record["validation_attempts"]
        record["scenario_passed"] = (
            record["harness_result"]["success"] and record["oracle_after"]["goal_satisfied"]
            and len(attempts) == 2 and attempts[0]["validation_result"]["status"] == "FAIL"
            and attempts[1]["validation_result"]["status"] == "PASS" and bool(agent.repair_contexts)
        )
        records.append(record)
    return records
