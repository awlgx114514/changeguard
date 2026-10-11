from code_governance.runtime.action import Action, ActionType
from code_governance.runtime.history import HistoryManager, Issue, IssueStatus, RuleBasedSummaryProvider
from code_governance.runtime.observation import Observation
from code_governance.runtime.state import AgentState


def state() -> AgentState:
    return AgentState.initialize("extend a channel", {}, {})


def test_history_compresses_duplicate_file_reads():
    manager = HistoryManager()
    current = state()
    action = Action.create(ActionType.READ, target="channels.py", reason="inspect interface")
    observation = Observation(action.id, success=True, summary="Read channels.py", type="file_content")

    manager.record(current, action, observation)
    manager.record(current, action, observation)

    assert len(current.working_memory_summary) == 1
    assert current.working_memory_summary[0].summary == "Read channels.py"


def test_history_merges_file_observations_into_module_summary():
    provider = RuleBasedSummaryProvider()
    observations = (
        Observation("a", success=True, summary="Read channels.py", artifact_reference="channels.py", type="file_content"),
        Observation("b", success=True, summary="Read tests/test_channels.py", artifact_reference="tests/test_channels.py", type="file_content"),
    )

    summary = provider.merge_observations(observations)

    assert summary.summary == "Reviewed 2 related files"
    assert summary.importance >= 50


def test_history_resolves_an_active_issue_and_removes_it_from_working_state():
    manager = HistoryManager()
    current = state()
    current.issues.append(
        Issue("issue-1", "tool_error", "missing register method", IssueStatus.OPEN)
    )

    manager.resolve_issue(current, "issue-1")

    assert current.open_issues == ()
    assert current.decisions[-1] == "Resolved issue: missing register method"


def test_history_orders_blocking_facts_before_ordinary_reads():
    manager = HistoryManager()
    current = state()
    read = Action.create(ActionType.READ, target="channels.py")
    failed = Action.create(ActionType.RUN_COMMAND, target="tests")

    manager.record(current, read, Observation(read.id, success=True, summary="Read channels.py", type="file_content"))
    manager.record(
        current,
        failed,
        Observation(failed.id, success=False, summary="Tests failed", error="missing register method", type="command_result"),
    )

    assert (
        current.working_memory_summary[0].importance
        > current.working_memory_summary[-1].importance
    )
    assert [issue.description for issue in current.open_issues] == ["missing register method"]
