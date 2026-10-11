from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.orchestrator import Orchestrator
from code_governance.contract.schema import (
    ChangeContract,
    Constraints,
    Expected,
    Intent,
    Metadata,
    Verification,
    VerificationClaim,
)
from code_governance.memory.analyzer import FailureAnalyzer
from code_governance.memory.manager import MemoryManager
from code_governance.memory.models import FailureRecord, MemoryRule
from code_governance.memory.resolver import MemoryResolver
from code_governance.memory.store import MemoryStore
from code_governance.runtime.action import Action, OperationKind
from code_governance.tools import FilesystemTool, ShellTool, ToolRegistry


def timestamp():
    return datetime(2026, 8, 23, tzinfo=timezone.utc).isoformat()


def failure_record(path="core.py"):
    return FailureRecord(
        run_id="run-1",
        timestamp=timestamp(),
        task="Add a Channel",
        validator_findings=(
            {
                "code": "unexpected_file",
                "message": "Changed file is outside expected files",
                "path": path,
            },
        ),
        changed_files=(path,),
        contract_summary={"expected_files": ["channels.py"]},
    )


def rule(rule_id, rule, scope, confidence=0.6, source_runs=("run-1",)):
    return MemoryRule(
        id=rule_id,
        type="avoid_path",
        rule=rule,
        scope=scope,
        confidence=confidence,
        source_runs=source_runs,
        created_at=timestamp(),
        updated_at=timestamp(),
    )


def test_failure_record_keeps_raw_facts_only():
    record = failure_record()

    payload = record.to_dict()

    assert payload["validator_findings"][0]["code"] == "unexpected_file"
    assert payload["changed_files"] == ["core.py"]
    assert "root_cause" not in payload


def test_failure_analyzer_explains_unexpected_file_scope_violation():
    analysis = FailureAnalyzer().analyze(failure_record())

    assert analysis.failure_type == "scope_violation"
    assert analysis.root_cause == "implementation exceeded contract scope"
    assert "core.py" in analysis.symptom
    assert "Contract" in analysis.prevention


def test_resolver_merges_duplicate_rules_without_duplication():
    existing = rule("old", "Avoid modifying core.py", "core.py", source_runs=("run-1",))
    candidate = rule("new", "Do not modify core.py", "core.py", source_runs=("run-2",))

    rules = MemoryResolver().resolve(candidate, [existing])

    assert len(rules) == 1
    assert rules[0].status == "active"
    assert rules[0].source_runs == ("run-1", "run-2")
    assert rules[0].confidence > existing.confidence


def test_resolver_deprecates_lower_confidence_conflicting_rule():
    existing = rule("old", "Avoid modifying core.py", "core.py", confidence=0.9)
    candidate = rule("new", "Allow modifying core.py", "core.py", confidence=0.2)

    rules = MemoryResolver().resolve(candidate, [existing])

    assert {item.status for item in rules} == {"active", "deprecated"}
    assert next(item for item in rules if item.status == "active").id == "old"


def test_resolver_can_deprecate_a_rule_without_deleting_history():
    deprecated = MemoryResolver().deprecate(rule("old", "Avoid core.py", "core.py"))

    assert deprecated.status == "deprecated"
    assert deprecated.id == "old"


def test_success_confirmation_increases_matching_rule_confidence(tmp_path):
    store = MemoryStore(tmp_path)
    original = rule("rule-1", "Avoid modifying core.py", "core.py")
    store.save_rules([original])
    manager = MemoryManager(tmp_path, store=store)

    rules = manager.confirm_success("run-2", ("channels.py",))

    assert rules[0].confidence > original.confidence
    patterns = json.loads(
        (tmp_path / ".agent" / "memory" / "successful_patterns.json").read_text(
            encoding="utf-8"
        )
    )
    assert patterns[0]["type"] == "constraint_confirmed"


def run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def prepare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test User")
    (repo / "app.py").write_text("old\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "initial")
    return repo


class FailingAgent:
    def __init__(self):
        self._actions = (
            Action.create_operation(
                OperationKind.FILE_CREATE,
                path="forbidden.py",
                content="changed\n",
                reason="exercise forbidden-file validation",
            ),
            Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete"),
        )

    def decide(self, state, context):
        del state, context
        return self._actions[0] if len(self._actions) == 1 else self._pop()

    def _pop(self):
        action, self._actions = self._actions[0], self._actions[1:]
        return action


def test_orchestrator_records_failure_memory_before_repair(tmp_path):
    repo = prepare_repo(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Update app", encoding="utf-8")
    contract = ChangeContract(
        metadata=Metadata(),
        intent=Intent(goal="Update app"),
        expected=Expected(files=[], change_level="L1"),
        constraints=Constraints(forbidden=["forbidden.py"]),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )
    tool_registry = ToolRegistry()
    tool_registry.register(FilesystemTool())
    tool_registry.register(ShellTool())
    result = Orchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: contract),
        coding_agent=FailingAgent(),
        tool_registry=tool_registry,
        max_repairs=0,
    ).run(task_file, repo)

    assert result.report.status == "FAIL"
    failures_path = repo / ".agent" / "memory" / "failures.json"
    failures = json.loads(failures_path.read_text(encoding="utf-8"))
    assert failures[0]["validator_findings"][0]["code"] == "forbidden_file"
    analysis_files = list((repo / ".agent" / "analysis" / "failures").glob("*.json"))
    assert analysis_files
