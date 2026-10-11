from datetime import datetime, timezone

from code_governance.memory.manager import MemoryManager
from code_governance.memory.models import FailureRecord
from code_governance.memory.retriever import MemoryRetriever
from code_governance.repo.analyzer import RepoContext


def failure_record() -> FailureRecord:
    return FailureRecord(
        run_id="run-1",
        timestamp=datetime(2026, 8, 24, tzinfo=timezone.utc).isoformat(),
        task="Extend the channel",
        validator_findings=(
            {
                "code": "unexpected_file",
                "message": "core.py was outside scope",
                "path": "core.py",
            },
        ),
        changed_files=("core.py",),
        contract_summary={"expected_files": ["channels.py"]},
    )


def test_failure_records_strategy_and_retriever_returns_it(tmp_path):
    manager = MemoryManager(tmp_path)
    manager.record_failure(failure_record())
    context = RepoContext(
        root=tmp_path,
        files=("core.py",),
        python_modules=("core",),
        git_status=(),
        core_files=("core.py",),
        module_relationships={},
    )
    retriever = MemoryRetriever(manager)

    retrieval = retriever.retrieve_context("Fix core.py", context)

    assert manager.load("strategies.json")[0]["source_failure"] == "scope_violation"
    assert retrieval.selected_strategies[0].strategy.startswith("Before executing")
