from datetime import datetime, timezone

from code_governance.memory.models import MemoryRule
from code_governance.memory.retriever import MemoryRetriever, TaskContext


def rule(rule_id, scope, text, confidence=0.8):
    now = datetime(2026, 8, 23, tzinfo=timezone.utc).isoformat()
    return MemoryRule(
        id=rule_id,
        type="constraint",
        rule=text,
        scope=scope,
        confidence=confidence,
        source_runs=("run-1",),
        created_at=now,
        updated_at=now,
    )


class MemorySource:
    def __init__(self, rules):
        self._rules = rules

    def active_rules(self):
        return tuple(self._rules)


def context(**overrides):
    values = {
        "task_text": "Add a Channel without changing core.py",
        "task_type": "channel_extension",
        "target_files": ("agent_lens/channels.py",),
        "modules": ("agent_lens.channels",),
        "keywords": ("add", "channel", "core"),
    }
    values.update(overrides)
    return TaskContext(**values)


def test_retriever_prioritizes_scope_match():
    retriever = MemoryRetriever(
        MemorySource(
            [
                rule("scope", "channel_extension", "Avoid modifying core.py"),
                rule("other", "api_change", "Avoid API changes", confidence=1.0),
            ]
        )
    )

    rules = retriever.retrieve(context())

    assert [item.id for item in rules] == ["scope"]


def test_retriever_matches_target_module():
    retriever = MemoryRetriever(
        MemorySource(
            [
                rule("module", "agent_lens.channels", "Keep Channel interfaces stable"),
                rule("other", "database", "Avoid schema migration"),
            ]
        )
    )

    rules = retriever.retrieve(context(task_type="general"))

    assert [item.id for item in rules] == ["module"]


def test_retriever_matches_keywords():
    retriever = MemoryRetriever(
        MemorySource(
            [
                rule("keyword", "general", "Channel extension should avoid core.py"),
                rule("other", "general", "Database migration only"),
            ]
        )
    )

    rules = retriever.retrieve(context(task_type="unrelated", modules=(), target_files=()))

    assert [item.id for item in rules] == ["keyword"]


def test_retriever_sorts_equivalent_matches_by_confidence():
    retriever = MemoryRetriever(
        MemorySource(
            [
                rule("low", "channel_extension", "Avoid core.py", confidence=0.4),
                rule("high", "channel_extension", "Avoid router.py", confidence=0.9),
            ]
        )
    )

    rules = retriever.retrieve(context())

    assert [item.id for item in rules] == ["high", "low"]


def test_retriever_respects_limit():
    retriever = MemoryRetriever(
        MemorySource(
            [
                rule("one", "channel_extension", "Avoid core.py"),
                rule("two", "channel_extension", "Avoid router.py"),
            ]
        )
    )

    rules = retriever.retrieve(context(), limit=1)

    assert [item.id for item in rules] == ["one"]


def test_retriever_returns_empty_for_unrelated_memory():
    retriever = MemoryRetriever(
        MemorySource([rule("db", "database", "Database migration only")])
    )

    assert retriever.retrieve(context(task_type="unrelated", modules=(), target_files=())) == []
